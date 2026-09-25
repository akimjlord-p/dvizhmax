"""Short per-user feed queues backed by Redis when it is configured."""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

try:
    import redis.asyncio as redis
except ImportError:  # pragma: no cover - dependency is installed in production
    redis = None


# Cards shown without a reaction stay excluded from the feed for a day.
BUFFER_TTL_SECONDS = 24 * 60 * 60


@dataclass(slots=True)
class _MemoryState:
    queue: list[UUID] = field(default_factory=list)
    shown: set[UUID] = field(default_factory=set)


class FeedBufferStore:
    """Atomic queue operations for six-card personal feed pages.

    Without REDIS_URL the class uses process memory, which keeps local
    development working but intentionally does not survive a bot restart.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis = redis.from_url(redis_url, decode_responses=True) if redis_url and redis else None
        self._memory: dict[UUID, _MemoryState] = {}
        self._locks: dict[UUID, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._refill_locks: dict[UUID, asyncio.Lock] = defaultdict(asyncio.Lock)

    @classmethod
    def from_env(cls) -> "FeedBufferStore":
        return cls(os.getenv("REDIS_URL", "").strip() or None)

    @staticmethod
    def _key(user_id: UUID, suffix: str) -> str:
        return f"dvizhmax:feed:{user_id}:{suffix}"

    async def pop(self, user_id: UUID) -> UUID | None:
        if self._redis is not None:
            raw = await self._redis.lpop(self._key(user_id, "queue"))
            if raw is None:
                return None
            event_id = UUID(raw)
            await self._redis.sadd(self._key(user_id, "shown"), str(event_id))
            await self._touch(user_id)
            return event_id
        async with self._locks[user_id]:
            state = self._memory.setdefault(user_id, _MemoryState())
            if not state.queue:
                return None
            event_id = state.queue.pop(0)
            state.shown.add(event_id)
            return event_id

    async def requeue(self, user_id: UUID, event_id: UUID) -> None:
        """Return a card that could not be delivered to the front of the queue."""
        if self._redis is not None:
            await asyncio.gather(
                self._redis.lpush(self._key(user_id, "queue"), str(event_id)),
                self._redis.srem(self._key(user_id, "shown"), str(event_id)),
            )
            await self._touch(user_id)
            return
        async with self._locks[user_id]:
            state = self._memory.setdefault(user_id, _MemoryState())
            state.shown.discard(event_id)
            if event_id not in state.queue:
                state.queue.insert(0, event_id)

    async def append(self, user_id: UUID, event_ids: list[UUID]) -> None:
        if not event_ids:
            return
        if self._redis is not None:
            await self._redis.rpush(self._key(user_id, "queue"), *(str(event_id) for event_id in event_ids))
            await self._touch(user_id)
            return
        async with self._locks[user_id]:
            state = self._memory.setdefault(user_id, _MemoryState())
            known = set(state.queue) | state.shown
            state.queue.extend(event_id for event_id in event_ids if event_id not in known)

    async def excluded_ids(self, user_id: UUID) -> set[UUID]:
        if self._redis is not None:
            queue, shown = await asyncio.gather(
                self._redis.lrange(self._key(user_id, "queue"), 0, -1),
                self._redis.smembers(self._key(user_id, "shown")),
            )
            return {UUID(value) for value in (*queue, *shown)}
        async with self._locks[user_id]:
            state = self._memory.setdefault(user_id, _MemoryState())
            return set(state.queue) | state.shown

    async def pending_count(self, user_id: UUID) -> int:
        if self._redis is not None:
            return int(await self._redis.llen(self._key(user_id, "queue")))
        async with self._locks[user_id]:
            return len(self._memory.setdefault(user_id, _MemoryState()).queue)

    async def acquire_refill_lock(self, user_id: UUID) -> bool:
        if self._redis is not None:
            return bool(await self._redis.set(self._key(user_id, "refill"), "1", nx=True, ex=30))
        lock = self._refill_locks[user_id]
        if lock.locked():
            return False
        await lock.acquire()
        return True

    async def release_refill_lock(self, user_id: UUID) -> None:
        if self._redis is not None:
            await self._redis.delete(self._key(user_id, "refill"))
            return
        lock = self._refill_locks[user_id]
        if lock.locked():
            lock.release()

    async def _touch(self, user_id: UUID) -> None:
        if self._redis is not None:
            await asyncio.gather(
                self._redis.expire(self._key(user_id, "queue"), BUFFER_TTL_SECONDS),
                self._redis.expire(self._key(user_id, "shown"), BUFFER_TTL_SECONDS),
            )
