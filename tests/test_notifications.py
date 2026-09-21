import unittest
from uuid import uuid4

from bot.notifications import interest_digest_message, match_message
from infrastructure.db.repositories.notifications import InterestDigest


class NotificationTextTests(unittest.TestCase):
    def test_match_message_contains_max_profile_link(self):
        text = match_message(event_title="Концерт [live]", peer_name="Ира (Ирина)", peer_max_user_id=42)

        self.assertIn("max://user/42", text)
        self.assertIn("Концерт \\[live\\]", text)

    def test_interest_digest_contains_aggregate_count(self):
        digest = InterestDigest(100, "Выставка", (uuid4(), uuid4(), uuid4()))

        self.assertIn("3 человека хотят", interest_digest_message(digest))
