"""Webhook process for the ДвижМАКС MAX bot."""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from maxapi import Bot, Dispatcher
from maxapi.enums import UpdateType

from bot.feed import register_feed_handlers
from bot.onboarding import register_onboarding_handlers
from infrastructure.db.session import create_async_database_engine, create_session_factory


async def _register_webhook(bot: Bot) -> None:
    url = os.getenv("MAX_WEBHOOK_URL", "").strip()
    if not url:
        return
    secret = os.getenv("MAX_WEBHOOK_SECRET", "").strip() or None
    subscriptions = await bot.get_subscriptions()
    if not any(subscription.url == url for subscription in subscriptions.subscriptions or []):
        await bot.subscribe_webhook(
            url,
            update_types=[UpdateType.BOT_STARTED, UpdateType.MESSAGE_CREATED, UpdateType.MESSAGE_CALLBACK],
            secret=secret,
        )


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot()
    dispatcher = Dispatcher()
    engine = create_async_database_engine()
    session_factory = create_session_factory(engine)
    register_onboarding_handlers(dispatcher, session_factory)
    register_feed_handlers(dispatcher, session_factory)
    try:
        try:
            await _register_webhook(bot)
        except Exception:
            logging.getLogger(__name__).exception("Unable to register MAX webhook")
        await dispatcher.handle_webhook(
            bot,
            host=os.getenv("MAX_WEBHOOK_HOST", "0.0.0.0"),
            port=int(os.getenv("MAX_WEBHOOK_PORT", "8080")),
            path=os.getenv("MAX_WEBHOOK_PATH", "/max/webhook"),
            secret=os.getenv("MAX_WEBHOOK_SECRET", "").strip() or None,
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
