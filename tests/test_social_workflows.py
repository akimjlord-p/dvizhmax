"""Optional integration tests; TEST_DATABASE_URL must name a dedicated *_test DB."""
import asyncio
import importlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from maxapi import Dispatcher
from maxapi.exceptions.max import MaxApiError
from maxapi.types import MessageCallback, MessageCreated
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.contacts import register_contact_handlers
from bot.feed import register_feed_handlers
from bot.onboarding import CONSENT_VERSION, register_onboarding_handlers
from infrastructure.db.models import City, Event, EventImage, EventSchedule, EventSource, EventTag, Tag
from infrastructure.db.repositories import CompanionRepository, DemoRepository, FeedRepository, NotificationRepository, OnboardingRepository, OnboardingError
from infrastructure.db.repositories.demo import DEMO_EVENT_ID, DEMO_MAX_USER_ID
from infrastructure.db.social_models import CompanionInterest, CompanionView, EventPlan, EventReaction, Match, MatchContact, User, UserTagWeight
from bot.feed import NO_CANDIDATES_TEXT, PERSON_LIKED_STATUS
from infrastructure.db.repositories.feed import KIDS_COMPANY_TEXT
from bot.navigation import ERROR_TEXT, MENU_PAYLOAD
from test_bot_routing import callback, message, select_handler


TEST_URL = os.getenv("TEST_DATABASE_URL")


async def strict_ack(notification=None):
    """MAX answers 400 to a callback answer without a notification or message."""
    if not notification:
        raise AssertionError("MAX rejects an empty callback answer")


