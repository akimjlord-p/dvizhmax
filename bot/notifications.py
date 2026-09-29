"""Outgoing social notifications sent by the running MAX bot."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

from maxapi import Bot
from maxapi.types import ButtonsPayload, CallbackButton
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infrastructure.db.repositories.demo import DEMO_CONTACT_TEXT, DEMO_MAX_USER_IDS
from infrastructure.db.repositories.notifications import InterestDigest, MatchRecipients, NotificationRepository
from .navigation import menu_rows


LOGGER = logging.getLogger(__name__)
INTEREST_DIGEST_INTERVAL_SECONDS = 60
# A match message that failed is retried by the same loop, up to 10 times.
MATCH_NOTIFY_MAX_ATTEMPTS = 10
MATCH_NOTIFY_RETRY_AFTER = timedelta(seconds=30)
def _people(count: int) -> str:
    """Russian agreement: 1 человек хочет, 2 человека хотят, 5 человек хотят."""
    if count % 10 == 1 and count % 100 != 11:
        return f"хочет пойти {count} человек"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"хотят пойти {count} человека"
    return f"хотят пойти {count} человек"


def match_message(*, event_title: str, peer_name: str, demo_contact: str | None = None) -> str:
    text = (
        "Есть мэтч 🎉\n\n"
        f"Вы оба хотите пойти на «{event_title}» вместе.\n\n"
        f"Твоя компания: {peer_name}"
    )
    if demo_contact:
        text += f"\n\nДемо-анкета делится контактом бота:\n{demo_contact}"
    return text


def match_attachments(match_id: UUID) -> list:
    return [ButtonsPayload(buttons=[
        [CallbackButton(text="Поделиться контактом", payload=f"contact:share:{match_id}")],
        [CallbackButton(text="Пока не сейчас", payload=f"contact:later:{match_id}")],
    ] + menu_rows()).pack()]


def interest_digest_message(digest: InterestDigest) -> str:
    return (
        f"На «{digest.event_title}» с тобой {_people(len(digest.interest_ids))}. "
        "Посмотри анкеты и реши, с кем хочешь пойти."
    )


def interest_digest_attachments(digest: InterestDigest) -> list:
    rows = menu_rows()
    if digest.recipient_plan_id is not None:
        rows = [[CallbackButton(text="Посмотреть анкеты", payload=f"feed:likers:{digest.recipient_plan_id}")]] + rows
    return [ButtonsPayload(buttons=rows).pack()]


async def send_match_notifications(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    match_id: UUID,
) -> None:
    """Send "Есть мэтч" to every real participant who has not received it yet."""
    async with session_factory() as session:
        repo = NotificationRepository(session)
        recipients = await repo.match_recipients(match_id)
        if recipients is None:
            return
        await repo.start_match_delivery(match_id)
        await session.commit()
    first, second = await _send_match_messages(bot, recipients, match_id)
    async with session_factory() as session:
        await NotificationRepository(session).record_match_delivery(match_id, first=first, second=second)
        await session.commit()


async def _send_match_messages(bot: Bot, recipients: MatchRecipients, match_id: UUID) -> tuple[bool, bool]:
    """Return whether each side now has the message; demo profiles count as delivered."""
    sides = (
        (
            recipients.first_notified,
            recipients.first_max_user_id,
            recipients.second_name,
            recipients.second_max_user_id,
        ),
        (
            recipients.second_notified,
            recipients.second_max_user_id,
            recipients.first_name,
            recipients.first_max_user_id,
        ),
    )
    delivered = []
    for already_sent, user_id, peer_name, peer_max_user_id in sides:
        if already_sent or user_id in DEMO_MAX_USER_IDS:
            delivered.append(True)
            continue
        try:
            await bot.send_message(
                user_id=user_id,
                text=match_message(
                    event_title=recipients.event_title,
                    peer_name=peer_name,
                    demo_contact=DEMO_CONTACT_TEXT if peer_max_user_id in DEMO_MAX_USER_IDS else None,
                ),
                attachments=match_attachments(match_id),
            )
            delivered.append(True)
        except Exception as exc:
            LOGGER.warning("Could not send match notification to MAX user %s: %s", user_id, exc)
            delivered.append(False)
    return delivered[0], delivered[1]


async def retry_match_notifications_once(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        match_ids = await NotificationRepository(session).undelivered_match_ids(
            max_attempts=MATCH_NOTIFY_MAX_ATTEMPTS,
            retry_after=MATCH_NOTIFY_RETRY_AFTER,
        )
    for match_id in match_ids:
        await send_match_notifications(bot, session_factory, match_id)


async def dispatch_interest_digests_once(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        digests = await NotificationRepository(session).unannounced_interest_digests()

    for digest in digests:
        try:
            await bot.send_message(
                user_id=digest.recipient_max_user_id,
                text=interest_digest_message(digest),
                attachments=interest_digest_attachments(digest),
            )
        except Exception:
            LOGGER.exception("Could not send interest digest to MAX user %s", digest.recipient_max_user_id)
            continue
        async with session_factory() as session:
            await NotificationRepository(session).mark_interests_announced(digest.interest_ids)
            await session.commit()


async def run_interest_digest_loop(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: int = INTEREST_DIGEST_INTERVAL_SECONDS,
) -> None:
    while True:
        for job in (retry_match_notifications_once, dispatch_interest_digests_once):
            try:
                await job(bot, session_factory)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("%s failed", job.__name__)
        await asyncio.sleep(interval_seconds)
