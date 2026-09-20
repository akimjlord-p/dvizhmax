"""Catalog persistence. No import, classification or bot behavior belongs here."""
from datetime import datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Double, ForeignKey, ForeignKeyConstraint,
    Index, Integer, Numeric, SmallInteger, String, Text, Time, UniqueConstraint,
    func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Timestamps, UUIDPrimaryKey


class City(UUIDPrimaryKey, Base):
    __tablename__ = "cities"

    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(100))
    source_codes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class Place(UUIDPrimaryKey, Base):
    __tablename__ = "places"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude_range"),
    )

    city_id: Mapped[UUID] = mapped_column(ForeignKey("cities.id"), index=True)
    source: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)


class Event(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("price_min >= 0", name="price_nonnegative"),
        CheckConstraint("age_min >= 0", name="age_nonnegative"),
        CheckConstraint("data_status IN ('current', 'uncertain', 'unavailable')", name="data_status"),
        CheckConstraint("tagging_status IN ('pending', 'processing', 'done', 'failed')", name="tagging_status"),
        Index("ix_events_city_status", "city_id", "data_status"),
    )

    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    city_id: Mapped[UUID] = mapped_column(ForeignKey("cities.id"))
    place_id: Mapped[UUID | None] = mapped_column(ForeignKey("places.id"), index=True)
    price_text: Mapped[str | None] = mapped_column(Text)
    price_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    is_free: Mapped[bool | None] = mapped_column(Boolean)
    age_min: Mapped[int | None] = mapped_column(SmallInteger)
    data_status: Mapped[str] = mapped_column(String(20), server_default="current")
    tagging_status: Mapped[str] = mapped_column(String(20), server_default="pending", index=True)
    tagging_operation_id: Mapped[str | None] = mapped_column(Text)
    tagging_error: Mapped[str | None] = mapped_column(Text)


class EventSource(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "event_sources"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("uq_event_sources_primary", "event_id", unique=True, postgresql_where=text("is_primary")),
        CheckConstraint("last_http_status BETWEEN 100 AND 599", name="http_status"),
    )

    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    source: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_http_status: Mapped[int | None] = mapped_column(SmallInteger)


class EventImage(UUIDPrimaryKey, Base):
    __tablename__ = "event_images"
    __table_args__ = (
        UniqueConstraint("event_source_id", "url"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
        Index("ix_event_images_source_position", "event_source_id", "position"),
    )

    event_source_id: Mapped[UUID] = mapped_column(ForeignKey("event_sources.id"))
    url: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    credit_name: Mapped[str | None] = mapped_column(Text)
    credit_url: Mapped[str | None] = mapped_column(Text)
    max_attachment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class EventSchedule(UUIDPrimaryKey, Base):
    __tablename__ = "event_schedules"
    __table_args__ = (
        CheckConstraint("ends_at >= starts_at", name="period_order"),
    )

    event_source_id: Mapped[UUID] = mapped_column(ForeignKey("event_sources.id"), index=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    is_startless: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_endless: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    use_place_schedule: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    recurrence: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSONB)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Tag(UUIDPrimaryKey, Base):
    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("id", "kind"),
        CheckConstraint("kind IN ('primary', 'secondary')", name="kind"),
    )

    code: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    show_in_onboarding: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class EventTag(Base):
    __tablename__ = "event_tags"
    __table_args__ = (
        ForeignKeyConstraint(["tag_id", "kind"], ["tags.id", "tags.kind"]),
        Index("uq_event_tags_primary", "event_id", unique=True, postgresql_where=text("kind = 'primary'")),
        Index("ix_event_tags_tag_id", "tag_id"),
    )

    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), primary_key=True)
    tag_id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))


class ImportRun(UUIDPrimaryKey, Base):
    __tablename__ = "import_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status"),
        CheckConstraint("created_count >= 0 AND updated_count >= 0", name="counts_nonnegative"),
        CheckConstraint("finished_at >= started_at", name="time_order"),
    )

    source: Mapped[str] = mapped_column(String(50))
    city_id: Mapped[UUID] = mapped_column(ForeignKey("cities.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), server_default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    updated_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