def payloads(attachments):
    return [button.payload for item in attachments if str(item.type) == "inline_keyboard"
            for row in item.payload.buttons for button in row if hasattr(button, "payload")]


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
        # Test events have no photo; do not upload the placeholder to MAX.
        placeholder = patch("bot.feed._placeholder_image_attachment", new=AsyncMock(return_value=None))
        placeholder.start()
        self.addCleanup(placeholder.stop)
        self.dispatcher = Dispatcher()
        register_onboarding_handlers(self.dispatcher, self.factory)
        register_feed_handlers(self.dispatcher, self.factory)
        register_contact_handlers(self.dispatcher, self.factory)

    async def asyncTearDown(self):
        await self.transaction.rollback()
        await self.connection.close()
        await self.engine.dispose()

    async def event(self, title="Event", event_id=None):
        event = Event(id=event_id or uuid4(), title=title, city_id=self.city.id, tagging_status="done")
        async with self.factory() as session:
            session.add(event)
            await session.flush()
            source = EventSource(event_id=event.id, source="test", external_id=str(event.id),
                                 source_url="https://example.test/event", raw_payload={}, is_primary=True)
            session.add(source)
            await session.flush()
            # Feed and liked lists only show events with an upcoming session.
            starts_at = datetime.now(timezone.utc) + timedelta(days=3)
            session.add(EventSchedule(event_source_id=source.id, starts_at=starts_at,
                                      ends_at=starts_at + timedelta(hours=2), raw_payload={}))
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
             patch.object(MessageCallback, "ack", new=AsyncMock(side_effect=strict_ack)) as ack:
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
            self.assertTrue((await people.react(self.user.id, b.plan_id, liked=True)).was_applied)
            # A repeated callback is acknowledged without changing state.
            self.assertFalse((await people.react(self.user.id, b.plan_id, liked=True)).was_applied)
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

    async def test_consent_decline_has_a_button_to_start_again(self):
        declined = await self.click("onboarding:consent:decline", max_id=300)
        self.assertIn("onboarding:resume:current", payloads(declined.call_args.kwargs["attachments"]))

        resumed = await self.click("onboarding:resume:current", max_id=300)
        self.assertIn("onboarding:consent:accept", payloads(resumed.call_args.kwargs["attachments"]))

    async def test_mandatory_text_step_has_no_continue_button(self):
        async with self.factory() as session:
            user = await session.get(User, self.user.id)
            user.onboarding_step = "name"
            await session.commit()

        result = await self.click("onboarding:resume:current")
        self.assertNotIn("onboarding:resume:current", payloads(result.call_args.kwargs["attachments"]))

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
        self.assertIn("Сейчас здесь нужно выбрать действие кнопкой ниже", answer.call_args.args[0])
        self.assertIn("onboarding:gender:male", payloads(answer.call_args.kwargs["attachments"]))
        async with self.factory() as session:
            self.assertEqual((await session.get(User, self.user.id)).onboarding_step, "gender")

    async def test_rejected_event_photo_falls_back_to_a_card_without_it(self):
        event = await self.event("Broken photo")
        async with self.factory() as session:
            source = await session.scalar(select(EventSource).where(EventSource.event_id == event.id))
            session.add(EventImage(event_source_id=source.id, url="https://example.test/broken.jpg"))
            await session.commit()
        rejected = MaxApiError(code=400, raw={"code": "proto.payload", "message": "Failed to upload image."})
        callback_event = callback("feed:browse:feed|0", 100)
        handler = await select_handler(self.dispatcher, callback_event)
        with patch.object(MessageCallback, "edit", new_callable=AsyncMock, side_effect=[rejected, None]) as edit, \
             patch.object(MessageCallback, "ack", new=AsyncMock(side_effect=strict_ack)) as ack:
            await handler(callback_event)
        self.assertFalse(ack.called)
        self.assertEqual(edit.await_count, 2)
        first, retry = (call.kwargs["attachments"] for call in edit.await_args_list)
        self.assertIn("example.test/broken.jpg", str(first))
        self.assertNotIn("example.test/broken.jpg", str(retry))
        self.assertIn("Broken photo", edit.call_args.args[0])

    async def matched_pair(self, title="Contact event"):
        event = await self.event(title)
        async with self.factory() as session:
            feed, people = FeedRepository(session), CompanionRepository(session)
            alice = await feed.want_to_go(self.user.id, event.id)
            bob = await feed.want_to_go(self.other.id, event.id)
            await feed.set_company_search(self.user.id, alice.plan_id, looking=True)
            await feed.set_company_search(self.other.id, bob.plan_id, looking=True)
            await people.next_candidate(self.user.id, alice.plan_id)
            await people.react(self.user.id, bob.plan_id, liked=True)
            await people.next_candidate(self.other.id, bob.plan_id)
            result = await people.react(self.other.id, alice.plan_id, liked=True)
            await session.commit()
        return result.match_id

    async def press(self, payload, max_id=100):
        event = callback(payload, max_id)
        handler = await select_handler(self.dispatcher, event)
        event.bot = SimpleNamespace(me=None, send_message=AsyncMock())
        with patch.object(MessageCallback, "edit", new_callable=AsyncMock) as edit, \
             patch.object(MessageCallback, "ack", new=AsyncMock(side_effect=strict_ack)) as ack:
            await handler(event)
        return event.bot.send_message, edit, ack

    async def send_contact(self, max_id=100, *, text=None, contact=None):
        body = {"mid": "m2", "seq": 2, "text": text}
        if contact is not None:
            body["attachments"] = [{"type": "contact", "payload": contact}]
        event = MessageCreated.model_validate({
            "update_type": "message_created", "timestamp": 0,
            "message": {"sender": {"user_id": max_id, "first_name": "A", "is_bot": False, "last_activity_time": 0},
                        "recipient": {"chat_id": max_id, "chat_type": "dialog"}, "timestamp": 0, "body": body},
        })
        handler = await select_handler(self.dispatcher, event)
        with patch.object(type(event.message), "answer", new_callable=AsyncMock) as answer:
            await handler(event)
        return answer

    async def test_match_contact_is_typed_confirmed_and_delivered(self):
        match_id = await self.matched_pair()
        send, _, _ = await self.press(f"contact:share:{match_id}")
        prompt = send.await_args.kwargs
        self.assertIn("Bob", prompt["text"])
        self.assertEqual(payloads(prompt["attachments"]), [f"contact:cancel:{match_id}"])

        # A MAX contact card is not accepted: the phone must be typed on purpose.
        vcf = "BEGIN:VCARD\nVERSION:3.0\nFN:Alice Smith\nTEL:+79990001122\nEND:VCARD"
        answer = await self.send_contact(contact={"vcf_info": vcf})
        self.assertIn("Отправь ссылку-приглашение MAX", answer.call_args.args[0])

        answer = await self.send_contact(text="https://max.ru/join/alice")
        self.assertIn("Отправить этот контакт пользователю Bob?", answer.call_args.args[0])
        async with self.factory() as session:
            user = await session.get(User, self.user.id)
            self.assertEqual(user.name, "Alice")  # the contact did not leak into the profile

        send, edit, _ = await self.press(f"contact:confirm:{match_id}")
        delivered = send.await_args.kwargs
        self.assertEqual(delivered["user_id"], 200)
        self.assertIn("Alice делится контактом", delivered["text"])
        self.assertIn("https://max.ru/join/alice", delivered["text"])
        self.assertIn(f"contact:share:{match_id}", payloads(delivered["attachments"]))
        self.assertIn("Контакт отправлен: Bob", edit.call_args.args[0])

        _, _, ack = await self.press(f"contact:confirm:{match_id}")
        ack.assert_awaited_once_with("Контакт уже отправлен")
        async with self.factory() as session:
            row = await session.scalar(select(MatchContact).where(MatchContact.match_id == match_id))
            self.assertEqual(row.status, "sent")

    async def test_leaving_profile_edit_through_the_menu_does_not_overwrite_the_field(self):
        await self.event("Some event")
        await self.click("onboarding:edit:name")
        await self.click("feed:browse:feed|0")
        event_message = message("привет", 100)
        handler = await select_handler(self.dispatcher, event_message)
        with patch.object(type(event_message.message), "answer", new_callable=AsyncMock) as answer:
            await handler(event_message)
        self.assertIn("нужно выбрать действие", answer.call_args.args[0])
        async with self.factory() as session:
            user = await session.get(User, self.user.id)
            self.assertEqual((user.name, user.onboarding_step), ("Alice", "complete"))

    async def test_feed_refills_when_every_queued_card_became_stale(self):
        first, second, third = await self.event("First"), await self.event("Second"), await self.event("Third")
        edit = await self.click("feed:browse:feed|0")  # queues all three, shows one
        shown = next(e for e in (first, second, third) if e.title in edit.call_args.args[0])
        async with self.factory() as session:
            repo = FeedRepository(session)
            for stale in {first, second, third} - {shown}:
                await repo.record_reaction(self.user.id, stale.id, "skip")
            await session.commit()
        fresh = await self.event("Fresh")
        edit = await self.click("feed:browse:feed|0")
        self.assertIn("Fresh", edit.call_args.args[0])
        self.assertNotEqual(fresh.id, shown.id)

    async def test_childrens_events_are_hidden_from_the_feed(self):
        adult, kids = await self.event("Концерт"), await self.event("Плавание для детей")
        async with self.factory() as session:
            cards = await FeedRepository(session).next_cards(self.user.id, limit=10)
        self.assertEqual([card.id for card in cards], [adult.id])
        self.assertNotEqual(kids.id, adult.id)

    async def test_company_search_is_not_offered_for_childrens_events(self):
        event = await self.event("Плавание для детей")
        edit = await self.click(f"feed:want:{event.id}")
        self.assertIn(KIDS_COMPANY_TEXT, edit.call_args.args[0])
        self.assertNotIn("feed:company:yes", " ".join(payloads(edit.call_args.kwargs["attachments"])))
        async with self.factory() as session:
            plan = await session.scalar(select(EventPlan).where(EventPlan.user_id == self.user.id))
            with self.assertRaisesRegex(OnboardingError, KIDS_COMPANY_TEXT):
                await FeedRepository(session).set_company_search(self.user.id, plan.id, looking=True)
            [card] = await FeedRepository(session).planned_cards(self.user.id)
        self.assertTrue(card.for_kids)

    async def test_profile_is_deleted_only_after_confirmation(self):
        match_id = await self.matched_pair("Before delete")
        await self.press(f"contact:share:{match_id}")
        await self.send_contact(text="@alice")
        send, edit, _ = await self.press("onboarding:delete:ask")
        self.assertIn("Удалить профиль навсегда?", edit.call_args.args[0])
        async with self.factory() as session:
            self.assertIsNotNone(await session.get(User, self.user.id))
        _, edit, _ = await self.press("onboarding:delete:confirm")
        self.assertIn("Профиль и все данные удалены", edit.call_args.args[0])
        async with self.factory() as session:
            self.assertIsNone(await session.get(User, self.user.id))
            self.assertIsNone(await session.get(Match, match_id))
            self.assertEqual((await session.scalars(select(EventPlan).where(EventPlan.user_id == self.user.id))).all(), [])
            self.assertIsNotNone(await session.get(User, self.other.id))

    async def test_cancelled_contact_is_not_sent_and_text_goes_back_to_normal(self):
        match_id = await self.matched_pair("Cancel contact")
        await self.press(f"contact:share:{match_id}")
        await self.send_contact(text="https://max.ru/join/abc")
        send, edit, _ = await self.press(f"contact:cancel:{match_id}")
        send.assert_not_awaited()
        self.assertIn("Контакт не отправлен", edit.call_args.args[0])
        answer = await self.send_contact(text="привет")
        self.assertIn("нужно выбрать действие", answer.call_args.args[0])

    async def test_contact_for_a_demo_peer_is_not_delivered(self):
        match_id = await self.matched_pair("Demo contact")
        async with self.factory() as session:
            (await session.get(User, self.other.id)).max_user_id = DEMO_MAX_USER_ID
            await session.commit()
        await self.press(f"contact:share:{match_id}")
        await self.send_contact(text="@alice")
        send, edit, _ = await self.press(f"contact:confirm:{match_id}")
        send.assert_not_awaited()
        self.assertIn("демо-анкета", edit.call_args.args[0])

    async def test_candidate_card_shows_only_interests_both_people_chose(self):
        event = await self.event("Common interests")
        async with self.factory() as session:
            # Bob keeps "concert" only as a learned weight, not a chosen interest.
            learned = await session.get(UserTagWeight, (self.other.id, self.tags[0].id))
            learned.initial_weight, learned.reaction_weight = Decimal("0"), Decimal("0.5")
            feed = FeedRepository(session)
            alice = await feed.want_to_go(self.user.id, event.id)
            bob = await feed.want_to_go(self.other.id, event.id)
            await feed.set_company_search(self.user.id, alice.plan_id, looking=True)
            await feed.set_company_search(self.other.id, bob.plan_id, looking=True)
            await session.commit()
        edit = await self.click(f"feed:company:yes|{alice.plan_id}|plans|0")
        self.assertIn("Общие интересы: calm, lecture", edit.call_args.args[0])

    async def test_person_like_without_match_is_confirmed_on_the_card(self):
        event = await self.event("Person like")
        async with self.factory() as session:
            feed, people = FeedRepository(session), CompanionRepository(session)
            alice = await feed.want_to_go(self.user.id, event.id)
            bob = await feed.want_to_go(self.other.id, event.id)
            await feed.set_company_search(self.user.id, alice.plan_id, looking=True)
            await feed.set_company_search(self.other.id, bob.plan_id, looking=True)
            await people.next_candidate(self.user.id, alice.plan_id)
            await session.commit()
        callback_event = MessageCallback.model_validate({
            "update_type": "message_callback", "timestamp": 0,
            "callback": {"timestamp": 0, "callback_id": "c1", "payload": f"feed:person_like:{bob.plan_id}",
                         "user": {"user_id": 100, "first_name": "A", "is_bot": False, "last_activity_time": 0}},
            "message": {"recipient": {"chat_id": 100, "chat_type": "dialog"}, "timestamp": 0,
                        "body": {"mid": "m1", "seq": 1, "text": "Bob, 25", "attachments": [{"type": "inline_keyboard", "payload": {"buttons": [[
                            {"type": "callback", "text": "👍 Пойти вместе", "payload": f"feed:person_like:{bob.plan_id}"},
                            {"type": "callback", "text": "Дальше", "payload": f"feed:person_skip:{bob.plan_id}"}]]}}]}},
        })
        handler = await select_handler(self.dispatcher, callback_event)
        with patch.object(MessageCallback, "edit", new_callable=AsyncMock) as edit, \
             patch.object(MessageCallback, "send", new_callable=AsyncMock) as send:
            await handler(callback_event)
        self.assertEqual(edit.call_args.args[0], f"Bob, 25\n\n{PERSON_LIKED_STATUS}")
        self.assertFalse(payloads(edit.call_args.kwargs["attachments"]))
        self.assertEqual(send.call_args.args[0], NO_CANDIDATES_TEXT)

    async def test_card_that_failed_to_send_returns_to_the_feed_queue(self):
        event = await self.event("Requeue me")
        callback_event = callback("feed:browse:feed|0", 100)
        handler = await select_handler(self.dispatcher, callback_event)
        callback_event.bot = SimpleNamespace(me=None, send_message=AsyncMock())
        with patch.object(MessageCallback, "edit", new_callable=AsyncMock, side_effect=RuntimeError("MAX down")), \
             patch.object(MessageCallback, "ack", new=AsyncMock(side_effect=strict_ack)):
            await handler(callback_event)
        edit = await self.click("feed:browse:feed|0")
        self.assertIn("Requeue me", edit.call_args.args[0])

    async def test_rated_card_shows_its_reaction_and_loses_reaction_buttons(self):
        for action, status, kept, removed in (
            ("like", "✅ Нравится", {"feed:want"}, {"feed:like", "feed:skip"}),
            ("skip", "✖️ Не моё", set(), {"feed:like", "feed:skip", "feed:want"}),
        ):
            with self.subTest(action=action):
                event = await self.event(f"Rated {action}")
                buttons = [[{"type": "callback", "text": "Нравится", "payload": f"feed:like:{event.id}"},
                            {"type": "callback", "text": "Не интересно", "payload": f"feed:skip:{event.id}"}],
                           [{"type": "callback", "text": "Пойду", "payload": f"feed:want:{event.id}"}],
                           [{"type": "link", "text": "Подробнее", "url": "https://example.test/event"}]]
                callback_event = MessageCallback.model_validate({
                    "update_type": "message_callback", "timestamp": 0,
                    "callback": {"timestamp": 0, "callback_id": "c1", "payload": f"feed:{action}:{event.id}",
                                 "user": {"user_id": 100, "first_name": "A", "is_bot": False, "last_activity_time": 0}},
                    "message": {"recipient": {"chat_id": 100, "chat_type": "dialog"}, "timestamp": 0,
                                "body": {"mid": "m1", "seq": 1, "text": f"Rated {action}",
                                         "attachments": [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]}},
                })
                handler = await select_handler(self.dispatcher, callback_event)
                with patch.object(MessageCallback, "edit", new_callable=AsyncMock) as edit, \
                     patch.object(MessageCallback, "send", new_callable=AsyncMock) as send, \
                     patch.object(MessageCallback, "ack", new=AsyncMock(side_effect=strict_ack)) as ack:
                    await handler(callback_event)
                self.assertFalse(ack.called)
                send.assert_awaited_once()
                self.assertEqual(edit.call_args.args[0], f"Rated {action}\n\n{status}")
                left = {payload.rsplit(":", 1)[0] for payload in payloads(edit.call_args.kwargs["attachments"])}
                self.assertTrue(kept <= left)
                self.assertFalse(removed & left)
                self.assertIn("Подробнее", str(edit.call_args.kwargs["attachments"]))

    async def test_unexpected_callback_error_offers_retry_and_menu(self):
        callback_event = callback("feed:browse:feed|0", 100)
        handler = await select_handler(self.dispatcher, callback_event)
        callback_event.bot = SimpleNamespace(me=None, send_message=AsyncMock())
        with patch("bot.feed.FeedBufferStore.pop", new=AsyncMock(side_effect=RuntimeError("redis down"))), \
             patch.object(MessageCallback, "ack", new=AsyncMock(side_effect=strict_ack)) as ack:
            await handler(callback_event)
        ack.assert_awaited_once_with("Не получилось выполнить действие")
        sent = callback_event.bot.send_message.await_args
        self.assertEqual(sent.kwargs["text"], ERROR_TEXT)
        self.assertEqual(payloads(sent.kwargs["attachments"]), ["feed:browse:feed|0", MENU_PAYLOAD])

    async def test_free_text_after_onboarding_shows_menu_not_profile(self):
        event_message = message("Хуй", 100)
        handler = await select_handler(self.dispatcher, event_message)
        with patch.object(type(event_message.message), "answer", new_callable=AsyncMock) as answer:
            await handler(event_message)
        answer.assert_awaited_once()
        self.assertNotIn("Твоя анкета", answer.call_args.args[0])
        self.assertIn("feed:browse:feed|0", payloads(answer.call_args.kwargs["attachments"]))
        async with self.factory() as session:
            user = await session.get(User, self.user.id)
            self.assertEqual((user.onboarding_step, user.name), ("complete", "Alice"))

    async def test_text_at_city_step_only_offers_moscow(self):
        async with self.factory() as session:
            guest = await OnboardingRepository(session).get_or_create_user(max_user_id=300, max_username=None)
            await OnboardingRepository(session).accept_consent(guest.id, CONSENT_VERSION)
            await session.commit()
        event_message = message("Санкт-Петербург", 300)
        handler = await select_handler(self.dispatcher, event_message)
        with patch.object(type(event_message.message), "answer", new_callable=AsyncMock) as answer:
            await handler(event_message)
        self.assertIn("На этапе MVP ДвижМАКС работает в Москве", answer.call_args.args[0])
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
        event_callback.bot = SimpleNamespace(me=None, send_message=AsyncMock())
        with patch.object(MessageCallback, "edit", new_callable=AsyncMock, side_effect=RuntimeError("MAX unavailable")), \
             patch.object(MessageCallback, "ack", new=AsyncMock(side_effect=strict_ack)):
            await handler(event_callback)
        self.assertEqual(event_callback.bot.send_message.await_args.kwargs["text"], ERROR_TEXT)
        async with self.factory() as session:
            self.assertIsNone(await session.get(CompanionView, (self.user.id, event.id, self.other.id)))
        await self.click(f"feed:company:yes|{a.plan_id}|plans|0")
        async with self.factory() as session:
            self.assertIsNotNone(await session.get(CompanionView, (self.user.id, event.id, self.other.id)))

    async def test_companion_view_is_saved_when_callback_sends_a_new_message(self):
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
        with patch.object(MessageCallback, "edit", new_callable=AsyncMock) as edit, \
             patch.object(MessageCallback, "send", new_callable=AsyncMock) as send:
            await handler(event_callback)
        edit.assert_awaited_once()
        send.assert_awaited_once()

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

    async def test_demo_command_undoes_a_skip_on_the_demo_card(self):
        event = await self.event(event_id=DEMO_EVENT_ID)
        async with self.factory() as session:
            (await session.get(User, self.other.id)).max_user_id = DEMO_MAX_USER_ID
            feed = FeedRepository(session)
            demo_plan = await feed.want_to_go(self.other.id, event.id)
            await feed.set_company_search(self.other.id, demo_plan.plan_id, looking=True)
            await feed.record_reaction(self.user.id, event.id, "skip")
            await session.commit()
        async with self.factory() as session:
            await DemoRepository(session).reset_for_user(self.user.id)
            await session.commit()
        async with self.factory() as session:
            result = await FeedRepository(session).want_to_go(self.user.id, event.id)
            self.assertTrue(result.was_created)

    async def test_fixed_demo_profile_is_available_for_any_company_search_event(self):
        event = await self.event("Ordinary event")
        async with self.factory() as session:
            demo_user = await session.get(User, self.other.id)
            demo_user.max_user_id = DEMO_MAX_USER_ID
            feed = FeedRepository(session)
            user_plan = await feed.want_to_go(self.user.id, event.id)
            await feed.set_company_search(self.user.id, user_plan.plan_id, looking=True)
            self.assertTrue(await DemoRepository(session).ensure_candidates_for_plan(
                user_id=self.user.id,
                user_plan_id=user_plan.plan_id,
            ))
            await session.commit()

        async with self.factory() as session:
            demo_plan = await session.scalar(select(EventPlan).where(
                EventPlan.user_id == self.other.id,
                EventPlan.event_id == event.id,
            ))
            self.assertIsNotNone(demo_plan)
            self.assertEqual((demo_plan.status, demo_plan.company_status), ("planned", "looking"))
            interest = await session.scalar(select(CompanionInterest).where(
                CompanionInterest.sender_plan_id == demo_plan.id,
                CompanionInterest.recipient_plan_id == user_plan.plan_id,
            ))
            self.assertEqual((interest.event_id, interest.status), (event.id, "active"))

    async def test_demo_profile_interest_is_excluded_from_the_digest(self):
        event = await self.event("Ordinary event")
        async with self.factory() as session:
            demo_user = await session.get(User, self.other.id)
            demo_user.max_user_id = DEMO_MAX_USER_ID
            feed = FeedRepository(session)
            user_plan = await feed.want_to_go(self.user.id, event.id)
            demo_plan = await feed.want_to_go(demo_user.id, event.id)
            await feed.set_company_search(self.user.id, user_plan.plan_id, looking=True)
            await feed.set_company_search(demo_user.id, demo_plan.plan_id, looking=True)
            session.add(CompanionInterest(
                sender_plan_id=demo_plan.plan_id,
                recipient_plan_id=user_plan.plan_id,
                event_id=event.id,
            ))
            await session.commit()

        async with self.factory() as session:
            self.assertEqual(await NotificationRepository(session).unannounced_interest_digests(), [])

    async def test_person_who_liked_after_a_skip_is_offered_again_as_a_liker(self):
        event = await self.event("Likers")
        async with self.factory() as session:
            feed, people = FeedRepository(session), CompanionRepository(session)
            alice = await feed.want_to_go(self.user.id, event.id)
            bob = await feed.want_to_go(self.other.id, event.id)
            await feed.set_company_search(self.user.id, alice.plan_id, looking=True)
            await feed.set_company_search(self.other.id, bob.plan_id, looking=True)
            await people.next_candidate(self.user.id, alice.plan_id)
            await people.react(self.user.id, bob.plan_id, liked=False)
            await people.next_candidate(self.other.id, bob.plan_id)
            await people.react(self.other.id, alice.plan_id, liked=True)
            # One test transaction freezes now(); move Bob's like after Alice's skip.
            skip = await session.get(CompanionView, (self.user.id, event.id, self.other.id))
            interest = await session.scalar(select(CompanionInterest).where(CompanionInterest.sender_plan_id == bob.plan_id))
            interest.updated_at = skip.reacted_at + timedelta(seconds=1)
            await session.commit()

        async with self.factory() as session:
            [digest] = await NotificationRepository(session).unannounced_interest_digests()
            self.assertEqual(digest.recipient_plan_id, alice.plan_id)
            people = CompanionRepository(session)
            self.assertIsNone(await people.next_candidate(self.user.id, alice.plan_id))
            self.assertEqual(await people.pending_liker_counts(self.user.id, [alice.plan_id]), {alice.plan_id: 1})
            await session.commit()

        edit = await self.click(f"feed:likers:{alice.plan_id}")
        self.assertIn("хочет пойти с тобой", edit.call_args.args[0])
        self.assertIn(f"feed:person_like:{bob.plan_id}|likers", payloads(edit.call_args.kwargs["attachments"]))

        with patch("bot.feed.send_match_notifications", new_callable=AsyncMock) as notify:
            edit = await self.click(f"feed:person_like:{bob.plan_id}|likers")
        notify.assert_awaited_once()
        self.assertIn("мэтч", edit.call_args.args[0])
        self.assertIn(f"feed:likers:{alice.plan_id}", payloads(edit.call_args.kwargs["attachments"]))

        async with self.factory() as session:
            people = CompanionRepository(session)
            self.assertEqual(await people.pending_liker_counts(self.user.id, [alice.plan_id]), {})
            self.assertIsNone(await people.next_liker(self.user.id, alice.plan_id))

    async def test_skip_in_likers_list_hides_that_like(self):
        event = await self.event("Likers skip")
        async with self.factory() as session:
            feed, people = FeedRepository(session), CompanionRepository(session)
            alice = await feed.want_to_go(self.user.id, event.id)
            bob = await feed.want_to_go(self.other.id, event.id)
            await feed.set_company_search(self.user.id, alice.plan_id, looking=True)
            await feed.set_company_search(self.other.id, bob.plan_id, looking=True)
            await people.next_candidate(self.other.id, bob.plan_id)
            await people.react(self.other.id, alice.plan_id, liked=True)
            self.assertEqual((await people.next_liker(self.user.id, alice.plan_id)).user_id, self.other.id)
            await people.react(self.user.id, bob.plan_id, liked=False)
            self.assertIsNone(await people.next_liker(self.user.id, alice.plan_id))
            await session.commit()

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
