"""Refresh KudaGo events and tag new cards twice a day."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Final

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from infrastructure.db.repositories import CatalogRepository
from infrastructure.db.session import create_async_database_engine, create_session_factory
from integrations.kudago import KudaGoClient, normalize_event
from integrations.yandex_tagger import PRIMARY_TAGS, SECONDARY_TAGS, YandexTagger
from integrations.yandex_tagger import _TAG_LABELS as TAG_LABELS


LOGGER: Final = logging.getLogger(__name__)
REFRESH_INTERVAL_SECONDS: Final = 12 * 60 * 60


@dataclass(frozen=True, slots=True)
class CityImportSettings:
    location: str
    name: str
    timezone: str


@dataclass(frozen=True, slots=True)
class CatalogWorkerSettings:
    cities: tuple[CityImportSettings, ...]
    interval_seconds: int = REFRESH_INTERVAL_SECONDS

    @classmethod
    def from_env(cls) -> "CatalogWorkerSettings":
        load_dotenv()
        interval = int(os.getenv("CATALOG_REFRESH_INTERVAL_SECONDS", str(REFRESH_INTERVAL_SECONDS)))
        if interval <= 0:
            raise ValueError("CATALOG_REFRESH_INTERVAL_SECONDS must be positive")
        return cls(cities=_cities_from_env(), interval_seconds=interval)


def _cities_from_env() -> tuple[CityImportSettings, ...]:
    raw_cities = os.getenv("KUDAGO_CITIES", "").strip()
    if not raw_cities:
        return (
            CityImportSettings(
                location=os.getenv("KUDAGO_LOCATION", "msk").strip() or "msk",
                name=os.getenv("KUDAGO_CITY_NAME", "Москва").strip() or "Москва",
                timezone=os.getenv("KUDAGO_CITY_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
            ),
        )
    try:
        data = json.loads(raw_cities)
    except json.JSONDecodeError as exc:
        raise ValueError("KUDAGO_CITIES must be a JSON array") from exc
    if not isinstance(data, list) or not data:
        raise ValueError("KUDAGO_CITIES must contain at least one city")

    cities: list[CityImportSettings] = []
    locations: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each KUDAGO_CITIES item must be an object")
        location = str(item.get("location", "")).strip()
        name = str(item.get("name", "")).strip()
        timezone = str(item.get("timezone", "")).strip()
        if not location or not name or not timezone:
            raise ValueError("Each KUDAGO_CITIES item needs location, name and timezone")
        if location in locations:
            raise ValueError(f"KUDAGO_CITIES has duplicate location: {location}")
        locations.add(location)
        cities.append(CityImportSettings(location=location, name=name, timezone=timezone))
    return tuple(cities)


def _tag_definitions() -> tuple[tuple[str, str, str], ...]:
    return tuple((code, TAG_LABELS[code], "primary") for code in PRIMARY_TAGS) + tuple(
        (code, TAG_LABELS[code], "secondary") for code in SECONDARY_TAGS
    )


async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    city_settings: CityImportSettings,
) -> tuple[int, int, int]:
    """Return created, updated and failed card counts for one source refresh."""
    async with session_factory() as session:
        repository = CatalogRepository(session)
        city = await repository.ensure_city(
            name=city_settings.name,
            timezone_name=city_settings.timezone,
            source="kudago",
            source_code=city_settings.location,
        )
        await repository.ensure_tags(_tag_definitions())
        run = await repository.start_import_run(source="kudago", city_id=city.id)
        await session.commit()
        city_id, run_id = city.id, run.id

    created = updated = failed = 0
    run_error: Exception | None = None
    tagger: YandexTagger | None
    try:
        tagger = YandexTagger()
    except Exception:
        tagger = None
        LOGGER.exception("Yandex tagger is unavailable; new cards will remain pending")

    try:
        async with KudaGoClient() as kudago:
            async for payload in kudago.iter_events(location=city_settings.location):
                try:
                    draft = normalize_event(payload, city_id=city_id)
                    async with session_factory() as session:
                        result = await CatalogRepository(session).upsert_event(draft)
                        await session.commit()

                    if result.is_new:
                        created += 1
                    else:
                        updated += 1

                    if result.needs_tagging and tagger is not None:
                        try:
                            tags = await tagger.tag_event(draft)
                            async with session_factory() as session:
                                await CatalogRepository(session).save_tags(result.event_id, tags)
                                await session.commit()
                        except Exception as exc:
                            LOGGER.exception("Tagging failed for KudaGo event %s", draft.external_id)
                            async with session_factory() as session:
                                await CatalogRepository(session).mark_tagging_failed(result.event_id, exc)
                                await session.commit()
                except Exception:
                    failed += 1
                    LOGGER.exception("Import failed for one KudaGo card")
    except Exception as exc:
        run_error = exc
        LOGGER.exception("KudaGo refresh failed")
    finally:
        async with session_factory() as session:
            await CatalogRepository(session).finish_import_run(
                run_id,
                status="failed" if run_error else "succeeded",
                created_count=created,
                updated_count=updated,
                error=str(run_error) if run_error else None,
            )
            await session.commit()

    if run_error:
        raise run_error
    return created, updated, failed


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = CatalogWorkerSettings.from_env()
    engine: AsyncEngine = create_async_database_engine()
    session_factory = create_session_factory(engine)
    try:
        while True:
            created = updated = failed = 0
            for city_settings in settings.cities:
                try:
                    city_created, city_updated, city_failed = await run_once(session_factory, city_settings)
                    created += city_created
                    updated += city_updated
                    failed += city_failed
                except Exception:
                    LOGGER.exception("Catalog refresh failed for city %s", city_settings.location)
            LOGGER.info("Catalog refresh finished: created=%s updated=%s failed=%s", created, updated, failed)
            await asyncio.sleep(settings.interval_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
