"""Persistence for KudaGo event drafts and AI event tags."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from integrations.kudago import EventDraft
from integrations.yandex_tagger import TaggingResult

from ..models import (
    City,
    Event,
    EventImage,
    EventSchedule,
    EventSource,
    EventTag,
    ImportRun,
    Place,
    Tag,
)


@dataclass(frozen=True, slots=True)
class EventUpsertResult:
    event_id: UUID
    is_new: bool
    needs_tagging: bool


class CatalogRepository:
    """Keeps source data current without rewriting old event text or tags."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_city(self, *, name: str, timezone_name: str, source: str, source_code: str) -> City:
        city = await self.session.scalar(
            select(City).where(City.source_codes[source].astext == source_code).limit(1)
        )
        if city is None:
            city = City(name=name, timezone=timezone_name, source_codes={source: source_code})
            self.session.add(city)
            await self.session.flush()
        return city

    async def ensure_tags(self, definitions: Iterable[tuple[str, str, str]]) -> None:
        codes = [code for code, _, _ in definitions]
        if not codes:
            return
        existing = set(
            (await self.session.scalars(select(Tag.code).where(Tag.code.in_(codes)))).all()
        )
        for code, name, kind in definitions:
            if code not in existing:
                self.session.add(
                    Tag(
                        code=code,
                        name=name,
                        kind=kind,
                        description=name,
                        show_in_onboarding=kind == "primary",
                    )
                )
        await self.session.flush()

    async def start_import_run(self, *, source: str, city_id: UUID) -> ImportRun:
        run = ImportRun(source=source, city_id=city_id, status="running")
        self.session.add(run)
        await self.session.flush()
        return run

    async def finish_import_run(
        self,
        run_id: UUID,
        *,
        status: str,
        created_count: int,
        updated_count: int,
        error: str | None = None,
    ) -> None:
        run = await self.session.get(ImportRun, run_id)
        if run is None:
            raise RuntimeError(f"Import run {run_id} does not exist")
        run.status = status
        run.finished_at = datetime.now(timezone.utc)
        run.created_count = created_count
        run.updated_count = updated_count
        run.error = error[:4000] if error else None

    async def mark_unseen_events_uncertain(
        self,
        *,
        source: str,
        city_id: UUID,
        seen_after: datetime,
    ) -> None:
        """Mark cards absent from a completed source scan as not confirmed."""
        unseen_event_ids = select(EventSource.event_id).where(
            EventSource.source == source,
            EventSource.is_primary.is_(True),
            EventSource.last_success_at < seen_after,
        )
        await self.session.execute(
            update(Event)
            .where(Event.city_id == city_id, Event.id.in_(unseen_event_ids), Event.data_status != "unavailable")
            .values(data_status="uncertain")
        )

    async def mark_stale_events_unavailable(
        self,
        *,
        source: str,
        city_id: UUID,
        stale_before: datetime,
    ) -> None:
        """Stop recommending cards the source has not confirmed for days."""
        stale_event_ids = select(EventSource.event_id).where(
            EventSource.source == source,
            EventSource.is_primary.is_(True),
            or_(EventSource.last_success_at.is_(None), EventSource.last_success_at < stale_before),
        )
        await self.session.execute(
            update(Event)
            .where(Event.city_id == city_id, Event.id.in_(stale_event_ids), Event.data_status != "unavailable")
            .values(data_status="unavailable")
        )

    async def upsert_event(self, draft: EventDraft) -> EventUpsertResult:
        now = datetime.now(timezone.utc)
        source = await self.session.scalar(
            select(EventSource).where(
                EventSource.source == draft.source,
                EventSource.external_id == draft.external_id,
            )
        )
        place_id = await self._upsert_place(draft)

        if source is None:
            event = Event(
                title=draft.title,
                description=draft.description,
                city_id=draft.city_id,
                place_id=place_id,
                price_text=draft.price_text,
                price_min=draft.price_min,
                currency=draft.currency,
                is_free=draft.is_free,
                age_min=draft.age_min,
                data_status="current",
                tagging_status="pending",
            )
            self.session.add(event)
            await self.session.flush()
            source = EventSource(
                event_id=event.id,
                source=draft.source,
                external_id=draft.external_id,
                source_url=draft.source_url,
                is_primary=True,
                raw_payload=draft.raw_payload,
                last_checked_at=now,
                last_success_at=now,
                last_http_status=200,
            )
            self.session.add(source)
            await self.session.flush()
            await self._replace_source_media(source.id, draft)
            return EventUpsertResult(event.id, is_new=True, needs_tagging=True)

        event = await self.session.get(Event, source.event_id)
        if event is None:
            raise RuntimeError(f"Event source {source.id} points to a missing event")

        # Text and event tags are immutable after the first import. The source
        # still keeps the latest raw payload for traceability.
        event.city_id = draft.city_id
        event.place_id = place_id
        event.price_text = draft.price_text
        event.price_min = draft.price_min
        event.currency = draft.currency
        event.is_free = draft.is_free
        event.age_min = draft.age_min
        event.data_status = "current"
        source.source_url = draft.source_url
        source.raw_payload = draft.raw_payload
        source.last_checked_at = now
        source.last_success_at = now
        source.last_http_status = 200
        await self._replace_source_media(source.id, draft)
        return EventUpsertResult(
            event.id,
            is_new=False,
            needs_tagging=event.tagging_status in {"pending", "failed"},
        )

    async def save_tags(self, event_id: UUID, result: TaggingResult) -> None:
        codes = (*result.primary, *result.secondary)
        tags = {
            tag.code: tag
            for tag in (await self.session.scalars(select(Tag).where(Tag.code.in_(codes)))).all()
        }
        missing = set(codes) - set(tags)
        if missing:
            raise RuntimeError(f"Tag dictionary is missing codes: {', '.join(sorted(missing))}")

        await self.session.execute(delete(EventTag).where(EventTag.event_id == event_id))
        for code in result.primary:
            self.session.add(EventTag(event_id=event_id, tag_id=tags[code].id, kind="primary"))
        for code in result.secondary:
            self.session.add(EventTag(event_id=event_id, tag_id=tags[code].id, kind="secondary"))

        event = await self.session.get(Event, event_id)
        if event is None:
            raise RuntimeError(f"Event {event_id} does not exist")
        event.tagging_status = "done"
        event.tagging_error = None
        event.tagging_operation_id = result.model_version

    async def mark_tagging_failed(self, event_id: UUID, error: Exception) -> None:
        event = await self.session.get(Event, event_id)
        if event is None:
            raise RuntimeError(f"Event {event_id} does not exist")
        event.tagging_status = "failed"
        event.tagging_error = str(error)[:4000]

    async def _upsert_place(self, draft: EventDraft) -> UUID | None:
        if draft.place is None:
            return None
        place = await self.session.scalar(
            select(Place).where(
                Place.source == draft.place.source,
                Place.external_id == draft.place.external_id,
            )
        )
        if place is None:
            place = Place(
                city_id=draft.city_id,
                source=draft.place.source,
                external_id=draft.place.external_id,
                name=draft.place.name,
                address=draft.place.address,
                latitude=draft.place.latitude,
                longitude=draft.place.longitude,
            )
            self.session.add(place)
            await self.session.flush()
        else:
            place.city_id = draft.city_id
            place.name = draft.place.name
            place.address = draft.place.address
            place.latitude = draft.place.latitude
            place.longitude = draft.place.longitude
        return place.id

    async def _replace_source_media(self, source_id: UUID, draft: EventDraft) -> None:
        await self.session.execute(delete(EventImage).where(EventImage.event_source_id == source_id))
        await self.session.execute(delete(EventSchedule).where(EventSchedule.event_source_id == source_id))
        self.session.add_all(
            EventImage(
                event_source_id=source_id,
                url=image.url,
                position=image.position,
                credit_name=image.credit_name,
                credit_url=image.credit_url,
            )
            for image in draft.images
        )
        self.session.add_all(
            EventSchedule(
                event_source_id=source_id,
                starts_at=schedule.starts_at,
                ends_at=schedule.ends_at,
                start_time=schedule.start_time,
                end_time=schedule.end_time,
                is_startless=schedule.is_startless,
                is_endless=schedule.is_endless,
                use_place_schedule=schedule.use_place_schedule,
                recurrence=schedule.recurrence,
                raw_payload=schedule.raw_payload,
            )
            for schedule in draft.schedules
        )
