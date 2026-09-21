"""PostgreSQL integration checks for duplicate and concurrent MAX callbacks."""
from __future__ import annotations

import asyncio
import os
import unittest
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from infrastructure.db.models import City, Event, EventSource, EventTag, Tag
from infrastructure.db.repositories import CompanionRepository, FeedRepository
from infrastructure.db.social_models import (
    CompanionInterest,
    CompanionView,
    EventPlan,
    EventReaction,
    Match,
    User,
    UserTagWeight,
)


TEST_URL = os.getenv("TEST_DATABASE_URL")


@dataclass(frozen=True, slots=True)
class Fixture:
    city_id: UUID
    event_id: UUID
    tag_id: UUID
    first_user_id: UUID
    second_user_id: UUID


@unittest.skipUnless(TEST_URL, "Set TEST_DATABASE_URL to an isolated PostgreSQL *_test database")
class CallbackConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not make_url(TEST_URL).database.endswith("_test"):
            raise RuntimeError("Use a dedicated database ending in _test")
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with patch.dict(os.environ, {"DATABASE_URL": TEST_URL}):
            command.upgrade(config, "head")

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(TEST_URL)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.fixtures: list[Fixture] = []

    async def asyncTearDown(self) -> None:
        for fixture in reversed(self.fixtures):
            await self._delete_fixture(fixture)
        await self.engine.dispose()

    async def _fixture(self) -> Fixture:
        fixture = Fixture(
            city_id=uuid4(),
            event_id=uuid4(),
            tag_id=uuid4(),
            first_user_id=uuid4(),
            second_user_id=uuid4(),
        )
        suffix = uuid4().hex
        async with self.factory() as session:
            session.add_all(
                [
                    City(id=fixture.city_id, name=f"Concurrency {suffix}", timezone="Europe/Moscow"),
                    Tag(
                        id=fixture.tag_id,
                        code=f"concurrency-{suffix}",
                        name="Concurrency",
                        kind="primary",
                        description="Test tag",
                        is_active=True,
                    ),
                    User(
                        id=fixture.first_user_id,
                        max_user_id=uuid4().int % 8_000_000_000_000_000_000,
                        name="First",
                        gender="female",
                        age=25,
                        city_id=fixture.city_id,
                        profile_status="active",
                        onboarding_step="complete",
                    ),
                    User(
                        id=fixture.second_user_id,
                        max_user_id=uuid4().int % 8_000_000_000_000_000_000,
                        name="Second",
                        gender="male",
                        age=25,
                        city_id=fixture.city_id,
                        profile_status="active",
                        onboarding_step="complete",
                    ),
                    Event(id=fixture.event_id, title="Concurrency event", city_id=fixture.city_id, tagging_status="done"),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    EventSource(
                        event_id=fixture.event_id,
                        source="test",
                        external_id=suffix,
                        source_url="https://example.test/event",
                        raw_payload={},
                        is_primary=True,
                    ),
                    EventTag(event_id=fixture.event_id, tag_id=fixture.tag_id, kind="primary"),
                ]
            )
            await session.commit()
        self.fixtures.append(fixture)
        return fixture

    async def _plans(self, fixture: Fixture) -> tuple[UUID, UUID]:
        async with self.factory() as session:
            feed = FeedRepository(session)
            first = await feed.want_to_go(fixture.first_user_id, fixture.event_id)
            second = await feed.want_to_go(fixture.second_user_id, fixture.event_id)
            await feed.set_company_search(fixture.first_user_id, first.plan_id, looking=True)
            await feed.set_company_search(fixture.second_user_id, second.plan_id, looking=True)
            await session.commit()
        return first.plan_id, second.plan_id

    async def _delete_fixture(self, fixture: Fixture) -> None:
        async with self.factory() as session:
            user_ids = (fixture.first_user_id, fixture.second_user_id)
            await session.execute(delete(CompanionInterest).where(CompanionInterest.event_id == fixture.event_id))
            await session.execute(delete(CompanionView).where(CompanionView.event_id == fixture.event_id))
            await session.execute(delete(Match).where(Match.event_id == fixture.event_id))
            await session.execute(delete(EventReaction).where(EventReaction.event_id == fixture.event_id))
            await session.execute(delete(EventPlan).where(EventPlan.event_id == fixture.event_id))
            await session.execute(delete(EventTag).where(EventTag.event_id == fixture.event_id))
            await session.execute(delete(EventSource).where(EventSource.event_id == fixture.event_id))
            await session.execute(delete(Event).where(Event.id == fixture.event_id))
            await session.execute(delete(UserTagWeight).where(UserTagWeight.user_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.execute(delete(Tag).where(Tag.id == fixture.tag_id))
            await session.execute(delete(City).where(City.id == fixture.city_id))
            await session.commit()

    async def test_duplicate_reaction_applies_weights_once(self) -> None:
        fixture = await self._fixture()

        async def record() -> bool:
            async with self.factory() as session:
                result = await FeedRepository(session).record_reaction(
                    fixture.first_user_id,
                    fixture.event_id,
                    "like",
                )
                await session.commit()
                return result

        self.assertEqual(sorted(await asyncio.gather(record(), record())), [False, True])
        async with self.factory() as session:
            reactions = list(
                (
                    await session.scalars(
                        select(EventReaction).where(
                            EventReaction.user_id == fixture.first_user_id,
                            EventReaction.event_id == fixture.event_id,
                        )
                    )
                ).all()
            )
            weight = await session.get(UserTagWeight, (fixture.first_user_id, fixture.tag_id))
        self.assertEqual(len(reactions), 1)
        self.assertEqual(weight.reaction_weight, Decimal("0.1"))

    async def test_parallel_candidate_requests_claim_one_view(self) -> None:
        fixture = await self._fixture()
        first_plan_id, _ = await self._plans(fixture)

        async def next_card():
            async with self.factory() as session:
                result = await CompanionRepository(session).next_candidate(fixture.first_user_id, first_plan_id)
                await session.commit()
                return result

        first, second = await asyncio.gather(next_card(), next_card())
        self.assertEqual(sum(card is not None for card in (first, second)), 1)
        async with self.factory() as session:
            views = list(
                (
                    await session.scalars(
                        select(CompanionView).where(
                            CompanionView.viewer_id == fixture.first_user_id,
                            CompanionView.event_id == fixture.event_id,
                        )
                    )
                ).all()
            )
        self.assertEqual(len(views), 1)

    async def test_parallel_mutual_likes_create_one_match(self) -> None:
        fixture = await self._fixture()
        first_plan_id, second_plan_id = await self._plans(fixture)
        async with self.factory() as session:
            session.add_all(
                [
                    CompanionView(
                        viewer_id=fixture.first_user_id,
                        event_id=fixture.event_id,
                        shown_user_id=fixture.second_user_id,
                    ),
                    CompanionView(
                        viewer_id=fixture.second_user_id,
                        event_id=fixture.event_id,
                        shown_user_id=fixture.first_user_id,
                    ),
                ]
            )
            await session.commit()

        async def react(user_id: UUID, candidate_plan_id: UUID):
            async with self.factory() as session:
                result = await CompanionRepository(session).react(user_id, candidate_plan_id, liked=True)
                await session.commit()
                return result

        first, second = await asyncio.gather(
            react(fixture.first_user_id, second_plan_id),
            react(fixture.second_user_id, first_plan_id),
        )
        self.assertEqual(sum(result.created_match for result in (first, second)), 1)
        async with self.factory() as session:
            matches = list((await session.scalars(select(Match).where(Match.event_id == fixture.event_id))).all())
            interests = list(
                (
                    await session.scalars(
                        select(CompanionInterest).where(CompanionInterest.event_id == fixture.event_id)
                    )
                ).all()
            )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].status, "active")
        self.assertEqual(len(interests), 2)
        self.assertEqual({interest.status for interest in interests}, {"active"})
