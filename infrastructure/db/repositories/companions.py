"""Persistence for showing and liking people for one planned event."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..social_models import CompanionInterest, CompanionView, EventPlan, Match, User, UserBlock
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


class CompanionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def next_candidate(self, user_id: UUID, plan_id: UUID) -> CompanionCard | None:
        plan = await self._own_looking_plan(user_id, plan_id)
        row = (
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
        if row is None:
            return None
        candidate_plan, candidate = row
        self.session.add(CompanionView(
            viewer_id=user_id, event_id=plan.event_id, shown_user_id=candidate.id,
        ))
        await self.session.flush()
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

    async def react(self, user_id: UUID, candidate_plan_id: UUID, *, liked: bool) -> CompanionReactionResult:
        candidate_plan = await self.session.get(EventPlan, candidate_plan_id)
        if candidate_plan is None or candidate_plan.status != "planned" or candidate_plan.company_status != "looking":
            raise OnboardingError("Companion is unavailable")
        owner_plan = await self.session.scalar(
            select(EventPlan).where(
                EventPlan.user_id == user_id,
                EventPlan.event_id == candidate_plan.event_id,
                EventPlan.status == "planned",
                EventPlan.company_status == "looking",
            )
        )
        if owner_plan is None or owner_plan.id == candidate_plan.id:
            raise OnboardingError("Company search is unavailable")
        view = await self.session.get(CompanionView, (user_id, candidate_plan.event_id, candidate_plan.user_id))
        if view is None:
            raise OnboardingError("Сначала открой эту анкету")
        if view.reacted_at is not None:
            raise OnboardingError("This profile has already been evaluated")
        view.reacted_at = datetime.now(timezone.utc)
        if not liked:
            return CompanionReactionResult(owner_plan.id, None)

        interest = await self.session.scalar(
            select(CompanionInterest).where(
                CompanionInterest.sender_plan_id == owner_plan.id,
                CompanionInterest.recipient_plan_id == candidate_plan.id,
            )
        )
        if interest is None:
            interest = CompanionInterest(
                sender_plan_id=owner_plan.id,
                recipient_plan_id=candidate_plan.id,
                event_id=candidate_plan.event_id,
            )
            self.session.add(interest)
        reverse = await self.session.scalar(
            select(CompanionInterest).where(
                CompanionInterest.sender_plan_id == candidate_plan.id,
                CompanionInterest.recipient_plan_id == owner_plan.id,
                CompanionInterest.status == "active",
            )
        )
        if reverse is None:
            return CompanionReactionResult(owner_plan.id, None)

        first_id, second_id = sorted((user_id, candidate_plan.user_id), key=str)
        match = await self.session.scalar(
            select(Match).where(
                Match.event_id == candidate_plan.event_id,
                Match.first_user_id == first_id,
                Match.second_user_id == second_id,
            )
        )
        if match is None:
            match = Match(
                event_id=candidate_plan.event_id,
                first_user_id=first_id,
                second_user_id=second_id,
            )
            self.session.add(match)
            await self.session.flush()
        return CompanionReactionResult(owner_plan.id, match.id)

    async def _own_looking_plan(self, user_id: UUID, plan_id: UUID) -> EventPlan:
        plan = await self.session.get(EventPlan, plan_id)
        if plan is None or plan.user_id != user_id or plan.status != "planned" or plan.company_status != "looking":
            raise OnboardingError("Company search is unavailable")
        return plan
