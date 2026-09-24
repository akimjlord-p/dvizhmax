import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from bot.notifications import DEMO_PROFILE_URL, _send_match_messages, interest_digest_attachments, interest_digest_message, match_message
from infrastructure.db.repositories.demo import DEMO_MAX_USER_ID
from infrastructure.db.repositories.notifications import InterestDigest, MatchRecipients


class NotificationTextTests(unittest.TestCase):
    def test_match_message_contains_max_profile_link(self):
        text = match_message(event_title="Концерт [live]", peer_name="Ира (Ирина)", peer_max_user_id=42)

        self.assertIn("max://user/42", text)
        self.assertIn("Концерт \\[live\\]", text)

    def test_demo_match_message_contains_a_working_https_link(self):
        text = match_message(event_title="Демо", peer_name="Демо Катя", peer_max_user_id=DEMO_MAX_USER_ID)

        self.assertIn(DEMO_PROFILE_URL, text)

    def test_interest_digest_contains_aggregate_count(self):
        digest = InterestDigest(100, "Выставка", (uuid4(), uuid4(), uuid4()))

        self.assertIn("3 человека хотят", interest_digest_message(digest))

    def test_interest_digest_opens_the_likers_of_the_plan(self):
        plan_id = uuid4()
        digest = InterestDigest(100, "Выставка", (uuid4(),), recipient_plan_id=plan_id)

        [keyboard] = interest_digest_attachments(digest)
        first_button = keyboard.payload.buttons[0][0]
        self.assertEqual(first_button.text, "Посмотреть")
        self.assertEqual(first_button.payload, f"feed:likers:{plan_id}")


class MatchNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_message_has_the_navigation_menu(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        await _send_match_messages(bot, MatchRecipients(
            event_title="Концерт",
            first_max_user_id=100,
            first_name="Аня",
            second_max_user_id=200,
            second_name="Борис",
        ))

        self.assertEqual(bot.send_message.await_count, 2)
        for call in bot.send_message.await_args_list:
            buttons = call.kwargs["attachments"][0].payload.buttons
            payloads = [button.payload for row in buttons for button in row]
            self.assertIn("feed:browse:feed|0", payloads)
            self.assertIn("feed:browse:plans|0", payloads)
