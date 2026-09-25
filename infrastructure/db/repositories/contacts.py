"""Voluntary contact exchange between the two participants of a match."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Event
from ..social_models import Match, MatchContact, User
from .onboarding import OnboardingError


# A forgotten share request must not swallow unrelated messages forever.
PENDING_CONTACT_TTL = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class ContactPeer:
    match_id: UUID
    event_title: str
    sender_name: str
    peer_name: str
    peer_max_user_id: int


@dataclass(frozen=True, slots=True)
class PendingContact:
    peer: ContactPeer
    status: str
    contact_text: str | None
    contact_attachment: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class SentContact:
    peer: ContactPeer
    contact_text: str
    contact_attachment: dict[str, Any] | None
    peer_has_shared: bool


class ContactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def start(self, user_id: UUID, match_id: UUID) -> ContactPeer:
        """Wait for this user's contact for one match; earlier requests are cancelled."""
        peer = await self._peer(user_id, match_id)
        row = await self._row(match_id, user_id, for_update=True)
        if row is not None and row.status == "sent":
            raise OnboardingError("Контакт по этому мэтчу уже отправлен")
        await self.session.execute(
            update(MatchContact)
            .where(
                MatchContact.sender_id == user_id,
                MatchContact.status.in_(("awaiting", "confirming")),
                MatchContact.match_id != match_id,
            )
            .values(status="cancelled")
        )
        if row is None:
            row = MatchContact(match_id=match_id, sender_id=user_id)
            self.session.add(row)
        row.status = "awaiting"
        row.contact_text = None
        row.contact_attachment = None
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return peer

    async def pending(self, user_id: UUID) -> PendingContact | None:
        row = await self.session.scalar(
            select(MatchContact).where(
                MatchContact.sender_id == user_id,
                MatchContact.status.in_(("awaiting", "confirming")),
                MatchContact.updated_at >= datetime.now(timezone.utc) - PENDING_CONTACT_TTL,
            )
        )
        if row is None:
            return None
        try:
            peer = await self._peer(user_id, row.match_id)
        except OnboardingError:
            row.status = "cancelled"
            return None
        return PendingContact(peer, row.status, row.contact_text, row.contact_attachment)

    async def set_contact(
        self,
        user_id: UUID,
        *,
        contact_text: str,
        contact_attachment: dict[str, Any] | None,
    ) -> PendingContact:
        contact_text = contact_text.strip()
        if not 1 <= len(contact_text) <= 500:
            raise OnboardingError("Контакт должен быть от 1 до 500 символов")
        pending = await self.pending(user_id)
        if pending is None:
            raise OnboardingError("Сначала нажми «Поделиться контактом» в сообщении о мэтче")
        row = await self._row(pending.peer.match_id, user_id, for_update=True)
        row.status = "confirming"
        row.contact_text = contact_text
        row.contact_attachment = contact_attachment
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return PendingContact(pending.peer, row.status, contact_text, contact_attachment)

    async def confirm(self, user_id: UUID, match_id: UUID) -> SentContact | None:
        """Mark the contact sent once; ``None`` means it was already sent."""
        peer = await self._peer(user_id, match_id)
        row = await self._row(match_id, user_id, for_update=True)
        if row is not None and row.status == "sent":
            return None
        if row is None or row.status != "confirming" or not row.contact_text:
            raise OnboardingError("Контакт не найден. Нажми «Поделиться контактом» ещё раз")
        row.status = "sent"
        row.sent_at = datetime.now(timezone.utc)
        await self.session.flush()
        peer_row = await self.session.scalar(
            select(MatchContact).where(
                MatchContact.match_id == match_id,
                MatchContact.sender_id != user_id,
                MatchContact.status == "sent",
            )
        )
        return SentContact(peer, row.contact_text, row.contact_attachment, peer_has_shared=peer_row is not None)

    async def cancel(self, user_id: UUID, match_id: UUID) -> None:
        row = await self._row(match_id, user_id, for_update=True)
        if row is not None and row.status in {"awaiting", "confirming"}:
            row.status = "cancelled"

    async def has_sent(self, user_id: UUID, match_id: UUID) -> bool:
        row = await self._row(match_id, user_id)
        return row is not None and row.status == "sent"

    async def _row(self, match_id: UUID, user_id: UUID, *, for_update: bool = False) -> MatchContact | None:
        query = select(MatchContact).where(MatchContact.match_id == match_id, MatchContact.sender_id == user_id)
        if for_update:
            query = query.with_for_update()
        return await self.session.scalar(query)

    async def _peer(self, user_id: UUID, match_id: UUID) -> ContactPeer:
        match = await self.session.get(Match, match_id)
        if match is None or user_id not in {match.first_user_id, match.second_user_id}:
            raise OnboardingError("Мэтч не найден")
        if match.status != "active":
            raise OnboardingError("Этот мэтч больше не активен: кто-то отменил поход")
        peer_id = match.second_user_id if match.first_user_id == user_id else match.first_user_id
        sender, peer = await self.session.get(User, user_id), await self.session.get(User, peer_id)
        event = await self.session.get(Event, match.event_id)
        return ContactPeer(
            match_id=match.id,
            event_title=event.title if event else "событие",
            sender_name=sender.name or "Пользователь",
            peer_name=peer.name or "Пользователь",
            peer_max_user_id=peer.max_user_id,
        )
