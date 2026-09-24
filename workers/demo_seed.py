"""Seed a fixed, repeatable /demo match scenario."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from dotenv import load_dotenv
from sqlalchemy import select, update

from infrastructure.db.models import City, Event, EventSchedule, EventSource, EventTag, Tag
from infrastructure.db.repositories.demo import DEMO_EVENT_ID, DEMO_PROFILES
from infrastructure.db.session import create_async_database_engine, create_session_factory
from infrastructure.db.social_models import EventPlan, User


DEMO_SOURCE = "dvizhmax_demo"
DEMO_SOURCE_ID = "match-flow-v1"


async def _meeting_tag(session) -> Tag:
    tag = await session.scalar(select(Tag).where(Tag.code == "meetup"))
    if tag is None:
        tag = Tag(
            id=uuid5(NAMESPACE_URL, "dvizhmax:tag:meetup"),
            code="meetup",
            name="Встречи",
            kind="primary",
            description="Встречи и общение",
            is_active=True,
            show_in_onboarding=True,
        )
        session.add(tag)
        await session.flush()
    return tag


async def _demo_event(session, city: City) -> Event:
    event = await session.get(Event, DEMO_EVENT_ID)
    if event is None:
        event = Event(id=DEMO_EVENT_ID, title="Демо-встреча ДвижМАКС", city_id=city.id)
        session.add(event)
    event.title = "Демо-встреча ДвижМАКС"
    event.description = "Фиксированное событие для демонстрации поиска компании и мэтча."
    event.city_id = city.id
    event.price_text = "Бесплатно"
    event.price_min = 0
    event.currency = "RUB"
    event.is_free = True
    event.age_min = 18
    event.data_status = "current"
    event.tagging_status = "done"
    await session.flush()

    source = await session.scalar(select(EventSource).where(
        EventSource.source == DEMO_SOURCE,
        EventSource.external_id == DEMO_SOURCE_ID,
    ))
    if source is None:
        source = EventSource(
            event_id=event.id,
            source=DEMO_SOURCE,
            external_id=DEMO_SOURCE_ID,
            source_url="https://max.ru/t110_hakaton_max_bot",
            raw_payload={"kind": "fixed_demo"},
            is_primary=True,
        )
        session.add(source)
        await session.flush()
    else:
        source.event_id = event.id
        source.source_url = "https://max.ru/t110_hakaton_max_bot"
        source.raw_payload = {"kind": "fixed_demo"}
        source.is_primary = True

    schedule = await session.scalar(select(EventSchedule).where(EventSchedule.event_source_id == source.id))
    starts_at = datetime.now(timezone.utc) + timedelta(days=7)
    if schedule is None:
        session.add(EventSchedule(
            event_source_id=source.id,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=3),
            raw_payload={"kind": "fixed_demo"},
        ))
    else:
        schedule.starts_at = starts_at
        schedule.ends_at = starts_at + timedelta(hours=3)
        schedule.raw_payload = {"kind": "fixed_demo"}

    tag = await _meeting_tag(session)
    if await session.get(EventTag, (event.id, tag.id)) is None:
        session.add(EventTag(event_id=event.id, tag_id=tag.id, kind=tag.kind))
    return event


async def _seed_profiles(session, event: Event) -> None:
    profile_ids: list[UUID] = []
    for profile in DEMO_PROFILES:
        user = await session.scalar(select(User).where(User.max_user_id == profile.max_user_id))
        if user is None:
            user = User(id=profile.id, max_user_id=profile.max_user_id)
            session.add(user)
        user.max_username = profile.username
        user.name = profile.name
        user.gender = profile.gender
        user.age = profile.age
        user.description = profile.description
        user.photo_url = f"asset://{profile.asset_name}"
        user.photo_attachment = None
        user.city_id = event.city_id
        user.profile_status = "active"
        user.onboarding_step = "complete"
        await session.flush()
        profile_ids.append(user.id)

        plan = await session.scalar(select(EventPlan).where(
            EventPlan.user_id == user.id,
            EventPlan.event_id == event.id,
        ))
        if plan is None:
            plan = EventPlan(user_id=user.id, event_id=event.id)
            session.add(plan)
        plan.status = "planned"
        plan.company_status = "looking"

    # Old seeds could attach a demo profile to a real event. Keep demo users
    # visible only on the fixed demo event from now on.
    await session.execute(
        update(EventPlan)
        .where(EventPlan.user_id.in_(profile_ids), EventPlan.event_id != event.id)
        .values(status="cancelled", company_status="not_looking")
    )


async def main() -> None:
    load_dotenv()
    engine = create_async_database_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            city = await session.scalar(select(City).where(City.name == "Москва").limit(1))
            if city is None:
                raise RuntimeError("Сначала загрузите каталог Москвы")
            event = await _demo_event(session, city)
            await _seed_profiles(session, event)
            await session.commit()
            print(f"Fixed demo match is ready for event {event.id}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
