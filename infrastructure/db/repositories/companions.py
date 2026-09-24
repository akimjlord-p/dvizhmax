"""Persistence for showing and liking people for one planned event."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..social_models import CompanionInterest, CompanionView, EventPlan, Match, User, UserBlock
from .demo import DEMO_MAX_USER_IDS
from .onboarding import OnboardingError


@dataclass(frozen=True, slots=True)
class CompanionCard:
    plan_id: UUID
    user_id: UUID
    name: str
    gender: str | None
    age: int | None
    description: str | None
    photo_url: str | None
    photo_attachment: dict | None


@dataclass(frozen=True, slots=True)
class CompanionReactionResult:
    owner_plan_id: UUID
    match_id: UUID | None
    was_applied: bool
    created_match: bool


class CompanionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def next_candidate(self, user_id: UUID, plan_id: UUID) -> CompanionCard | None:
        plan = await self._own_looking_plan(user_id, plan_id)
        while True:
            row = await self._next_unseen_candidate(user_id, plan)
            if row is None:
                return None
            candidate_plan, candidate = row
            claimed = await self.session.scalar(
                insert(CompanionView)
                .values(viewer_id=user_id, event_id=plan.event_id, shown_user_id=candidate.id)
                .on_conflict_do_nothing(
                    index_elements=(
                        CompanionView.viewer_id,
                        CompanionView.event_id,
                        CompanionView.shown_user_id,
                    )
                )
                .returning(CompanionView.shown_user_id)
            )
            if claimed is None:
                # Another callback displayed this person first. Try the next candidate.
                continue
            return CompanionCard(
                plan_id=candidate_plan.id,
                user_id=candidate.id,
                name=candidate.name or "Без имени",
                gender=candidate.gender,
                age=candidate.age,
                description=candidate.description,
                photo_url=candidate.photo_url,
                photo_attachment=candidate.photo_attachment,
            )

    async def next_liker(self, user_id: UUID, plan_id: UUID) -> CompanionCard | None:
        """Show the newest person who liked this plan and still waits for an answer.

        Unlike ``next_candidate`` this may return someone the user already skipped,
        provided that their like arrived after that skip.
        """
        plan = await self.session.get(EventPlan, plan_id)
        if plan is None or plan.user_id != user_id or plan.status != "planned":
            raise OnboardingError("Этот план уже неактуален")
        if plan.company_status != "looking":
            raise OnboardingError("Включи поиск компании в «Моих планах», чтобы ответить")
        row = (
            await self.session.execute(
                select(EventPlan, User)
                .join(CompanionInterest, CompanionInterest.sender_plan_id == EventPlan.id)
                .join(User, User.id == EventPlan.user_id)
                .where(*self._pending_liker_conditions(user_id, plan.id))
                .order_by(CompanionInterest.updated_at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        candidate_plan, candidate = row
        # Re-open the view so the regular react() accepts an answer to this like.
        await self.session.execute(
            insert(CompanionView)
            .values(viewer_id=user_id, event_id=plan.event_id, shown_user_id=candidate.id)
            .on_conflict_do_update(
                index_elements=(CompanionView.viewer_id, CompanionView.event_id, CompanionView.shown_user_id),
                set_={"shown_at": func.now(), "reacted_at": None},
            )
        )
        return CompanionCard(
            plan_id=candidate_plan.id,
            user_id=candidate.id,
            name=candidate.name or "Без имени",
            gender=candidate.gender,
            age=candidate.age,
            description=candidate.description,
            photo_url=candidate.photo_url,
            photo_attachment=candidate.photo_attachment,
        )

    async def pending_liker_counts(self, user_id: UUID, plan_ids: list[UUID]) -> dict[UUID, int]:
        if not plan_ids:
            return {}
        rows = (
            await self.session.execute(
                select(CompanionInterest.recipient_plan_id, func.count())
                .join(EventPlan, EventPlan.id == CompanionInterest.sender_plan_id)
                .join(User, User.id == EventPlan.user_id)
                .where(
                    CompanionInterest.recipient_plan_id.in_(plan_ids),
                    *self._pending_liker_conditions(user_id, CompanionInterest.recipient_plan_id),
                )
                .group_by(CompanionInterest.recipient_plan_id)
            )
        ).all()
        return {plan_id: count for plan_id, count in rows}

    @staticmethod
    def _pending_liker_conditions(user_id: UUID, recipient_plan_id) -> tuple:
        """Active likes to the plan from searching real people the user has not answered yet.

        Expects ``EventPlan`` to be the sender plan and ``User`` its owner.
        """
        reverse = aliased(CompanionInterest)
        return (
            CompanionInterest.recipient_plan_id == recipient_plan_id,
            CompanionInterest.status == "active",
            EventPlan.status == "planned",
            EventPlan.company_status == "looking",
            EventPlan.user_id != user_id,
            User.profile_status == "active",
            User.max_user_id.not_in(DEMO_MAX_USER_IDS),
            ~exists(
                select(reverse.id).where(
                    reverse.sender_plan_id == recipient_plan_id,
                    reverse.recipient_plan_id == CompanionInterest.sender_plan_id,
                    reverse.status == "active",
                )
            ),
            ~exists(
                select(CompanionView.viewer_id).where(
                    CompanionView.viewer_id == user_id,
                    CompanionView.event_id == CompanionInterest.event_id,
                    CompanionView.shown_user_id == EventPlan.user_id,
                    CompanionView.reacted_at >= CompanionInterest.updated_at,
                )
            ),
            ~exists(
                select(UserBlock.blocker_id).where(
                    or_(
                        (UserBlock.blocker_id == user_id) & (UserBlock.blocked_id == EventPlan.user_id),
                        (UserBlock.blocker_id == EventPlan.user_id) & (UserBlock.blocked_id == user_id),
                    )
                )
            ),
        )

    async def react(self, user_id: UUID, candidate_plan_id: UUID, *, liked: bool) -> CompanionReactionResult:
        candidate_plan = await self.session.get(EventPlan, candidate_plan_id)
        if candidate_plan is None:
            raise OnboardingError("Companion is unavailable")
        owner_plan_id = await self.session.scalar(
            select(EventPlan.id).where(
                EventPlan.user_id == user_id,
                EventPlan.event_id == candidate_plan.event_id,
                EventPlan.status == "planned",
                EventPlan.company_status == "looking",
            )
        )
        if owner_plan_id is None or owner_plan_id == candidate_plan.id:
            raise OnboardingError("Company search is unavailable")

        # Both reciprocal callbacks lock this same pair in one deterministic order.
        # The second transaction therefore sees the first interest and creates the match.
        locked_plans = list(
            (
                await self.session.scalars(
                    select(EventPlan)
                    .where(EventPlan.id.in_((owner_plan_id, candidate_plan.id)))
                    .order_by(EventPlan.id)
                    .with_for_update()
                )
            ).all()
        )
        if len(locked_plans) != 2:
            raise OnboardingError("Company search is unavailable")
        plans_by_id = {plan.id: plan for plan in locked_plans}
        owner_plan = plans_by_id[owner_plan_id]
        candidate_plan = plans_by_id[candidate_plan.id]
        if (
            owner_plan.status != "planned"
            or owner_plan.company_status != "looking"
            or candidate_plan.status != "planned"
            or candidate_plan.company_status != "looking"
        ):
            raise OnboardingError("Companion is unavailable")

        view = await self.session.scalar(
            select(CompanionView)
            .where(
                CompanionView.viewer_id == user_id,
                CompanionView.event_id == candidate_plan.event_id,
                CompanionView.shown_user_id == candidate_plan.user_id,
            )
            .with_for_update()
        )
        if view is None:
            raise OnboardingError("Сначала открой эту анкету")
        if view.reacted_at is not None:
            return CompanionReactionResult(owner_plan.id, None, was_applied=False, created_match=False)
        view.reacted_at = datetime.now(timezone.utc)
        if not liked:
            return CompanionReactionResult(owner_plan.id, None, was_applied=True, created_match=False)

        # A resumed plan reuses its old database row. Reactivate an earlier
        # withdrawn interest so the user can match again after coming back.
        await self.session.execute(
            insert(CompanionInterest)
            .values(
                sender_plan_id=owner_plan.id,
                recipient_plan_id=candidate_plan.id,
                event_id=candidate_plan.event_id,
            )
            .on_conflict_do_update(
                index_elements=(CompanionInterest.sender_plan_id, CompanionInterest.recipient_plan_id),
                set_={
                    "status": "active",
                    "viewed_at": None,
                    "announced_at": None,
                    "updated_at": func.now(),
                },
            )
        )
        reverse = await self.session.scalar(
            select(CompanionInterest).where(
                CompanionInterest.sender_plan_id == candidate_plan.id,
                CompanionInterest.recipient_plan_id == owner_plan.id,
                CompanionInterest.status == "active",
            )
        )
        if reverse is None:
            return CompanionReactionResult(owner_plan.id, None, was_applied=True, created_match=False)

        first_id, second_id = sorted((user_id, candidate_plan.user_id), key=str)
        match = await self.session.scalar(
            select(Match).where(
                Match.event_id == candidate_plan.event_id,
                Match.first_user_id == first_id,
                Match.second_user_id == second_id,
            )
        )
        if match is not None:
            reopened = match.status == "closed"
            if reopened:
                match.status = "active"
            return CompanionReactionResult(owner_plan.id, match.id, was_applied=True, created_match=reopened)

        match_id = await self.session.scalar(
            insert(Match)
            .values(
                event_id=candidate_plan.event_id,
                first_user_id=first_id,
                second_user_id=second_id,
            )
            .on_conflict_do_nothing(
                index_elements=(Match.event_id, Match.first_user_id, Match.second_user_id)
            )
            .returning(Match.id)
        )
        if match_id is not None:
            return CompanionReactionResult(owner_plan.id, match_id, was_applied=True, created_match=True)
        match_id = await self.session.scalar(
            select(Match.id).where(
                Match.event_id == candidate_plan.event_id,
                Match.first_user_id == first_id,
                Match.second_user_id == second_id,
            )
        )
        return CompanionReactionResult(owner_plan.id, match_id, was_applied=True, created_match=False)

    async def _next_unseen_candidate(self, user_id: UUID, plan: EventPlan):
        return (
            await self.session.execute(
                select(EventPlan, User)
                .join(User, User.id == EventPlan.user_id)
                .where(
                    EventPlan.event_id == plan.event_id,
                    EventPlan.user_id != user_id,
                    EventPlan.status == "planned",
                    EventPlan.company_status == "looking",
                    User.profile_status == "active",
                    ~exists(
                        select(CompanionView.viewer_id).where(
                            CompanionView.viewer_id == user_id,
                            CompanionView.event_id == plan.event_id,
                            CompanionView.shown_user_id == EventPlan.user_id,
                        )
                    ),
                    ~exists(
                        select(UserBlock.blocker_id).where(
                            UserBlock.blocker_id == user_id,
                            UserBlock.blocked_id == EventPlan.user_id,
                        )
                    ),
                    ~exists(
                        select(UserBlock.blocker_id).where(
                            UserBlock.blocker_id == EventPlan.user_id,
                            UserBlock.blocked_id == user_id,
                        )
                    ),
                )
                .order_by(EventPlan.created_at.desc())
                .limit(1)
            )
        ).first()

    async def _own_looking_plan(self, user_id: UUID, plan_id: UUID) -> EventPlan:
        plan = await self.session.get(EventPlan, plan_id)
        if plan is None or plan.user_id != user_id or plan.status != "planned" or plan.company_status != "looking":
            raise OnboardingError("Company search is unavailable")
        return plan
