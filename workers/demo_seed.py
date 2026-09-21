"""Create one explicit test companion for the /demo MAX command."""
from __future__ import annotations

import asyncio

from dotenv import load_dotenv
from sqlalchemy import select

from infrastructure.db.models import City
from infrastructure.db.repositories.demo import DEMO_MAX_USER_ID
from infrastructure.db.repositories.feed import FeedRepository
from infrastructure.db.session import create_async_database_engine, create_session_factory
from infrastructure.db.social_models import EventPlan, User


async def main() -> None:
    load_dotenv()
    engine = create_async_database_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            city = await session.scalar(select(City).where(City.name == "Москва").limit(1))
            if city is None:
                raise RuntimeError("Сначала загрузите каталог Москвы")
            demo_user = await session.scalar(select(User).where(User.max_user_id == DEMO_MAX_USER_ID))
            if demo_user is None:
                demo_user = User(
                    max_user_id=DEMO_MAX_USER_ID,
                    max_username="dvizhmax_demo",
                    name="Демо Катя",
                    gender="female",
                    age=24,
                    description="Тестовая анкета для демонстрации мэтча.",
                    city_id=city.id,
                    profile_status="active",
                    onboarding_step="complete",
                )
                session.add(demo_user)
                await session.flush()

            plan = await session.scalar(
                select(EventPlan).where(
                    EventPlan.user_id == demo_user.id,
                    EventPlan.status == "planned",
                    EventPlan.company_status == "looking",
                ).limit(1)
            )
            if plan is None:
                cards = await FeedRepository(session).next_cards(demo_user.id, limit=1)
                if not cards:
                    raise RuntimeError("В афише нет актуального размеченного мероприятия для демо")
                plan = EventPlan(user_id=demo_user.id, event_id=cards[0].id, status="planned", company_status="looking")
                session.add(plan)
            await session.commit()
            print(f"Demo match is ready for event {plan.event_id}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
