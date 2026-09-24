"""Read and update the small set of notifications sent by the bot process."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .demo import DEMO_MAX_USER_IDS
from ..models import Event
from ..social_models import CompanionInterest, EventPlan, Match, User


@dataclass(frozen=True, slots=True)
class MatchRecipients:
    event_title: str
    first_max_user_id: int
    first_name: str
    second_max_user_id: int
    second_name: str


@dataclass(frozen=True, slots=True)
class InterestDigest:
    recipient_max_user_id: int
    event_title: str
    interest_ids: tuple[UUID, ...]


class NotificationRepository:
    """Queries used by direct match messages and the 15-minute digest."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def match_recipients(self, match_id: UUID) -> MatchRecipients | None:
        first_user = aliased(User)
        second_user = aliased(User)
        row = (
            await self.session.execute(
                select(
                    Event.title,
                    first_user.max_user_id.label("first_max_user_id"),
                    first_user.name.label("first_name"),
                    second_user.max_user_id.label("second_max_user_id"),
                    second_user.name.label("second_name"),
                )
                .select_from(Match)
                .join(Event, Event.id == Match.event_id)
                .join(first_user, first_user.id == Match.first_user_id)
                .join(second_user, second_user.id == Match.second_user_id)
                .where(Match.id == match_id, Match.status == "active")
            )
        ).one_or_none()
        if row is None:
            return None
        return MatchRecipients(
            event_title=row.title,
            first_max_user_id=row.first_max_user_id,
            first_name=row.first_name or "Пользователь",
            second_max_user_id=row.second_max_user_id,
            second_name=row.second_name or "Пользователь",
        )

    async def unannounced_interest_digests(self) -> list[InterestDigest]:
        recipient_plan = aliased(EventPlan)
        sender_plan = aliased(EventPlan)
        recipient = aliased(User)
        sender = aliased(User)
        reverse_interest = aliased(CompanionInterest)
        rows = (
            await self.session.execute(
                select(
                    CompanionInterest.id,
                    recipient.max_user_id,
                    Event.title,
                    recipient_plan.id,
                )
                .join(recipient_plan, recipient_plan.id == CompanionInterest.recipient_plan_id)
                .join(sender_plan, sender_plan.id == CompanionInterest.sender_plan_id)
                .join(recipient, recipient.id == recipient_plan.user_id)
                .join(sender, sender.id == sender_plan.user_id)
                .join(Event, Event.id == CompanionInterest.event_id)
                .where(
                    CompanionInterest.status == "active",
                    CompanionInterest.announced_at.is_(None),
                    recipient.notifications_enabled.is_(True),
                    recipient_plan.status == "planned",
                    recipient_plan.company_status == "looking",
                    sender_plan.status == "planned",
                    sender_plan.company_status == "looking",
                    sender.max_user_id.not_in(DEMO_MAX_USER_IDS),
                    ~exists(
                        select(reverse_interest.id).where(
                            reverse_interest.sender_plan_id == recipient_plan.id,
                            reverse_interest.recipient_plan_id == sender_plan.id,
                            reverse_interest.status == "active",
                        )
                    ),
                )
                .order_by(recipient_plan.id, CompanionInterest.created_at)
            )
        ).all()
        grouped: dict[tuple[int, UUID, str], list[UUID]] = {}
        for interest_id, max_user_id, title, plan_id in rows:
            grouped.setdefault((max_user_id, plan_id, title), []).append(interest_id)
        return [
            InterestDigest(
                recipient_max_user_id=max_user_id,
                event_title=title,
                interest_ids=tuple(interest_ids),
            )
            for (max_user_id, _plan_id, title), interest_ids in grouped.items()
        ]

    async def mark_interests_announced(self, interest_ids: tuple[UUID, ...]) -> None:
        if not interest_ids:
            return
        await self.session.execute(
            update(CompanionInterest)
            .where(
                CompanionInterest.id.in_(interest_ids),
                CompanionInterest.announced_at.is_(None),
            )
            .values(announced_at=datetime.now(timezone.utc))
        )
