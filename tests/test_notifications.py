import unittest
from uuid import uuid4

from bot.notifications import DEMO_PROFILE_URL, interest_digest_message, match_message
from infrastructure.db.repositories.demo import DEMO_MAX_USER_ID
from infrastructure.db.repositories.notifications import InterestDigest


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
