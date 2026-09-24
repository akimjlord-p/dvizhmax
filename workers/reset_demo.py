"""Clear all social state of the fixed /demo event and seed it again.

Real users lose their plans, reactions, likes, views and matches on the demo
event only. Demo profiles and opted-in team profiles are re-armed as looking.
Run with: docker compose --profile tools run --rm demo-reset
"""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from sqlalchemy import delete, func, select

from infrastructure.db.models import City, Event
from infrastructure.db.repositories.demo import DEMO_EVENT_ID
from infrastructure.db.session import create_async_database_engine, create_session_factory
from infrastructure.db.social_models import (
    CompanionInterest,
    CompanionView,
    EventPlan,
    EventReaction,
    Match,
    Notification,
    NotificationInterest,
)
from workers.demo_seed import _demo_event, _seed_profiles, _seed_team_profiles


async def reset_demo_event() -> dict[str, int]:
    """Return the number of deleted rows per table."""
    engine = create_async_database_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            if await session.get(Event, DEMO_EVENT_ID) is None:
                print("Demo event is not seeded yet; nothing to reset.")
                return {}

            async def count(model, *conditions) -> int:
                return int(await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0)

            demo_interest_ids = select(CompanionInterest.id).where(CompanionInterest.event_id == DEMO_EVENT_ID)
            deleted = {
                "companion_views": await count(CompanionView, CompanionView.event_id == DEMO_EVENT_ID),
                "companion_interests": await count(CompanionInterest, CompanionInterest.event_id == DEMO_EVENT_ID),
                "matches": await count(Match, Match.event_id == DEMO_EVENT_ID),
                "event_reactions": await count(EventReaction, EventReaction.event_id == DEMO_EVENT_ID),
                "event_plans": await count(EventPlan, EventPlan.event_id == DEMO_EVENT_ID),
            }

            # Children first: every social row below references a demo plan.
            await session.execute(delete(NotificationInterest).where(
                NotificationInterest.companion_interest_id.in_(demo_interest_ids)
            ))
            await session.execute(delete(Notification).where(Notification.event_id == DEMO_EVENT_ID))
            await session.execute(delete(CompanionView).where(CompanionView.event_id == DEMO_EVENT_ID))
            await session.execute(delete(CompanionInterest).where(CompanionInterest.event_id == DEMO_EVENT_ID))
            await session.execute(delete(Match).where(Match.event_id == DEMO_EVENT_ID))
            await session.execute(delete(EventReaction).where(EventReaction.event_id == DEMO_EVENT_ID))
            await session.execute(delete(EventPlan).where(EventPlan.event_id == DEMO_EVENT_ID))

            city = await session.scalar(select(City).where(City.name == "Москва").limit(1))
            event = await _demo_event(session, city)
            await _seed_profiles(session, event)
            team_profiles = await _seed_team_profiles(session, event)
            await session.commit()
            print(
                "Demo event reset: "
                + ", ".join(f"{table}={rows}" for table, rows in deleted.items())
                + f"; team profiles={team_profiles}"
            )
            return deleted
    finally:
        await engine.dispose()


async def main() -> None:
    load_dotenv()
    await reset_demo_event()


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
