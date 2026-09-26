"""Voluntary contact exchange after a match: the user types a contact, confirms, the bot delivers it.

The contact is always typed by hand. MAX contact cards (request_contact) are not
used, so a phone number is never passed on without the user writing it.
"""
from __future__ import annotations

import logging
from uuid import UUID

from maxapi import Bot, Dispatcher, F
from maxapi.types import ButtonsPayload, CallbackButton, MessageCallback, MessageCreated
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infrastructure.db.repositories import ContactRepository, OnboardingError, OnboardingRepository
from infrastructure.db.repositories.contacts import ContactPeer, SentContact
from infrastructure.db.repositories.demo import DEMO_CONTACT_TEXT, DEMO_MAX_USER_IDS
from .navigation import menu, menu_rows, report_error


LOGGER = logging.getLogger(__name__)


def _share_button(match_id: UUID, text: str = "Поделиться контактом") -> list[CallbackButton]:
    return [CallbackButton(text=text, payload=f"contact:share:{match_id}")]


def request_text(peer: ContactPeer) -> str:
    return (
        f"Напиши контакт, которым готов поделиться с {peer.peer_name}: ссылку-приглашение MAX, @ник или телефон. "
        "Бот передаст его дословно и не проверяет."
    )


def request_attachments(match_id: UUID) -> list:
    return [ButtonsPayload(buttons=[
        [CallbackButton(text="Отмена", payload=f"contact:cancel:{match_id}")],
    ]).pack()]


def confirm_attachments(match_id: UUID) -> list:
    return [ButtonsPayload(buttons=[
        [CallbackButton(text="Да, отправить", payload=f"contact:confirm:{match_id}")],
        [CallbackButton(text="Изменить", payload=f"contact:edit:{match_id}"),
         CallbackButton(text="Отмена", payload=f"contact:cancel:{match_id}")],
    ]).pack()]


def contact_from_message(event: MessageCreated) -> str | None:
    """Only a typed link or contact counts; attached contact cards are ignored."""
    text = (event.message.body.text or "").strip()
    return text or None


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
            saved = await repo.set_contact(user_id, contact_text=contact, contact_attachment=None)
        except OnboardingError as exc:
            await event.message.answer(str(exc), attachments=request_attachments(pending.peer.match_id))
            return True
        await session.commit()
    await event.message.answer(
        f"{saved.peer.peer_name} получит ровно этот текст:\n\n{saved.contact_text}",
        attachments=confirm_attachments(saved.peer.match_id),
    )
    return True


async def _deliver(bot: Bot, sent: SentContact) -> None:
    peer = sent.peer
    rows = [] if sent.peer_has_shared else [_share_button(peer.match_id, "Поделиться своим контактом")]
    await bot.send_message(
        user_id=peer.peer_max_user_id,
        text=f"{peer.sender_name} делится контактом по событию «{peer.event_title}»:\n\n{sent.contact_text}",
        attachments=[ButtonsPayload(buttons=rows + menu_rows()).pack()],
    )


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

            if action in {"share", "edit"}:
                async with session_factory() as session:
                    peer = await ContactRepository(session).start(user.id, match_id)
                    await session.commit()
                await event.ack("Пришли контакт следующим сообщением")
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
                    text += "\n\nЭто демо-анкета, в ответ она делится своим демо-контактом."
                elif not sent.peer_has_shared:
                    text += f"\n\nЕсли {sent.peer.peer_name} тоже поделится контактом, мы пришлём его сюда."
                await event.edit(text, attachments=menu(), notify=False)
                if demo_peer:
                    # Show the receiving side of the exchange as well.
                    await event.bot.send_message(
                        user_id=event.callback.user.user_id,
                        text=f"{sent.peer.peer_name} делится контактом по событию «{sent.peer.event_title}»:\n\n{DEMO_CONTACT_TEXT}",
                        attachments=menu(),
                    )
        except (OnboardingError, ValueError) as exc:
            if not answered:
                await event.ack(str(exc))
        except Exception:
            LOGGER.exception("Contact callback %r failed", event.callback.payload)
            await report_error(event, event.callback.payload, answered=answered)
