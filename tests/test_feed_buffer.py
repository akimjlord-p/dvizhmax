from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from uuid import uuid4

from infrastructure.cache.feed_buffer import FeedBufferStore


class FeedBufferStoreTests(IsolatedAsyncioTestCase):
    async def test_queue_does_not_repeat_prepared_or_shown_events(self) -> None:
        store = FeedBufferStore()
        user_id = uuid4()
        event_ids = [uuid4() for _ in range(3)]
        fourth_event_id = uuid4()

        await store.append(user_id, event_ids)
        self.assertEqual(await store.pop(user_id), event_ids[0])
        await store.append(user_id, [event_ids[0], event_ids[1], fourth_event_id])

        self.assertEqual(await store.pending_count(user_id), 3)
        self.assertEqual(
            await store.excluded_ids(user_id),
            set(event_ids) | {fourth_event_id},
        )

    async def test_only_one_refill_can_run_for_a_user(self) -> None:
        store = FeedBufferStore()
        user_id = uuid4()

        self.assertTrue(await store.acquire_refill_lock(user_id))
        self.assertFalse(await store.acquire_refill_lock(user_id))
        await store.release_refill_lock(user_id)
        self.assertTrue(await store.acquire_refill_lock(user_id))
        await store.release_refill_lock(user_id)
