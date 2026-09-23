"""Small, explicit seed used to demonstrate the social MVP flow."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..social_models import CompanionInterest, CompanionView, EventPlan, Match, User


DEMO_MAX_USER_ID = 9_000_000_001


class DemoRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def event_id(self) -> UUID | None:
        return await self.session.scalar(
            select(EventPlan.event_id)
            .join(User, User.id == EventPlan.user_id)
            .where(
                User.max_user_id == DEMO_MAX_USER_ID,
                EventPlan.status == "planned",
                EventPlan.company_status == "looking",
            )
            .limit(1)
        )

    async def reset_for_user(self, user_id: UUID) -> UUID | None:
        """Re-arm the isolated demo scenario for one user without touching real data."""
        demo_plan = await self.session.scalar(
            select(EventPlan)
            .join(User, User.id == EventPlan.user_id)
            .where(
                User.max_user_id == DEMO_MAX_USER_ID,
                EventPlan.status == "planned",
                EventPlan.company_status == "looking",
            )
            .limit(1)
        )
        if demo_plan is None:
            return None

        user_plan = await self.session.scalar(
            select(EventPlan).where(
                EventPlan.user_id == user_id,
                EventPlan.event_id == demo_plan.event_id,
            )
        )
        if user_plan is None:
            return demo_plan.event_id

        await self.session.execute(
            delete(CompanionView).where(
                CompanionView.viewer_id == user_id,
                CompanionView.event_id == demo_plan.event_id,
                CompanionView.shown_user_id == demo_plan.user_id,
            )
        )
        await self.session.execute(
            update(CompanionInterest)
            .where(
                CompanionInterest.event_id == demo_plan.event_id,
                or_(
                    (CompanionInterest.sender_plan_id == user_plan.id)
                    & (CompanionInterest.recipient_plan_id == demo_plan.id),
                    (CompanionInterest.sender_plan_id == demo_plan.id)
                    & (CompanionInterest.recipient_plan_id == user_plan.id),
                ),
            )
            .values(status="withdrawn", viewed_at=None, announced_at=None)
        )
        first_user_id, second_user_id = sorted((user_id, demo_plan.user_id), key=str)
        await self.session.execute(
            update(Match)
            .where(
                Match.event_id == demo_plan.event_id,
                Match.first_user_id == first_user_id,
                Match.second_user_id == second_user_id,
            )
            .values(status="closed")
        )
        return demo_plan.event_id

    async def arm_reverse_interest(self, *, user_id: UUID, user_plan_id: UUID) -> bool:
        """Prepare a reciprocal demo like for the seeded event only."""
        user_plan = await self.session.get(EventPlan, user_plan_id)
        if user_plan is None or user_plan.user_id != user_id or user_plan.company_status != "looking":
            return False
        demo_plan = await self.session.scalar(
            select(EventPlan)
            .join(User, User.id == EventPlan.user_id)
            .where(
                User.max_user_id == DEMO_MAX_USER_ID,
                EventPlan.event_id == user_plan.event_id,
                EventPlan.status == "planned",
                EventPlan.company_status == "looking",
            )
            .limit(1)
        )
        if demo_plan is None:
            return False
        interest = await self.session.scalar(
            select(CompanionInterest).where(
                CompanionInterest.sender_plan_id == demo_plan.id,
                CompanionInterest.recipient_plan_id == user_plan.id,
            )
        )
        if interest is None:
            self.session.add(
                CompanionInterest(
                    sender_plan_id=demo_plan.id,
                    recipient_plan_id=user_plan.id,
                    event_id=user_plan.event_id,
                )
            )
        else:
            interest.status = "active"
        return True
