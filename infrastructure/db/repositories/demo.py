"""Small, explicit seed used to demonstrate the social MVP flow."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..social_models import CompanionInterest, EventPlan, User


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
