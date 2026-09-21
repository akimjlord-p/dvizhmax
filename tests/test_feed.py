from decimal import Decimal
from uuid import uuid4
import unittest

from bot.feed import card_text
from infrastructure.db.repositories.feed import EventCard, rank_cards, score_card


def card(*, score: str, primary: str, tags: tuple[tuple[str, str], ...] | None = None) -> EventCard:
    tagged = tags or ((primary, "primary"),)
    return EventCard(
        id=uuid4(),
        title="Event",
        description=None,
        city_timezone="Europe/Moscow",
        place_name=None,
        place_address=None,
        price_text=None,
        is_free=None,
        data_status="current",
        source_url="https://example.test/event",
        starts_at=None,
        image_url=None,
        primary_codes=frozenset({primary}),
        tag_codes=frozenset(code for code, _ in tagged),
        tag_kinds=tagged,
        score=Decimal(score),
    )


class FeedRecommendationTests(unittest.TestCase):
    def test_primary_weight_is_more_important_than_secondary(self):
        event = card(
            score="0",
            primary="lecture",
            tags=(("lecture", "primary"), ("calm", "secondary")),
        )
        self.assertEqual(
            score_card(event, {"lecture": Decimal("1"), "calm": Decimal("1")}),
            Decimal("1.4"),
        )

    def test_ranking_does_not_place_three_same_primary_tags_in_a_row(self):
        ranked = rank_cards(
            [
                card(score="5", primary="concert"),
                card(score="4", primary="concert"),
                card(score="3", primary="concert"),
                card(score="2", primary="lecture"),
            ],
            weights={},
            reaction_count=0,
            prior_primary_tags=[],
        )
        self.assertNotEqual(
            ranked[0].primary_codes & ranked[1].primary_codes & ranked[2].primary_codes,
            frozenset({"concert"}),
        )

    def test_card_text_contains_event_details(self):
        result = card_text(card(score="0", primary="concert"))
        self.assertIn("Event", result)
        self.assertIn("https://example.test/event", result)

if __name__ == "__main__":
    unittest.main()
