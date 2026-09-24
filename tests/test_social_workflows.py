"""Optional integration tests; TEST_DATABASE_URL must name a dedicated *_test DB."""
import asyncio
import importlib
import os
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from maxapi import Dispatcher
from maxapi.types import MessageCallback
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.feed import register_feed_handlers
from bot.onboarding import CONSENT_VERSION, register_onboarding_handlers
from infrastructure.db.models import City, Event, EventSource, EventTag, Tag
from infrastructure.db.repositories import CompanionRepository, DemoRepository, FeedRepository, OnboardingRepository, OnboardingError
from infrastructure.db.repositories.demo import DEMO_EVENT_ID, DEMO_MAX_USER_ID
from infrastructure.db.social_models import CompanionInterest, CompanionView, EventPlan, EventReaction, Match, User, UserTagWeight
from test_bot_routing import callback, message, select_handler


TEST_URL = os.getenv("TEST_DATABASE_URL")


def payloads(attachments):
    return [button.payload for item in attachments if str(item.type) == "inline_keyboard"
            for row in item.payload.buttons for button in row]


@unittest.skipUnless(TEST_URL, "Set TEST_DATABASE_URL to an isolated PostgreSQL *_test database")
class SocialWorkflowTests(unittest.IsolatedAsyncioTestCase):
    loop_factory = staticmethod(asyncio.SelectorEventLoop)

    @classmethod
    def setUpClass(cls):
        if not make_url(TEST_URL).database.endswith("_test"):
            raise RuntimeError("Use a dedicated database ending in _test")
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with patch.dict(os.environ, {"DATABASE_URL": TEST_URL}):
            command.upgrade(config, "head")

    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_URL)
        self.connection = await self.engine.connect()
        self.transaction = await self.connection.begin()
        self.factory = async_sessionmaker(self.connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
        self.city = City(id=uuid4(), name="Москва", timezone="Europe/Moscow")
        self.user = User(id=uuid4(), max_user_id=100, name="Alice", gender="female", age=25,
                         city_id=self.city.id, profile_status="active", onboarding_step="complete")
        self.other = User(id=uuid4(), max_user_id=200, name="Bob", gender="male", age=25,
                          city_id=self.city.id, profile_status="active", onboarding_step="complete")
        self.tags = [Tag(id=uuid4(), code=code, name=code, kind=kind, description=code,
                         show_in_onboarding=True, is_active=True)
                     for code, kind in (("concert", "primary"), ("lecture", "primary"), ("calm", "secondary"))]
        async with self.factory() as session:
            session.add(self.city)
            session.add_all(self.tags)
            await session.flush()
            session.add_all([self.user, self.other])
            await session.flush()
            for user in (self.user, self.other):
                await OnboardingRepository(session).accept_consent(user.id, CONSENT_VERSION)
                session.add_all([UserTagWeight(user_id=user.id, tag_id=tag.id, initial_weight=Decimal("1"), reaction_weight=Decimal("0")) for tag in self.tags])
            await session.commit()
        self.dispatcher = Dispatcher()
        register_onboarding_handlers(self.dispatcher, self.factory)
        register_feed_handlers(self.dispatcher, self.factory)

    async def asyncTearDown(self):
        await self.transaction.rollback()
        await self.connection.close()
        await self.engine.dispose()

    async def event(self, title="Event", event_id=None):
        event = Event(id=event_id or uuid4(), title=title, city_id=self.city.id, tagging_status="done")
        async with self.factory() as session:
            session.add(event)
            await session.flush()
            session.add(EventSource(event_id=event.id, source="test", external_id=str(event.id),
                                    source_url="https://example.test/event", raw_payload={}, is_primary=True))
            session.add_all([EventTag(event_id=event.id, tag_id=tag.id, kind=tag.kind) for tag in (self.tags[0], self.tags[2])])
            await session.commit()
        return event

    async def weight(self, user_id):
        async with self.factory() as session:
            return (await session.get(UserTagWeight, (user_id, self.tags[0].id))).reaction_weight

    async def click(self, payload, max_id=100):
        event = callback(payload, max_id)
        handler = await select_handler(self.dispatcher, event)
        self.assertIsNotNone(handler)
        with patch.object(MessageCallback, "edit", new_callable=AsyncMock) as edit, \
             patch.object(MessageCallback, "ack", new_callable=AsyncMock) as ack:
            await handler(event)
            self.assertFalse(ack.called, ack.call_args)
            return edit

    async def test_like_and_direct_plan_have_identical_reversible_weight(self):
        event = await self.event()
        async with self.factory() as session:
            repo = FeedRepository(session)
            await repo.record_reaction(self.user.id, event.id, "like")
            first = await repo.want_to_go(self.user.id, event.id)
            await repo.want_to_go(self.other.id, event.id)
            await repo.want_to_go(self.user.id, event.id)
            await session.commit()
        self.assertEqual(await self.weight(self.user.id), Decimal("0.5"))
        self.assertEqual(await self.weight(self.other.id), Decimal("0.5"))
        async with self.factory() as session:
            repo = FeedRepository(session)
            await repo.cancel_plan(self.user.id, first.plan_id)
            await repo.cancel_plan(self.user.id, first.plan_id)
            await session.commit()
        self.assertEqual(await self.weight(self.user.id), Decimal("0.1"))
        async with self.factory() as session:
            repo = FeedRepository(session)
            self.assertEqual([item.id for item in await repo.liked_cards(self.user.id)], [event.id])
            restored = await repo.want_to_go(self.user.id, event.id)
            self.assertEqual(restored.plan_id, first.plan_id)
            await repo.remove_like(self.user.id, event.id)
            await session.commit()
        self.assertEqual(await self.weight(self.user.id), Decimal("0.5"))
        async with self.factory() as session:
            repo = FeedRepository(session)
            await repo.cancel_plan(self.user.id, first.plan_id)
            await repo.remove_like(self.user.id, event.id)
            await session.commit()
            self.assertIsNone(await repo.next_card(self.user.id))
            self.assertEqual((await session.get(EventReaction, (self.user.id, event.id))).reaction, "skip")
        self.assertEqual(await self.weight(self.user.id), Decimal("0"))

    async def test_unlike_removes_only_its_own_contribution(self):
        first, second = await self.event("First"), await self.event("Second")
        async with self.factory() as session:
            repo = FeedRepository(session)
            for event in (first, second):
                await repo.record_reaction(self.user.id, event.id, "like")
            await repo.remove_like(self.user.id, first.id)
            await repo.remove_like(self.user.id, first.id)
            await session.commit()
        self.assertEqual(await self.weight(self.user.id), Decimal("0.1"))

    async def test_views_are_saved_on_display_and_reaction_is_only_allowed_once(self):
        first, second = await self.event("First"), await self.event("Second")
        async with self.factory() as session:
            feed, people = FeedRepository(session), CompanionRepository(session)
            pairs = []
            for event in (first, second):
                a = await feed.want_to_go(self.user.id, event.id)
                b = await feed.want_to_go(self.other.id, event.id)
                await feed.set_company_search(self.user.id, a.plan_id, looking=True)
                await feed.set_company_search(self.other.id, b.plan_id, looking=True)
                pairs.append((a, b))
            a, b = pairs[0]
            self.assertEqual((await people.next_candidate(self.user.id, a.plan_id)).user_id, self.other.id)
            self.assertIsNone(await people.next_candidate(self.user.id, a.plan_id))
            view = await session.get(CompanionView, (self.user.id, first.id, self.other.id))
            self.assertIsNone(view.reacted_at)
            await people.react(self.user.id, b.plan_id, liked=True)
            with self.assertRaises(OnboardingError):
                await people.react(self.user.id, b.plan_id, liked=True)
            self.assertEqual((await people.next_candidate(self.user.id, pairs[1][0].plan_id)).user_id, self.other.id)
            await people.next_candidate(self.other.id, b.plan_id)
            result = await people.react(self.other.id, a.plan_id, liked=True)
            self.assertIsNotNone(result.match_id)
            await feed.cancel_plan(self.user.id, a.plan_id)
            self.assertEqual((await session.get(EventPlan, a.plan_id)).company_status, "not_looking")
            self.assertEqual((await session.get(Match, result.match_id)).status, "closed")
            interests = (await session.scalars(select(CompanionInterest).where(CompanionInterest.event_id == first.id))).all()
            self.assertEqual({item.status for item in interests}, {"withdrawn"})
            await session.commit()

    async def test_city_callback_resumes_with_callback_sender(self):
        async with self.factory() as session:
            guest = await OnboardingRepository(session).get_or_create_user(max_user_id=300, max_username=None)
            await OnboardingRepository(session).accept_consent(guest.id, CONSENT_VERSION)
            await session.commit()
        result = await self.click(f"onboarding:city:{self.city.id}", max_id=300)
        self.assertIn("onboarding:profile:create", payloads(result.call_args.kwargs["attachments"]))

    async def test_guest_is_offered_profile_and_keeps_existing_recommendation_weights(self):
        event = await self.event()
        async with self.factory() as session:
            user = await session.get(User, self.user.id)
            user.profile_status = "guest"
            plan = await FeedRepository(session).want_to_go(user.id, event.id)
            await session.commit()
        result = await self.click(f"feed:company:yes|{plan.plan_id}|plans|0")
        self.assertIn("onboarding:profile:create", payloads(result.call_args.kwargs["attachments"]))
        await self.click("onboarding:profile:create")
        async with self.factory() as session:
            user = await session.get(User, self.user.id)
            self.assertEqual((user.profile_status, user.onboarding_step), ("draft", "name"))
            self.assertEqual((await session.get(EventPlan, plan.plan_id)).company_status, "not_looking")
            self.assertEqual(len(await OnboardingRepository(session).selected_interest_ids(user.id)), 3)
        self.assertEqual(await self.weight(self.user.id), Decimal("0.5"))

    async def test_profile_edit_saves_field_without_restarting_onboarding(self):
        await self.click("onboarding:edit:gender")
        result = await self.click("onboarding:gender:female")
        self.assertIn("onboarding:edit:photo", payloads(result.call_args.kwargs["attachments"]))
        await self.click("onboarding:edit:photo")
        await self.click("onboarding:photo:skip")
        async with self.factory() as session:
            user = await session.get(User, self.user.id)
            self.assertEqual((user.gender, user.onboarding_step, user.profile_status), ("female", "complete", "active"))
            self.assertIsNone(user.photo_attachment)
            self.assertEqual(len(await OnboardingRepository(session).selected_interest_ids(user.id)), 3)

    async def test_edit_name_and_interests_preserves_plans_and_learned_weights(self):
        event = await self.event()
        async with self.factory() as session:
            await FeedRepository(session).want_to_go(self.user.id, event.id)
            tag = Tag(id=uuid4(), code="sport", name="Sport", kind="primary", description="Sport",
                      is_active=True, show_in_onboarding=True)
            session.add(tag)
            await session.commit()
        await self.click("onboarding:edit:name")
        event_message = message("New Name", 100)
        handler = await select_handler(self.dispatcher, event_message)
        with patch.object(type(event_message.message), "answer", new_callable=AsyncMock) as answer:
            await handler(event_message)
            self.assertIn("New Name", answer.call_args.args[0])
        await self.click("onboarding:edit:interests")
        await self.click(f"onboarding:interest:{tag.id}")
        await self.click(f"onboarding:interest:{self.tags[0].id}")
        await self.click("onboarding:interest:finish")
        async with self.factory() as session:
            user = await session.get(User, self.user.id)
            self.assertEqual((user.name, user.city_id, user.onboarding_step), ("New Name", self.city.id, "complete"))
            weight = await session.get(UserTagWeight, (user.id, self.tags[0].id))
            self.assertEqual((weight.initial_weight, weight.reaction_weight), (Decimal("0"), Decimal("0.5")))
            self.assertEqual([card.id for card in await FeedRepository(session).planned_cards(user.id)], [event.id])

    async def test_text_at_button_step_repeats_current_buttons(self):
        async with self.factory() as session:
            user = await session.get(User, self.user.id)
            user.profile_status = "draft"
            user.onboarding_step = "gender"
            await session.commit()
        event_message = message("любой текст", 100)
        handler = await select_handler(self.dispatcher, event_message)
        with patch.object(type(event_message.message), "answer", new_callable=AsyncMock) as answer:
            await handler(event_message)
        self.assertIn("Выбери действие кнопкой ниже", answer.call_args.args[0])
        self.assertIn("onboarding:gender:male", payloads(answer.call_args.kwargs["attachments"]))
        async with self.factory() as session:
            self.assertEqual((await session.get(User, self.user.id)).onboarding_step, "gender")

    async def test_text_at_city_step_only_offers_moscow(self):
        async with self.factory() as session:
            guest = await OnboardingRepository(session).get_or_create_user(max_user_id=300, max_username=None)
            await OnboardingRepository(session).accept_consent(guest.id, CONSENT_VERSION)
            await session.commit()
        event_message = message("Санкт-Петербург", 300)
        handler = await select_handler(self.dispatcher, event_message)
        with patch.object(type(event_message.message), "answer", new_callable=AsyncMock) as answer:
            await handler(event_message)
        self.assertIn("Сейчас MVP работает только в Москве", answer.call_args.args[0])
        self.assertIn(f"onboarding:city:{self.city.id}", payloads(answer.call_args.kwargs["attachments"]))
        async with self.factory() as session:
            self.assertIsNone((await session.get(User, guest.id)).city_id)

    async def test_failed_companion_send_does_not_consume_profile(self):
        event = await self.event()
        async with self.factory() as session:
            repo = FeedRepository(session)
            a = await repo.want_to_go(self.user.id, event.id)
            b = await repo.want_to_go(self.other.id, event.id)
            await repo.set_company_search(self.other.id, b.plan_id, looking=True)
            await session.commit()
        event_callback = callback(f"feed:company:yes|{a.plan_id}|plans|0", 100)
        handler = await select_handler(self.dispatcher, event_callback)
        with patch.object(MessageCallback, "edit", new_callable=AsyncMock, side_effect=RuntimeError("MAX unavailable")):
            with self.assertRaisesRegex(RuntimeError, "MAX unavailable"):
                await handler(event_callback)
        async with self.factory() as session:
            self.assertIsNone(await session.get(CompanionView, (self.user.id, event.id, self.other.id)))
        await self.click(f"feed:company:yes|{a.plan_id}|plans|0")
        async with self.factory() as session:
            self.assertIsNotNone(await session.get(CompanionView, (self.user.id, event.id, self.other.id)))

    async def test_companion_view_is_saved_when_callback_edits_existing_message(self):
        event = await self.event()
        async with self.factory() as session:
            repo = FeedRepository(session)
            plan = await repo.want_to_go(self.user.id, event.id)
            other_plan = await repo.want_to_go(self.other.id, event.id)
            await repo.set_company_search(self.other.id, other_plan.plan_id, looking=True)
            await session.commit()

        event_callback = callback(f"feed:company:yes|{plan.plan_id}|plans|0", 100)
        event_callback.message = message("Previous card", 100).message
        handler = await select_handler(self.dispatcher, event_callback)
        with patch.object(MessageCallback, "edit", new_callable=AsyncMock) as edit:
            await handler(event_callback)
        edit.assert_awaited_once()

        async with self.factory() as session:
            self.assertIsNotNone(await session.get(CompanionView, (self.user.id, event.id, self.other.id)))

    async def test_event_navigation_sends_a_new_message_without_replacing_the_old_card(self):
        event = await self.event()
        async with self.factory() as session:
            await FeedRepository(session).record_reaction(self.user.id, event.id, "like")
            await session.commit()

        event_callback = callback("feed:browse:liked|0", 100)
        event_callback.message = message("Previous card", 100).message
        handler = await select_handler(self.dispatcher, event_callback)
        with patch.object(MessageCallback, "edit", new_callable=AsyncMock) as edit, \
             patch.object(MessageCallback, "send", new_callable=AsyncMock) as send:
            await handler(event_callback)

        edit.assert_awaited_once()
        self.assertIn(event.title, send.call_args.args[0])

    async def test_demo_reset_rearms_a_completed_demo_match(self):
        event = await self.event(event_id=DEMO_EVENT_ID)
        async with self.factory() as session:
            other = await session.get(User, self.other.id)
            other.max_user_id = DEMO_MAX_USER_ID
            feed = FeedRepository(session)
            user_plan = await feed.want_to_go(self.user.id, event.id)
            demo_plan = await feed.want_to_go(self.other.id, event.id)
            await feed.set_company_search(self.user.id, user_plan.plan_id, looking=True)
            await feed.set_company_search(self.other.id, demo_plan.plan_id, looking=True)
            session.add(CompanionView(
                viewer_id=self.user.id,
                event_id=event.id,
                shown_user_id=self.other.id,
            ))
            session.add_all([
                CompanionInterest(
                    sender_plan_id=user_plan.plan_id,
                    recipient_plan_id=demo_plan.plan_id,
                    event_id=event.id,
                ),
                CompanionInterest(
                    sender_plan_id=demo_plan.plan_id,
                    recipient_plan_id=user_plan.plan_id,
                    event_id=event.id,
                ),
            ])
            first_user_id, second_user_id = sorted((self.user.id, self.other.id), key=str)
            session.add(Match(
                event_id=event.id,
                first_user_id=first_user_id,
                second_user_id=second_user_id,
            ))
            await session.commit()

        async with self.factory() as session:
            self.assertEqual(await DemoRepository(session).reset_for_user(self.user.id), event.id)
            await session.commit()

        async with self.factory() as session:
            self.assertIsNone(await session.get(CompanionView, (self.user.id, event.id, self.other.id)))
            interests = list((await session.scalars(select(CompanionInterest).where(
                CompanionInterest.event_id == event.id,
            ))).all())
            self.assertEqual({interest.status for interest in interests}, {"withdrawn"})
            match = await session.scalar(select(Match).where(Match.event_id == event.id))
            self.assertEqual(match.status, "closed")

    async def test_user_cannot_cancel_another_users_plan(self):
        event = await self.event()
        async with self.factory() as session:
            repo = FeedRepository(session)
            plan = await repo.want_to_go(self.other.id, event.id)
            with self.assertRaises(OnboardingError):
                await repo.cancel_plan(self.user.id, plan.plan_id)
            self.assertEqual((await session.get(EventPlan, plan.plan_id)).status, "planned")

    async def test_browsing_passes_tenth_card_and_cancel_unlike_are_inline(self):
        for i in range(12):
            event = await self.event(f"Event {i}")
            async with self.factory() as session:
                await FeedRepository(session).record_reaction(self.user.id, event.id, "like")
                await session.commit()
        seen = set()
        next_payload = "feed:browse:liked|0"
        for i in range(12):
            result = await self.click(next_payload)
            self.assertIn(f"{i + 1}/12", result.call_args.args[0])
            seen.add(result.call_args.args[0].split("\n\n")[1])
            buttons = payloads(result.call_args.kwargs["attachments"])
            next_payload = f"feed:browse:liked|{i + 1}"
            if i < 11:
                self.assertIn(next_payload, buttons)
        self.assertEqual(len(seen), 12)
        want = next(value for value in buttons if value.startswith("feed:want:"))
        company = await self.click(want)
        no = next(value for value in payloads(company.call_args.kwargs["attachments"]) if value.startswith("feed:company:no|"))
        after = await self.click(no)
        self.assertIn("11/11", after.call_args.args[0])
        plan = await self.click("feed:browse:plans|0")
        plan_buttons = payloads(plan.call_args.kwargs["attachments"])
        await self.click(next(value for value in plan_buttons if value.startswith("feed:unlike:")))
        empty = await self.click(next(value for value in plan_buttons if value.startswith("feed:cancel:")))
        self.assertIn("пока пусто", empty.call_args.args[0])

    async def test_migration_normalizes_legacy_weights_and_preserves_acted_views(self):
        event = await self.event()
        async with self.factory() as session:
            await FeedRepository(session).want_to_go(self.user.id, event.id)
            weight = await session.get(UserTagWeight, (self.user.id, self.tags[0].id))
            weight.reaction_weight = Decimal("0.6")
            session.add(CompanionView(viewer_id=self.user.id, event_id=event.id, shown_user_id=self.other.id))
            await session.commit()
        migration = importlib.import_module("infrastructure.db.migrations.versions.0005_social_actions")
        def run(connection):
            with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
                migration.downgrade()
                migration.upgrade()
        await self.connection.run_sync(run)
        self.assertEqual(await self.weight(self.user.id), Decimal("0.5"))
        async with self.factory() as session:
            view = await session.get(CompanionView, (self.user.id, event.id, self.other.id))
            self.assertEqual(view.reacted_at, view.shown_at)
            weight = await session.get(UserTagWeight, (self.user.id, self.tags[0].id))
            self.assertEqual(weight.initial_weight, Decimal("1"))
