"""Persistence helpers for the fixed /demo match scenario."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..social_models import CompanionInterest, CompanionView, EventPlan, Match, User


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
    # Tag codes chosen as interests; None means every onboarding interest.
    interest_codes: tuple[str, ...] | None = ()


DEMO_PROFILES = (
    DemoProfile(
        id=uuid5(NAMESPACE_URL, "dvizhmax:demo:katya"),
        max_user_id=9_000_000_001,
        username="dvizhmax_demo_katya",
        name="Демо Катя",
        gender="female",
        age=24,
        description="Тестовая анкета: выбраны все интересы, чтобы показать общие.",
        asset_name="demo-profile-katya.png",
        interest_codes=None,
    ),
    DemoProfile(
        id=uuid5(NAMESPACE_URL, "dvizhmax:demo:sasha"),
        max_user_id=9_000_000_002,
        username="dvizhmax_demo_sasha",
        name="Демо Саша",
        gender="male",
        age=26,
        description="Тестовая анкета для демонстрации мэтча.",
        asset_name="demo-profile-sasha.png",
        interest_codes=("concert", "standup", "quiz"),
    ),
    DemoProfile(
        id=uuid5(NAMESPACE_URL, "dvizhmax:demo:lesha"),
        max_user_id=9_000_000_003,
        username="dvizhmax_demo_lesha",
        name="Демо Лёша",
        gender="male",
        age=23,
        description="Тестовая анкета для демонстрации мэтча.",
        asset_name="demo-profile-lesha.png",
        interest_codes=("sport", "quest", "board_games"),
    ),
)
DEMO_MAX_USER_IDS = frozenset(profile.max_user_id for profile in DEMO_PROFILES)
# Kept for compatibility with the demo notification formatter.
DEMO_MAX_USER_ID = DEMO_PROFILES[0].max_user_id


class DemoRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def event_id(self) -> UUID | None:
        return await self.session.scalar(
            select(EventPlan.event_id)
            .join(User, User.id == EventPlan.user_id)
            .where(
                User.max_user_id.in_(DEMO_MAX_USER_IDS),
                EventPlan.event_id == DEMO_EVENT_ID,
                EventPlan.status == "planned",
                EventPlan.company_status == "looking",
            )
            .limit(1)
        )

    async def reset_for_user(self, user_id: UUID) -> UUID | None:
        """Re-arm demo candidates and matches for one real user."""
        demo_plans = await self._active_demo_plans()
        if not demo_plans:
            return None
        event_id = DEMO_EVENT_ID
        user_plan = await self.session.scalar(select(EventPlan).where(
            EventPlan.user_id == user_id,
            EventPlan.event_id == event_id,
        ))
        if user_plan is None:
            return event_id

        demo_plan_ids = tuple(plan.id for plan in demo_plans)
        demo_user_ids = tuple(plan.user_id for plan in demo_plans)
        await self.session.execute(delete(CompanionView).where(
            CompanionView.viewer_id == user_id,
            CompanionView.event_id == event_id,
            CompanionView.shown_user_id.in_(demo_user_ids),
        ))
        await self.session.execute(
            update(CompanionInterest)
            .where(
                CompanionInterest.event_id == event_id,
                or_(
                    (CompanionInterest.sender_plan_id == user_plan.id)
                    & CompanionInterest.recipient_plan_id.in_(demo_plan_ids),
                    CompanionInterest.sender_plan_id.in_(demo_plan_ids)
                    & (CompanionInterest.recipient_plan_id == user_plan.id),
                ),
            )
            .values(status="withdrawn", viewed_at=None, announced_at=None)
        )
        for demo_user_id in demo_user_ids:
            first_user_id, second_user_id = sorted((user_id, demo_user_id), key=str)
            await self.session.execute(
                update(Match)
                .where(
                    Match.event_id == event_id,
                    Match.first_user_id == first_user_id,
                    Match.second_user_id == second_user_id,
                )
                .values(status="closed")
            )
        return event_id

    async def ensure_candidates_for_plan(self, *, user_id: UUID, user_plan_id: UUID) -> bool:
        """Attach the fixed test profiles to a selected event on demand."""
        user_plan = await self.session.get(EventPlan, user_plan_id)
        if (
            user_plan is None
            or user_plan.user_id != user_id
            or user_plan.status != "planned"
            or user_plan.company_status != "looking"
        ):
            return False
        demo_users = list((await self.session.scalars(
            select(User).where(User.max_user_id.in_(DEMO_MAX_USER_IDS))
        )).all())
        if not demo_users:
            return False

        for demo_user in demo_users:
            # Several users can open company search for one event together.
            # The unique plan constraint makes this upsert safe across callbacks.
            demo_plan_id = await self.session.scalar(
                insert(EventPlan)
                .values(
                    id=uuid4(),
                    user_id=demo_user.id,
                    event_id=user_plan.event_id,
                    status="planned",
                    company_status="looking",
                )
                .on_conflict_do_update(
                    index_elements=(EventPlan.user_id, EventPlan.event_id),
                    set_={
                        "status": "planned",
                        "company_status": "looking",
                    },
                )
                .returning(EventPlan.id)
            )
            await self.session.execute(
                insert(CompanionInterest)
                .values(
                    id=uuid4(),
                    sender_plan_id=demo_plan_id,
                    recipient_plan_id=user_plan.id,
                    event_id=user_plan.event_id,
                )
                .on_conflict_do_update(
                    index_elements=(
                        CompanionInterest.sender_plan_id,
                        CompanionInterest.recipient_plan_id,
                    ),
                    set_={
                        "status": "active",
                        "viewed_at": None,
                        "announced_at": None,
                    },
                )
            )
        return True

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
