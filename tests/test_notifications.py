import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from bot.notifications import _people, _send_match_messages, interest_digest_attachments, interest_digest_message, match_message
from infrastructure.db.repositories.demo import DEMO_MAX_USER_ID
from infrastructure.db.repositories.notifications import InterestDigest, MatchRecipients


def payloads(attachments):
    return [button.payload for item in attachments for row in item.payload.buttons for button in row]


class NotificationTextTests(unittest.TestCase):
    def test_match_message_names_the_event_and_the_peer_without_a_profile_link(self):
        text = match_message(event_title="Концерт [live]", peer_name="Ира")

        self.assertIn("Есть мэтч 🎉", text)
        self.assertIn("«Концерт [live]»", text)
        self.assertIn("Ира", text)
        self.assertNotIn("max://", text)

    def test_interest_digest_uses_russian_plural_forms(self):
        self.assertEqual(
            [_people(count) for count in (1, 2, 5, 11, 12, 21, 22)],
            ["хочет пойти 1 человек", "хотят пойти 2 человека", "хотят пойти 5 человек",
             "хотят пойти 11 человек", "хотят пойти 12 человек", "хочет пойти 21 человек",
             "хотят пойти 22 человека"],
        )
        digest = InterestDigest(100, "Выставка", (uuid4(), uuid4(), uuid4()))
        self.assertIn("На «Выставка» с тобой хотят пойти 3 человека", interest_digest_message(digest))

    def test_interest_digest_opens_the_likers_of_the_plan(self):
        plan_id = uuid4()
        digest = InterestDigest(100, "Выставка", (uuid4(),), recipient_plan_id=plan_id)

        [keyboard] = interest_digest_attachments(digest)
        first_button = keyboard.payload.buttons[0][0]
        self.assertEqual(first_button.text, "Посмотреть анкеты")
        self.assertEqual(first_button.payload, f"feed:likers:{plan_id}")
        self.assertIn("feed:browse:plans|0", payloads([keyboard]))


class MatchNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_message_offers_contact_sharing_and_the_menu(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        match_id = uuid4()
        await _send_match_messages(bot, MatchRecipients(
            event_title="Концерт",
            first_max_user_id=100,
            first_name="Аня",
            second_max_user_id=200,
            second_name="Борис",
        ), match_id)

        self.assertEqual(bot.send_message.await_count, 2)
        for call in bot.send_message.await_args_list:
            buttons = payloads(call.kwargs["attachments"])
            self.assertEqual(buttons[:2], [f"contact:share:{match_id}", f"contact:later:{match_id}"])
            self.assertIn("feed:browse:feed|0", buttons)
            self.assertIn("feed:browse:plans|0", buttons)

    async def test_demo_profile_gets_no_match_message(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        await _send_match_messages(bot, MatchRecipients(
            event_title="Демо",
            first_max_user_id=100,
            first_name="Аня",
            second_max_user_id=DEMO_MAX_USER_ID,
            second_name="Демо Катя",
        ), uuid4())

        self.assertEqual([call.kwargs["user_id"] for call in bot.send_message.await_args_list], [100])
