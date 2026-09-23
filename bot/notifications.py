"""Outgoing social notifications sent by the running MAX bot."""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from maxapi import Bot
from maxapi.enums import TextFormat
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infrastructure.db.repositories.demo import DEMO_MAX_USER_ID
from infrastructure.db.repositories.notifications import InterestDigest, MatchRecipients, NotificationRepository


LOGGER = logging.getLogger(__name__)
INTEREST_DIGEST_INTERVAL_SECONDS = 15 * 60
DEMO_PROFILE_URL = "https://max.ru/t110_hakaton_max_bot"


def _escape_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("(", "\\(").replace(")", "\\)")


def _profile_link(max_user_id: int) -> str:
    # The demo companion has no real MAX account. Link it to the project bot
    # so the demo still contains a visible, working MAX link.
    return DEMO_PROFILE_URL if max_user_id == DEMO_MAX_USER_ID else f"max://user/{max_user_id}"


def match_message(*, event_title: str, peer_name: str, peer_max_user_id: int) -> str:
    return (
        f"У вас мэтч на событие «{_escape_markdown(event_title)}»!\n\n"
        f"Профиль: [{_escape_markdown(peer_name)}]({_profile_link(peer_max_user_id)})"
    )


def interest_digest_message(digest: InterestDigest) -> str:
    count = len(digest.interest_ids)
    noun = "человек хочет" if count == 1 else "человека хотят" if 2 <= count <= 4 else "человек хотят"
    return (
        f"На событие «{digest.event_title}» с тобой {count} {noun} пойти.\n\n"
        "Открой «Мои планы» и выбери поиск компании."
    )


async def send_match_notifications(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    match_id: UUID,
) -> None:
    async with session_factory() as session:
        recipients = await NotificationRepository(session).match_recipients(match_id)
    if recipients is None:
        return
    await _send_match_messages(bot, recipients)


async def _send_match_messages(bot: Bot, recipients: MatchRecipients) -> None:
    sends = (
        bot.send_message(
            user_id=recipients.first_max_user_id,
            text=match_message(
                event_title=recipients.event_title,
                peer_name=recipients.second_name,
                peer_max_user_id=recipients.second_max_user_id,
            ),
            format=TextFormat.MARKDOWN,
        ),
        bot.send_message(
            user_id=recipients.second_max_user_id,
            text=match_message(
                event_title=recipients.event_title,
                peer_name=recipients.first_name,
                peer_max_user_id=recipients.first_max_user_id,
            ),
            format=TextFormat.MARKDOWN,
        ),
    )
    results = await asyncio.gather(*sends, return_exceptions=True)
    for recipient_max_user_id, result in zip(
        (recipients.first_max_user_id, recipients.second_max_user_id), results, strict=True,
    ):
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
            await bot.send_message(user_id=digest.recipient_max_user_id, text=interest_digest_message(digest))
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
