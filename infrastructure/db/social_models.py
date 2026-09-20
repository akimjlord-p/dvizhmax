"""User profiles, recommendations, companion search and outgoing notifications.

Business workflows are deliberately not implemented in these persistence models.
"""
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey,
    ForeignKeyConstraint, Index, Integer, Numeric, SmallInteger, String, Text,
    Time, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, Timestamps, UUIDPrimaryKey


class User(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("age BETWEEN 1 AND 120", name="age_range"),
        CheckConstraint("profile_status IN ('guest', 'draft', 'active', 'hidden')", name="profile_status"),
        CheckConstraint(
            "profile_status != 'active' OR (name IS NOT NULL AND length(trim(name)) > 0 "
            "AND gender IS NOT NULL AND length(trim(gender)) > 0 AND age IS NOT NULL "
            "AND city_id IS NOT NULL)",
            name="active_profile_complete",
        ),
    )

    max_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    max_username: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    gender: Mapped[str | None] = mapped_column(String(30))
    age: Mapped[int | None] = mapped_column(SmallInteger)
    photo_url: Mapped[str | None] = mapped_column(Text)
    photo_attachment: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    city_id: Mapped[UUID | None] = mapped_column(ForeignKey("cities.id"), index=True)
    profile_status: Mapped[str] = mapped_column(String(20), server_default="guest")
    onboarding_step: Mapped[str | None] = mapped_column(String(50))
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    tag_weights: Mapped[list["UserTagWeight"]] = relationship(back_populates="user", lazy="raise", passive_deletes="all")
    event_reactions: Mapped[list["EventReaction"]] = relationship(back_populates="user", lazy="raise", passive_deletes="all")
    plans: Mapped[list["EventPlan"]] = relationship(back_populates="user", lazy="raise", passive_deletes="all")


class UserConsent(UUIDPrimaryKey, Base):
    __tablename__ = "user_consents"
    __table_args__ = (
        CheckConstraint("revoked_at >= accepted_at", name="time_order"),
        Index("uq_user_consents_current", "user_id", "document_version", unique=True,
              postgresql_where=text("revoked_at IS NULL")),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    document_version: Mapped[str] = mapped_column(String(100))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserTagWeight(Timestamps, Base):
    __tablename__ = "user_tag_weights"
    __table_args__ = (CheckConstraint("initial_weight >= 0", name="initial_nonnegative"),)

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    tag_id: Mapped[UUID] = mapped_column(ForeignKey("tags.id"), primary_key=True, index=True)
    initial_weight: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default=text("0"))
    reaction_weight: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default=text("0"))
    user: Mapped[User] = relationship(back_populates="tag_weights", lazy="raise")


class EventReaction(Timestamps, Base):
    __tablename__ = "event_reactions"
    __table_args__ = (
        CheckConstraint("reaction IN ('like', 'dislike', 'skip')", name="reaction"),
        Index("ix_event_reactions_user_reaction", "user_id", "reaction"),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), primary_key=True, index=True)
    reaction: Mapped[str] = mapped_column(String(10))
    user: Mapped[User] = relationship(back_populates="event_reactions", lazy="raise")


class EventPlan(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "event_plans"
    __table_args__ = (
        UniqueConstraint("user_id", "event_id"),
        UniqueConstraint("id", "event_id"),
        CheckConstraint("status IN ('planned', 'cancelled', 'completed')", name="status"),
        CheckConstraint("company_status IN ('not_looking', 'looking', 'found')", name="company_status"),
        CheckConstraint("company_status != 'looking' OR status = 'planned'", name="search_requires_plan"),
        CheckConstraint("planned_time IS NULL OR planned_date IS NOT NULL", name="time_requires_date"),
        Index("ix_event_plans_search", "event_id", "planned_date", postgresql_where=text("status = 'planned' AND company_status = 'looking'")),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    planned_date: Mapped[date | None] = mapped_column(Date)
    planned_time: Mapped[time | None] = mapped_column(Time)
    status: Mapped[str] = mapped_column(String(20), server_default="planned")
    company_status: Mapped[str] = mapped_column(String(20), server_default="not_looking")
    user: Mapped[User] = relationship(back_populates="plans", lazy="raise")


class CompanionView(Base):
    __tablename__ = "companion_views"
    __table_args__ = (
        CheckConstraint("viewer_id != shown_user_id", name="not_self"),
        Index("ix_companion_views_shown_user", "shown_user_id"),
    )

    viewer_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), primary_key=True)
    shown_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    shown_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompanionInterest(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "companion_interests"
    __table_args__ = (
        UniqueConstraint("sender_plan_id", "recipient_plan_id"),
        ForeignKeyConstraint(["sender_plan_id", "event_id"], ["event_plans.id", "event_plans.event_id"]),
        ForeignKeyConstraint(["recipient_plan_id", "event_id"], ["event_plans.id", "event_plans.event_id"]),
        CheckConstraint("sender_plan_id != recipient_plan_id", name="not_self"),
        CheckConstraint("status IN ('active', 'withdrawn')", name="status"),
        Index("ix_companion_interests_incoming", "recipient_plan_id", "status", "viewed_at", "announced_at"),
        Index("ix_companion_interests_event", "event_id"),
    )

    sender_plan_id: Mapped[UUID] = mapped_column()
    recipient_plan_id: Mapped[UUID] = mapped_column()
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    announced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Match(UUIDPrimaryKey, Base):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("event_id", "first_user_id", "second_user_id"),
        ForeignKeyConstraint(["first_user_id", "event_id"], ["event_plans.user_id", "event_plans.event_id"]),
        ForeignKeyConstraint(["second_user_id", "event_id"], ["event_plans.user_id", "event_plans.event_id"]),
        CheckConstraint("first_user_id < second_user_id", name="ordered_users"),
        CheckConstraint("status IN ('active', 'closed')", name="status"),
        Index("ix_matches_first_user", "first_user_id"),
        Index("ix_matches_second_user", "second_user_id"),
    )

    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    first_user_id: Mapped[UUID] = mapped_column()
    second_user_id: Mapped[UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserBlock(Base):
    __tablename__ = "user_blocks"
    __table_args__ = (CheckConstraint("blocker_id != blocked_id", name="not_self"),)

    blocker_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    blocked_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint("type IN ('interests_digest', 'match')", name="type"),
        CheckConstraint("status IN ('pending', 'processing', 'sent', 'failed', 'cancelled')", name="status"),
        CheckConstraint("attempt_count >= 0", name="attempt_nonnegative"),
        CheckConstraint("(type = 'match' AND match_id IS NOT NULL) OR (type = 'interests_digest' AND match_id IS NULL)", name="match_required"),
        CheckConstraint("status != 'sent' OR sent_at IS NOT NULL", name="sent_timestamp"),
        UniqueConstraint("match_id", "recipient_id"),
        Index("uq_notifications_pending_digest", "recipient_id", "event_id", unique=True,
              postgresql_where=text("type = 'interests_digest' AND status IN ('pending', 'processing')")),
        Index("ix_notifications_due", "next_attempt_at", postgresql_where=text("status = 'pending'")),
        Index("ix_notifications_lease", "locked_until", postgresql_where=text("status = 'processing'")),
    )

    recipient_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    match_id: Mapped[UUID | None] = mapped_column(ForeignKey("matches.id"))
    type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    deduplication_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    max_message_id: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationInterest(Base):
    __tablename__ = "notification_interests"

    notification_id: Mapped[UUID] = mapped_column(ForeignKey("notifications.id"), primary_key=True)
    companion_interest_id: Mapped[UUID] = mapped_column(ForeignKey("companion_interests.id"), primary_key=True, unique=True)
