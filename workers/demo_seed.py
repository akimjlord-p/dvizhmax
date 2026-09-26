"""Seed a fixed, repeatable /demo match scenario."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import or_, select, update

from infrastructure.db.models import City, Event, EventSchedule, EventSource, EventTag, Tag
from infrastructure.db.repositories.demo import DEMO_EVENT_ID, DEMO_PROFILES
from infrastructure.db.session import create_async_database_engine, create_session_factory
from infrastructure.db.social_models import CompanionInterest, EventPlan, Match, User, UserTagWeight


DEMO_SOURCE = "dvizhmax_demo"
DEMO_SOURCE_ID = "match-flow-v1"


def _team_demo_user_ids() -> tuple[int, ...]:
    """Read real team profiles that should participate in the fixed demo."""
    raw_ids = os.getenv("DEMO_TEAM_MAX_USER_IDS", "")
    values = [value.strip() for value in raw_ids.split(",") if value.strip()]
    try:
        return tuple(dict.fromkeys(int(value) for value in values))
    except ValueError as exc:
        raise ValueError("DEMO_TEAM_MAX_USER_IDS must be a comma-separated list of MAX user IDs") from exc


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
    # A week ahead at 19:00 Moscow time, so the demo card shows a natural time.
    moscow = ZoneInfo("Europe/Moscow")
    day = (datetime.now(moscow) + timedelta(days=7)).date()
    starts_at = datetime.combine(day, time(19, 0), tzinfo=moscow).astimezone(timezone.utc)
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


async def _seed_interests(session, user: User, codes: tuple[str, ...] | None) -> None:
    """Make the chosen interests exactly the given tags, so common interests are predictable."""
    query = select(Tag).where(Tag.is_active.is_(True), Tag.show_in_onboarding.is_(True))
    if codes is not None:
        query = query.where(Tag.code.in_(codes))
    chosen = {tag.id for tag in (await session.scalars(query)).all()}
    existing = {weight.tag_id: weight for weight in (await session.scalars(
        select(UserTagWeight).where(UserTagWeight.user_id == user.id)
    )).all()}
    for tag_id in chosen - existing.keys():
        session.add(UserTagWeight(user_id=user.id, tag_id=tag_id, initial_weight=Decimal("1.0")))
    for tag_id, weight in existing.items():
        weight.initial_weight = Decimal("1.0") if tag_id in chosen else Decimal("0")
    await session.flush()


async def _seed_profiles(session, event: Event) -> None:
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
        await _seed_interests(session, user, profile.interest_codes)
        plan = await session.scalar(select(EventPlan).where(
            EventPlan.user_id == user.id,
            EventPlan.event_id == event.id,
        ))
        if plan is None:
            plan = EventPlan(user_id=user.id, event_id=event.id)
            session.add(plan)
        plan.status = "planned"
        plan.company_status = "looking"


async def _detach_demo_from_other_events(session) -> None:
    """Demo profiles live only on the demo event; drop what older versions left elsewhere."""
    demo_users = select(User.id).where(User.max_user_id.in_([profile.max_user_id for profile in DEMO_PROFILES]))
    stray_plans = select(EventPlan.id).where(EventPlan.user_id.in_(demo_users), EventPlan.event_id != DEMO_EVENT_ID)
    await session.execute(
        update(CompanionInterest)
        .where(or_(CompanionInterest.sender_plan_id.in_(stray_plans), CompanionInterest.recipient_plan_id.in_(stray_plans)))
        .values(status="withdrawn")
    )
    await session.execute(
        update(Match)
        .where(Match.event_id != DEMO_EVENT_ID, or_(Match.first_user_id.in_(demo_users), Match.second_user_id.in_(demo_users)))
        .values(status="closed")
    )
    await session.execute(
        update(EventPlan)
        .where(EventPlan.id.in_(stray_plans))
        .values(status="cancelled", company_status="not_looking")
    )


async def _seed_team_profiles(session, event: Event) -> int:
    """Add opted-in team profiles to the fixed demo event as real candidates."""
    max_user_ids = _team_demo_user_ids()
    if not max_user_ids:
        return 0
    users = list((await session.scalars(select(User).where(
        User.max_user_id.in_(max_user_ids),
        User.profile_status == "active",
        User.onboarding_step == "complete",
    ))).all())
    for user in users:
        plan = await session.scalar(select(EventPlan).where(
            EventPlan.user_id == user.id,
            EventPlan.event_id == event.id,
        ))
        if plan is None:
            plan = EventPlan(user_id=user.id, event_id=event.id)
            session.add(plan)
        plan.status = "planned"
        plan.company_status = "looking"
    return len(users)


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
            await _detach_demo_from_other_events(session)
            team_profiles = await _seed_team_profiles(session, event)
            await session.commit()
            print(f"Fixed demo match is ready for event {event.id}; team profiles={team_profiles}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
