"""Use the actual maxapi matcher: a return inside a catch-all is not a filter."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from maxapi import Dispatcher
from maxapi.types import MessageCallback, MessageCreated

from bot.feed import register_feed_handlers
from bot.onboarding import register_onboarding_handlers


def message(text, max_id=1):
    return MessageCreated.model_validate({
        "update_type": "message_created", "timestamp": 0,
        "message": {"sender": sender(max_id), "recipient": {"chat_id": max_id, "chat_type": "dialog"},
                    "timestamp": 0, "body": {"mid": "m1", "seq": 1, "text": text}},
    })


def sender(max_id=1):
    return {"user_id": max_id, "first_name": "Test", "is_bot": False, "last_activity_time": 0}


def callback(payload, max_id=1):
    return MessageCallback.model_validate({
        "update_type": "message_callback", "timestamp": 0,
        "callback": {"timestamp": 0, "callback_id": "c1", "user": sender(max_id), "payload": payload},
    })


async def select_handler(dispatcher, event):
    event.bot = SimpleNamespace(me=None)
    execute = AsyncMock()
    dispatcher._execute_handler = execute
    handlers = dispatcher._find_matching_handlers(router=dispatcher, event_type=event.update_type)
    await dispatcher._run_router_handlers(
        router=dispatcher, event=event, data={}, matching_handlers=handlers,
        memory_context=None, current_state=None, router_id=1, process_info="test",
    )
    return execute.call_args.kwargs["handler"].func_event if execute.called else None


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_callbacks_and_photo_reach_their_handlers(self):
        dispatcher = Dispatcher()
        register_onboarding_handlers(dispatcher, None)
        register_feed_handlers(dispatcher, None)
        cases = [
            (message("/feed"), "on_feed"), (message("/liked"), "on_liked"),
            (message("/plans"), "on_plans"), (message("/start"), "on_start"),
            (message("/profile"), "on_profile"), (message("Alice"), "on_message"),
            (message(None), "on_message"),
            (callback("feed:like:123"), "on_feed_callback"),
            (callback("onboarding:profile:create"), "on_callback"),
            (callback("other:payload"), None), (callback(None), None),
        ]
        for event, expected in cases:
            with self.subTest(expected=expected, event=event.update_type):
                handler = await select_handler(dispatcher, event)
                self.assertEqual(handler.__name__ if handler else None, expected)
