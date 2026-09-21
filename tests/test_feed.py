from datetime import datetime, time, timezone
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import unittest

from maxapi.types import LinkButton
from maxapi.types.attachments.attachment import Attachment

import bot.feed as feed_module
from bot.feed import _buttons, _cached_image_attachment, _event_image_attachment, card_text
from infrastructure.db.repositories.feed import EventCard, event_timing, rank_cards, score_card


def card(
    *,
    score: str,
    primary: str,
    tags: tuple[tuple[str, str], ...] | None = None,
    description: str | None = None,
    image_url: str | None = None,
) -> EventCard:
    tagged = tags or ((primary, "primary"),)
    return EventCard(
        id=uuid4(),
        title="Event",
        description=description,
        city_timezone="Europe/Moscow",
        place_name=None,
        place_address=None,
        price_text=None,
        is_free=None,
        data_status="current",
        source_url="https://example.test/event",
        starts_at=None,
        ends_at=None,
        schedule_state="unknown",
        image_url=image_url,
        image_id=None,
        max_attachment=None,
        primary_codes=frozenset({primary}),
        tag_codes=frozenset(code for code, _ in tagged),
        tag_kinds=tagged,
        score=Decimal(score),
    )


class FeedRecommendationTests(unittest.IsolatedAsyncioTestCase):
    def test_old_event_without_end_is_not_current(self):
        schedule = SimpleNamespace(
            starts_at=datetime(2014, 8, 9, tzinfo=timezone.utc),
            ends_at=None,
            is_endless=False,
            is_startless=False,
            recurrence=None,
            start_time=None,
            end_time=None,
        )
        self.assertIsNone(event_timing([schedule], datetime(2026, 9, 22, tzinfo=timezone.utc), "Europe/Moscow"))

    def test_recurring_event_shows_the_next_session(self):
        schedule = SimpleNamespace(
            starts_at=datetime(2014, 8, 8, 20, tzinfo=timezone.utc),
            ends_at=datetime(9999, 12, 31, 21, tzinfo=timezone.utc),
            is_endless=True,
            is_startless=False,
            recurrence=[{"days_of_week": [1], "start_time": "10:00:00", "end_time": "20:00:00"}],
            start_time=time(10),
            end_time=time(20),
        )
        timing = event_timing([schedule], datetime(2026, 9, 22, 9, tzinfo=timezone.utc), "Europe/Moscow")
        self.assertEqual((timing.state, timing.starts_at), ("recurring", datetime(2026, 9, 28, 7, tzinfo=timezone.utc)))

    def test_ongoing_event_does_not_show_its_old_start_date(self):
        event = replace(
            card(score="0", primary="exhibition"),
            starts_at=datetime(2026, 6, 11, 21, tzinfo=timezone.utc),
            ends_at=datetime(2026, 10, 11, 21, tzinfo=timezone.utc),
            schedule_state="ongoing",
        )
        result = card_text(event)
        self.assertIn("Идёт сейчас", result)
        self.assertNotIn("11.06.2026", result)
    def test_cached_max_image_attachment_is_reused(self):
        attachment = _cached_image_attachment({"type": "image", "payload": {"token": "token"}})
        self.assertIsNotNone(attachment)
        self.assertEqual(attachment.payload.token, "token")
        self.assertIsNone(_cached_image_attachment({"type": "file", "payload": {"token": "token"}}))

    async def test_event_without_image_uses_the_placeholder(self):
        event = card(score="0", primary="concert")
        attachment = _cached_image_attachment({"type": "image", "payload": {"token": "placeholder"}})
        bot = SimpleNamespace(upload_media=AsyncMock(return_value=attachment))

        with patch.object(feed_module, "_placeholder_attachment", None):
            result = await _event_image_attachment(event, bot=bot)

        self.assertEqual(result, attachment)
        bot.upload_media.assert_awaited_once()

    async def test_event_image_is_passed_to_max_by_url(self):
        event = card(score="0", primary="concert", image_url="https://example.test/event.jpg")
        bot = SimpleNamespace(upload_media=AsyncMock())

        result = await _event_image_attachment(event, bot=bot)

        self.assertIsInstance(result, Attachment)
        self.assertEqual(result.payload.url, event.image_url)
        bot.upload_media.assert_not_awaited()

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
        self.assertNotIn("https://example.test/event", result)

    def test_card_hides_description_and_has_source_link_button(self):
        event = card(score="0", primary="concert", description="Long <event> description")
        self.assertNotIn("Long <event> description", card_text(event))
        buttons = _buttons(event, mode="feed")[0].payload.buttons
        link = next(button for row in buttons for button in row if isinstance(button, LinkButton))
        self.assertEqual((link.text, link.url), ("Подробнее", event.source_url))

if __name__ == "__main__":
    unittest.main()
