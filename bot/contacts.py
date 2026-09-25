"""Voluntary contact exchange after a match: request, confirm, deliver."""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from maxapi import Bot, Dispatcher, F
from maxapi.exceptions.max import MaxApiError
from maxapi.types import ButtonsPayload, CallbackButton, MessageCallback, MessageCreated
from maxapi.types.attachments.attachment import ContactAttachmentPayload
from maxapi.types.attachments.buttons.request_contact import RequestContactButton
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infrastructure.db.repositories import ContactRepository, OnboardingError, OnboardingRepository
from infrastructure.db.repositories.contacts import ContactPeer, SentContact
from infrastructure.db.repositories.demo import DEMO_MAX_USER_IDS
from .navigation import menu, menu_rows, report_error


LOGGER = logging.getLogger(__name__)
SHARE_BUTTON_TEXT = "📱 Отправить мой контакт"


class _OutgoingContact(BaseModel):
    """MAX API shape of a contact attachment sent by a bot."""

    type: str = "contact"
    payload: dict[str, Any]


def _share_button(match_id: UUID, text: str = "Поделиться контактом") -> list[CallbackButton]:
    return [CallbackButton(text=text, payload=f"contact:share:{match_id}")]


def request_text(peer: ContactPeer) -> str:
    return (
        f"Отправь ссылку-приглашение MAX или другой контакт, которым готов поделиться с {peer.peer_name}. "
        "Мы покажем его только после подтверждения.\n\n"
        f"Проще всего — нажми «{SHARE_BUTTON_TEXT}»."
    )


def request_attachments(match_id: UUID) -> list:
    return [ButtonsPayload(buttons=[
        [RequestContactButton(text=SHARE_BUTTON_TEXT)],
        [CallbackButton(text="Отмена", payload=f"contact:cancel:{match_id}")],
    ]).pack()]


def confirm_attachments(match_id: UUID) -> list:
    return [ButtonsPayload(buttons=[
        [CallbackButton(text="Да, отправить", payload=f"contact:confirm:{match_id}")],
        [CallbackButton(text="Отмена", payload=f"contact:cancel:{match_id}")],
    ]).pack()]


def contact_from_message(event: MessageCreated) -> tuple[str, dict[str, Any] | None] | None:
    """Read a shared MAX contact card, or a contact typed as text."""
    for item in event.message.body.attachments or []:
        if str(item.type) != "contact" or not isinstance(item.payload, ContactAttachmentPayload):
            continue
        vcf = item.payload.vcf
        owner = item.payload.max_info
        name = vcf.full_name or (owner.full_name if owner else None)
        lines = [line for line in (name, f"📞 {vcf.phone}" if vcf.phone else None) if line]
        if not lines:
            continue
        outgoing = {"name": name, "vcf_info": item.payload.vcf_info}
        if owner is not None:
            outgoing["contact_id"] = owner.user_id
        return "\n".join(lines), {key: value for key, value in outgoing.items() if value is not None}
    text = (event.message.body.text or "").strip()
    return (text, None) if text else None


async def handle_contact_message(
    event: MessageCreated,
    user_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Consume a message while the user is sharing a contact; ``False`` otherwise."""
    async with session_factory() as session:
        repo = ContactRepository(session)
        pending = await repo.pending(user_id)
        if pending is None:
            await session.commit()
            return False
        contact = contact_from_message(event)
        if contact is None:
            await event.message.answer(request_text(pending.peer), attachments=request_attachments(pending.peer.match_id))
            return True
        try:
            saved = await repo.set_contact(user_id, contact_text=contact[0], contact_attachment=contact[1])
        except OnboardingError as exc:
            await event.message.answer(str(exc), attachments=request_attachments(pending.peer.match_id))
            return True
        await session.commit()
    await event.message.answer(
        f"Отправить этот контакт пользователю {saved.peer.peer_name}?\n\n{saved.contact_text}",
        attachments=confirm_attachments(saved.peer.match_id),
    )
    return True


async def _deliver(bot: Bot, sent: SentContact) -> None:
    peer = sent.peer
    rows = [] if sent.peer_has_shared else [_share_button(peer.match_id, "Поделиться своим контактом")]
    buttons = ButtonsPayload(buttons=rows + menu_rows()).pack()
    text = f"{peer.sender_name} делится контактом по событию «{peer.event_title}»:\n\n{sent.contact_text}"
    if sent.contact_attachment:
        try:
            await bot.send_message(
                user_id=peer.peer_max_user_id,
                text=text,
                attachments=[_OutgoingContact(payload=sent.contact_attachment), buttons],
            )
            return
        except MaxApiError as exc:
            # The text already carries the contact; the card is a convenience.
            LOGGER.warning("MAX rejected a forwarded contact card: %s", exc)
    await bot.send_message(user_id=peer.peer_max_user_id, text=text, attachments=[buttons])


def register_contact_handlers(
    dispatcher: Dispatcher,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    @dispatcher.message_callback(F.callback.payload.startswith("contact:"))
    async def on_contact_callback(event: MessageCallback) -> None:
        parts = (event.callback.payload or "").split(":", maxsplit=2)
        if len(parts) != 3:
            return
        _, action, value = parts
        answered = False
        try:
            match_id = UUID(value)
            async with session_factory() as session:
                user = await OnboardingRepository(session).get_user(event.callback.user.user_id)
            if user is None:
                await event.ack("Сначала пройди /start")
                return

            if action == "share":
                async with session_factory() as session:
                    peer = await ContactRepository(session).start(user.id, match_id)
                    await session.commit()
                await event.ack("Пришли контакт — кнопка в сообщении ниже")
                answered = True
                await event.bot.send_message(
                    user_id=event.callback.user.user_id,
                    text=request_text(peer),
                    attachments=request_attachments(match_id),
                )
            elif action == "later":
                await event.edit(
                    "Контактом можно поделиться позже — кнопка останется здесь.",
                    attachments=[ButtonsPayload(buttons=[_share_button(match_id)] + menu_rows()).pack()],
                    notify=False,
                )
            elif action == "cancel":
                async with session_factory() as session:
                    await ContactRepository(session).cancel(user.id, match_id)
                    await session.commit()
                await event.edit(
                    "Контакт не отправлен. Поделиться им можно в любой момент.",
                    attachments=[ButtonsPayload(buttons=[_share_button(match_id)] + menu_rows()).pack()],
                    notify=False,
                )
            elif action == "confirm":
                async with session_factory() as session:
                    sent = await ContactRepository(session).confirm(user.id, match_id)
                    if sent is None:
                        await event.ack("Контакт уже отправлен")
                        return
                    demo_peer = sent.peer.peer_max_user_id in DEMO_MAX_USER_IDS
                    if not demo_peer:
                        # Commit only after delivery, so a failed send can be retried.
                        await _deliver(event.bot, sent)
                    await session.commit()
                text = f"Контакт отправлен: {sent.peer.peer_name}."
                if demo_peer:
                    text += "\n\nЭто демо-анкета: в настоящем мэтче контакт получил бы второй человек."
                elif not sent.peer_has_shared:
                    text += f"\n\nЕсли {sent.peer.peer_name} тоже поделится контактом, мы пришлём его сюда."
                await event.edit(text, attachments=menu(), notify=False)
        except (OnboardingError, ValueError) as exc:
            if not answered:
                await event.ack(str(exc))
        except Exception:
            LOGGER.exception("Contact callback %r failed", event.callback.payload)
            await report_error(event, event.callback.payload, answered=answered)
