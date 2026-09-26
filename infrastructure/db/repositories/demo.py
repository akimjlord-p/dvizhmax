"""Persistence helpers for the fixed /demo match scenario."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Tag
from ..social_models import (
    CompanionInterest, CompanionView, EventPlan, EventReaction, Match, MatchContact, User, UserTagWeight,
)


DEMO_EVENT_ID = uuid5(NAMESPACE_URL, "dvizhmax:demo:match-flow:v1")


@dataclass(frozen=True, slots=True)
class DemoProfile:
    id: UUID
    max_user_id: int
    username: str
    name: str
    gender: str
    age: int
    description: str
    asset_name: str
    # Chosen interests; empty means none, so there is nothing in common with anyone.
    interest_codes: tuple[str, ...] = ()
    # Copies the first interests of whoever runs /demo, to show common interests.
    mirrors_interests: bool = False
    # Has already liked the user, so a like back is a match.
    likes_user: bool = False


# Shown on the demo event in this order, before any real people.
DEMO_PROFILES = (
    DemoProfile(
        id=uuid5(NAMESPACE_URL, "dvizhmax:demo:lesha"),
        max_user_id=9_000_000_003,
        username="dvizhmax_demo_lesha",
        name="Демо Лёша",
        gender="male",
        age=23,
        description="Демо-анкета: без общих интересов. Лайк не будет взаимным.",
        asset_name="demo-profile-lesha.png",
    ),
    DemoProfile(
        id=uuid5(NAMESPACE_URL, "dvizhmax:demo:sasha"),
        max_user_id=9_000_000_002,
        username="dvizhmax_demo_sasha",
        name="Демо Саша",
        gender="male",
        age=26,
        description="Демо-анкета: 2–3 общих интереса с тобой. Лайк не будет взаимным.",
        asset_name="demo-profile-sasha.png",
        mirrors_interests=True,
    ),
    DemoProfile(
        id=uuid5(NAMESPACE_URL, "dvizhmax:demo:katya"),
        max_user_id=9_000_000_001,
        username="dvizhmax_demo_katya",
        name="Демо Катя",
        gender="female",
        age=24,
        description="Демо-анкета: уже лайкнула тебя. Лайк в ответ — мэтч и обмен контактом.",
        asset_name="demo-profile-katya.png",
        likes_user=True,
    ),
)
DEMO_MAX_USER_IDS = frozenset(profile.max_user_id for profile in DEMO_PROFILES)
DEMO_CANDIDATE_ORDER = {profile.max_user_id: position for position, profile in enumerate(DEMO_PROFILES)}
# Kept for compatibility: the demo profile that likes the user.
DEMO_MAX_USER_ID = next(profile.max_user_id for profile in DEMO_PROFILES if profile.likes_user)
MIRRORED_INTERESTS = 3
DEMO_CONTACT_TEXT = "https://max.ru/t110_hakaton_max_bot (демо-контакт: у демо-анкеты нет настоящего профиля)"


class DemoRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def reset_for_user(self, user_id: UUID) -> UUID | None:
        """Start the demo from scratch for one user; other events are untouched.

        Clears the user's views, likes, matches and contacts on the demo event,
        including those with real team members, so the scenario can be replayed.
        """
        if not await self._active_demo_plans():
            return None
        event_id = DEMO_EVENT_ID
        # "Не моё" on the demo card would block "Хочу пойти" there for good.
        await self.session.execute(delete(EventReaction).where(
            EventReaction.user_id == user_id,
            EventReaction.event_id == event_id,
            EventReaction.reaction == "skip",
        ))
        await self._mirror_interests(user_id)
        user_plan = await self.session.scalar(select(EventPlan).where(
            EventPlan.user_id == user_id,
            EventPlan.event_id == event_id,
        ))
        if user_plan is None:
            return event_id

        await self.session.execute(delete(CompanionView).where(
            CompanionView.viewer_id == user_id,
            CompanionView.event_id == event_id,
        ))
        await self.session.execute(
            update(CompanionInterest)
            .where(
                CompanionInterest.event_id == event_id,
                or_(
                    CompanionInterest.sender_plan_id == user_plan.id,
                    CompanionInterest.recipient_plan_id == user_plan.id,
                ),
            )
            .values(status="withdrawn", viewed_at=None, announced_at=None)
        )
        match_ids = select(Match.id).where(
            Match.event_id == event_id,
            or_(Match.first_user_id == user_id, Match.second_user_id == user_id),
        )
        await self.session.execute(delete(MatchContact).where(MatchContact.match_id.in_(match_ids)))
        await self.session.execute(update(Match).where(Match.id.in_(match_ids)).values(status="closed"))
        return event_id

    async def ensure_candidates_for_plan(self, *, user_id: UUID, user_plan_id: UUID) -> bool:
        """Put the demo profiles on the demo event; ordinary events never get them."""
        user_plan = await self.session.get(EventPlan, user_plan_id)
        if (
            user_plan is None
            or user_plan.event_id != DEMO_EVENT_ID
            or user_plan.user_id != user_id
            or user_plan.status != "planned"
            or user_plan.company_status != "looking"
        ):
            return False
        demo_users = {user.max_user_id: user for user in (await self.session.scalars(
            select(User).where(User.max_user_id.in_(DEMO_MAX_USER_IDS))
        )).all()}
        if not demo_users:
            return False

        for profile in DEMO_PROFILES:
            demo_user = demo_users.get(profile.max_user_id)
            if demo_user is None:
                continue
            # Several users can open company search at once; the unique plan
            # constraint makes this upsert safe across callbacks.
            demo_plan_id = await self.session.scalar(
                insert(EventPlan)
                .values(
                    id=uuid4(),
                    user_id=demo_user.id,
                    event_id=DEMO_EVENT_ID,
                    status="planned",
                    company_status="looking",
                )
                .on_conflict_do_update(
                    index_elements=(EventPlan.user_id, EventPlan.event_id),
                    set_={"status": "planned", "company_status": "looking"},
                )
                .returning(EventPlan.id)
            )
            if not profile.likes_user:
                continue
            await self.session.execute(
                insert(CompanionInterest)
                .values(
                    id=uuid4(),
                    sender_plan_id=demo_plan_id,
                    recipient_plan_id=user_plan.id,
                    event_id=DEMO_EVENT_ID,
                )
                .on_conflict_do_update(
                    index_elements=(CompanionInterest.sender_plan_id, CompanionInterest.recipient_plan_id),
                    set_={"status": "active", "viewed_at": None, "announced_at": None},
                )
            )
        return True

    async def _mirror_interests(self, user_id: UUID) -> None:
        """Give the mirroring demo profile the user's first chosen interests."""
        chosen = list((await self.session.scalars(
            select(Tag.id)
            .join(UserTagWeight, UserTagWeight.tag_id == Tag.id)
            .where(UserTagWeight.user_id == user_id, UserTagWeight.initial_weight > 0, Tag.is_active.is_(True))
            .order_by(Tag.name)
            .limit(MIRRORED_INTERESTS)
        )).all())
        for profile in DEMO_PROFILES:
            if not profile.mirrors_interests:
                continue
            demo_user = await self.session.scalar(select(User).where(User.max_user_id == profile.max_user_id))
            if demo_user is None:
                continue
            await self.session.execute(delete(UserTagWeight).where(UserTagWeight.user_id == demo_user.id))
            self.session.add_all(
                UserTagWeight(user_id=demo_user.id, tag_id=tag_id, initial_weight=Decimal("1.0"))
                for tag_id in chosen
            )
        await self.session.flush()

    async def _active_demo_plans(self) -> list[EventPlan]:
        return list((await self.session.scalars(
            select(EventPlan)
            .join(User, User.id == EventPlan.user_id)
            .where(
                User.max_user_id.in_(DEMO_MAX_USER_IDS),
                EventPlan.event_id == DEMO_EVENT_ID,
                EventPlan.status == "planned",
                EventPlan.company_status == "looking",
            )
        )).all())
