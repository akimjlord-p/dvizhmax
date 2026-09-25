"""Delete real user profiles and all social data linked to them.

The fixed demo profiles and their single /demo event are kept intact.
Run with: docker compose --profile tools run --rm profile-reset
"""
from __future__ import annotations

import asyncio

from dotenv import load_dotenv
from sqlalchemy import delete, func, select

from infrastructure.db.repositories.demo import DEMO_EVENT_ID, DEMO_MAX_USER_IDS
from infrastructure.db.session import create_async_database_engine, create_session_factory
from infrastructure.db.social_models import (
    CompanionInterest,
    CompanionView,
    EventPlan,
    EventReaction,
    Match,
    MatchContact,
    Notification,
    NotificationInterest,
    User,
    UserBlock,
    UserConsent,
    UserTagWeight,
)


async def reset_registered_profiles() -> int:
    """Clear every non-demo user and return the number of deleted users."""
    engine = create_async_database_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            real_user_ids = list((await session.scalars(
                select(User.id).where(User.max_user_id.not_in(DEMO_MAX_USER_IDS))
            )).all())
            demo_user_ids = list((await session.scalars(
                select(User.id).where(User.max_user_id.in_(DEMO_MAX_USER_IDS))
            )).all())
            if not real_user_ids:
                print("No registered profiles to reset.")
                return 0

            deleted_users = await session.scalar(
                select(func.count()).select_from(User).where(User.id.in_(real_user_ids))
            )

            # Social rows can reference both a real profile and a demo profile.
            # Clearing this state first avoids dangling foreign keys and makes a
            # subsequent demo or ordinary flow start from a clean slate.
            await session.execute(delete(NotificationInterest))
            await session.execute(delete(Notification))
            await session.execute(delete(CompanionView))
            await session.execute(delete(CompanionInterest))
            await session.execute(delete(MatchContact))
            await session.execute(delete(Match))
            await session.execute(delete(UserBlock))

            await session.execute(delete(EventReaction).where(EventReaction.user_id.in_(real_user_ids)))
            await session.execute(delete(UserTagWeight).where(UserTagWeight.user_id.in_(real_user_ids)))
            await session.execute(delete(UserConsent).where(UserConsent.user_id.in_(real_user_ids)))
            await session.execute(delete(EventPlan).where(
                EventPlan.user_id.in_(demo_user_ids),
                EventPlan.event_id != DEMO_EVENT_ID,
            ))
            await session.execute(delete(EventPlan).where(EventPlan.user_id.in_(real_user_ids)))
            await session.execute(delete(User).where(User.id.in_(real_user_ids)))
            await session.commit()
            print(f"Reset {deleted_users or 0} registered profile(s).")
            return int(deleted_users or 0)
    finally:
        await engine.dispose()


async def main() -> None:
    load_dotenv()
    await reset_registered_profiles()


if __name__ == "__main__":
    asyncio.run(main())
