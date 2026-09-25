"""Outgoing social notifications sent by the running MAX bot."""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from maxapi import Bot
from maxapi.types import ButtonsPayload, CallbackButton
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infrastructure.db.repositories.demo import DEMO_MAX_USER_IDS
from infrastructure.db.repositories.notifications import InterestDigest, MatchRecipients, NotificationRepository
from .navigation import menu_rows


LOGGER = logging.getLogger(__name__)
INTEREST_DIGEST_INTERVAL_SECONDS = 60
def _people(count: int) -> str:
    """Russian agreement: 1 человек хочет, 2 человека хотят, 5 человек хотят."""
    if count % 10 == 1 and count % 100 != 11:
        return f"хочет пойти {count} человек"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"хотят пойти {count} человека"
    return f"хотят пойти {count} человек"


def match_message(*, event_title: str, peer_name: str) -> str:
    return (
        "Есть мэтч 🎉\n\n"
        f"Вы оба хотите пойти на «{event_title}» вместе.\n\n"
        f"Твоя компания: {peer_name}"
    )


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
    async with session_factory() as session:
        recipients = await NotificationRepository(session).match_recipients(match_id)
    if recipients is None:
        return
    await _send_match_messages(bot, recipients, match_id)


async def _send_match_messages(bot: Bot, recipients: MatchRecipients, match_id: UUID) -> None:
    messages = []
    if recipients.first_max_user_id not in DEMO_MAX_USER_IDS:
        messages.append((
            recipients.first_max_user_id,
            match_message(event_title=recipients.event_title, peer_name=recipients.second_name),
        ))
    if recipients.second_max_user_id not in DEMO_MAX_USER_IDS:
        messages.append((
            recipients.second_max_user_id,
            match_message(event_title=recipients.event_title, peer_name=recipients.first_name),
        ))
    results = await asyncio.gather(*(
        bot.send_message(user_id=user_id, text=text, attachments=match_attachments(match_id))
        for user_id, text in messages
    ), return_exceptions=True)
    for (recipient_max_user_id, _), result in zip(messages, results, strict=True):
        if isinstance(result, Exception):
            LOGGER.warning("Could not send match notification to MAX user %s: %s", recipient_max_user_id, result)


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
        try:
            await dispatch_interest_digests_once(bot, session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Interest digest run failed")
        await asyncio.sleep(interval_seconds)
