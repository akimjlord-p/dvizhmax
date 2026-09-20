import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
import unittest

import httpx

from integrations.kudago import KudaGoClient, KudaGoHTTPError, normalize_event


EVENT = {
    "id": 123,
    "title": "  Лекция о городе  ",
    "body_text": "Лекция проходит в музее.",
    "description": "ignored when body_text exists",
    "site_url": "https://kudago.com/msk/event/example/",
    "price": "от 1 200 до 2400 рублей",
    "is_free": False,
    "age_restriction": "18+",
    "place": {"id": 77, "title": "Музей", "address": "ул. Тестовая, 1", "coords": {"lat": 55.7, "lon": 37.6}},
    "images": [
        {"image": "https://img.test/a.jpg", "source": {"name": "Author", "link": "https://author.test"}},
        {"image": "https://img.test/a.jpg"},
    ],
    "dates": [{"start": 1790000000, "end": 1790003600, "start_time": "12:00:00", "end_time": "13:00:00"}],
}


class KudaGoTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_event(self):
        draft = normalize_event(EVENT, city_id=uuid4())
        self.assertEqual(draft.external_id, "123")
        self.assertEqual(draft.title, "Лекция о городе")
        self.assertEqual(draft.price_min, Decimal("1200"))
        self.assertEqual(draft.age_min, 18)
        self.assertEqual(draft.place.external_id, "77")
        self.assertEqual(len(draft.images), 1)
        self.assertEqual(draft.schedules[0].start_time.hour, 12)
        self.assertEqual(draft.schedules[0].starts_at.tzinfo, timezone.utc)

    def test_normalize_event_without_place_and_free_price(self):
        payload = {"id": 1, "title": "Бесплатно", "price": "бесплатно", "is_free": True}
        draft = normalize_event(payload, city_id=uuid4())
        self.assertIsNone(draft.place)
        self.assertIsNone(draft.price_min)
        self.assertTrue(draft.is_free)
        self.assertEqual(draft.images, ())

    async def test_iter_events_follows_next(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            if "page=2" in str(request.url):
                return httpx.Response(200, json={"results": [{"id": 2}], "next": None})
            return httpx.Response(200, json={"results": [{"id": 1}], "next": "https://test.kudago/public-api/v1.4/events/?page=2"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            async with KudaGoClient(client=http_client, base_url="https://test.kudago/public-api/v1.4") as client:
                events = [event async for event in client.iter_events(location="msk")]
        self.assertEqual([event["id"] for event in events], [1, 2])
        self.assertEqual(len(requests), 2)

    async def test_rejects_non_kudago_pagination(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"results": [], "next": "https://evil.test/events"}))
        async with httpx.AsyncClient(transport=transport) as http_client:
            async with KudaGoClient(client=http_client, base_url="https://test.kudago/public-api/v1.4") as client:
                with self.assertRaises(Exception):
                    _ = [event async for event in client.iter_events(location="msk")]


if __name__ == "__main__":
    unittest.main()
