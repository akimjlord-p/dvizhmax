"""Queries and state changes for the event recommendation feed."""
from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import City, Event, EventImage, EventSchedule, EventSource, EventTag, Place, Tag
from ..social_models import CompanionInterest, EventPlan, EventReaction, Match, User, UserTagWeight
from .onboarding import OnboardingError


LIKE_WEIGHT_DELTA = Decimal("0.1")
WANT_TO_GO_WEIGHT_DELTA = Decimal("0.5")
SECONDARY_TAG_MULTIPLIER = Decimal("0.4")


@dataclass(frozen=True, slots=True)
class EventCard:
    id: UUID
    title: str
    description: str | None
    city_timezone: str
    place_name: str | None
    place_address: str | None
    price_text: str | None
    is_free: bool | None
    data_status: str
    source_url: str
    starts_at: datetime | None
    ends_at: datetime | None
    schedule_state: str
    image_url: str | None
    image_id: UUID | None
    max_attachment: dict[str, Any] | None
    primary_codes: frozenset[str]
    tag_codes: frozenset[str]
    tag_kinds: tuple[tuple[str, str], ...]
    score: Decimal = Decimal("0")
    plan_id: UUID | None = None
    company_status: str | None = None
    is_liked: bool = False
    pending_likes: int = 0


@dataclass(frozen=True, slots=True)
class WantToGoResult:
    plan_id: UUID
    was_created: bool


class FeedRepository:
    """Produces unseen cards and records the preference signals they create."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def next_card(self, user_id: UUID) -> EventCard | None:
        cards, weights, reaction_count, history = await self._candidate_cards(user_id)
        ranked = rank_cards(
            cards,
            weights=weights,
            reaction_count=reaction_count,
            prior_primary_tags=history,
        )
        return ranked[0] if ranked else None

    async def next_cards(
        self,
        user_id: UUID,
        *,
        limit: int,
        excluded_event_ids: Iterable[UUID] = (),
    ) -> list[EventCard]:
        """Build an ordered page of unseen cards for a feed buffer."""
        if limit <= 0:
            return []
        cards, weights, reaction_count, history = await self._candidate_cards(
            user_id,
            excluded_event_ids=excluded_event_ids,
        )
        return rank_cards(
            cards,
            weights=weights,
            reaction_count=reaction_count,
            prior_primary_tags=history,
        )[:limit]

    async def buffered_card(
        self,
        user_id: UUID,
        event_id: UUID,
        *,
        include_reacted: bool = False,
    ) -> EventCard | None:
        cards, _, _, _ = await self._candidate_cards(
            user_id,
            event_ids=(event_id,),
            include_reacted=include_reacted,
        )
        return cards[0] if cards else None

    async def liked_cards(self, user_id: UUID) -> list[EventCard]:
        user = await self._require_user(user_id)
        event_ids = list(
            (
                await self.session.scalars(
            select(EventReaction.event_id)
            .where(
                EventReaction.user_id == user.id,
                EventReaction.reaction == "like",
                ~exists(
                    select(EventPlan.id).where(
                        EventPlan.user_id == user.id,
                        EventPlan.event_id == EventReaction.event_id,
                        EventPlan.status == "planned",
                    )
                ),
            )
            .order_by(EventReaction.updated_at.desc())
                )
            ).all()
        )
        if not event_ids:
            return []
        cards, weights, _, _ = await self._candidate_cards(
            user_id,
            event_ids=event_ids,
            include_reacted=True,
        )
        cards_by_id = {card.id: card for card in cards}
        return [
            replace(cards_by_id[event_id], score=score_card(cards_by_id[event_id], weights))
            for event_id in event_ids
            if event_id in cards_by_id
        ]

    async def liked_card(self, user_id: UUID, event_id: UUID) -> EventCard | None:
        return next(
            (card for card in await self.liked_cards(user_id) if card.id == event_id),
            None,
        )

    async def planned_cards(self, user_id: UUID) -> list[EventCard]:
        user = await self._require_user(user_id)
        plans = list(
            (
                await self.session.scalars(
                    select(EventPlan)
                    .where(EventPlan.user_id == user.id, EventPlan.status == "planned")
                    .order_by(EventPlan.created_at.desc())
                )
            ).all()
        )
        if not plans:
            return []
        cards, weights, _, _ = await self._candidate_cards(
            user_id,
            event_ids=(plan.event_id for plan in plans),
            include_reacted=True,
            feed_only=False,
        )
        cards_by_id = {card.id: card for card in cards}
        return [
            replace(
                cards_by_id[plan.event_id],
                score=score_card(cards_by_id[plan.event_id], weights),
                plan_id=plan.id,
                company_status=plan.company_status,
            )
            for plan in plans
            if plan.event_id in cards_by_id
        ]

    async def record_reaction(self, user_id: UUID, event_id: UUID, reaction: str) -> bool:
        """Record an event reaction once and report whether this callback changed state."""
        if reaction not in {"like", "skip"}:
            raise OnboardingError("Неизвестная реакция")
        user = await self._lock_user(user_id)
        event = await self.session.get(Event, event_id)
        if event is None or event.city_id != user.city_id:
            raise OnboardingError("Это событие больше недоступно")
        inserted = await self.session.scalar(
            insert(EventReaction)
            .values(user_id=user.id, event_id=event_id, reaction=reaction)
            .on_conflict_do_nothing(index_elements=(EventReaction.user_id, EventReaction.event_id))
            .returning(EventReaction.event_id)
        )
        if inserted is None:
            return False
        if reaction == "like":
            await self._change_event_tag_weights(user.id, event_id, LIKE_WEIGHT_DELTA)
        return True

    async def want_to_go(self, user_id: UUID, event_id: UUID) -> WantToGoResult:
        """Create a plan and record implicit positive intent when needed."""
        user = await self._lock_user(user_id)
        event = await self.session.get(Event, event_id)
        reaction = await self.session.get(EventReaction, (user.id, event_id))
        if event is None or (event.city_id != user.city_id and (reaction is None or reaction.reaction != "like")):
            raise OnboardingError("Это событие больше недоступно")
        existing = await self.session.scalar(
            select(EventPlan).where(EventPlan.user_id == user.id, EventPlan.event_id == event_id)
        )
        if existing is not None and existing.status == "planned":
            return WantToGoResult(plan_id=existing.id, was_created=False)
        if existing is not None and existing.status == "completed":
            raise OnboardingError("Этот план уже завершён")
        previous = LIKE_WEIGHT_DELTA if reaction is not None and reaction.reaction == "like" else Decimal("0")
        if reaction is None:
            self.session.add(EventReaction(user_id=user.id, event_id=event_id, reaction="like"))
        elif reaction.reaction != "like":
            raise OnboardingError("Это событие отмечено как «Не моё». Выбери другое в афише")
        plan = existing or EventPlan(user_id=user.id, event_id=event_id)
        plan.status = "planned"
        plan.company_status = "not_looking"
        if existing is None:
            self.session.add(plan)
        await self._change_event_tag_weights(user.id, event_id, WANT_TO_GO_WEIGHT_DELTA - previous)
        await self.session.flush()
        return WantToGoResult(plan_id=plan.id, was_created=True)

    async def remove_like(self, user_id: UUID, event_id: UUID) -> None:
        await self._lock_user(user_id)
        reaction = await self.session.get(EventReaction, (user_id, event_id))
        if reaction is None or reaction.reaction != "like":
            return
        plan = await self.session.scalar(select(EventPlan).where(
            EventPlan.user_id == user_id, EventPlan.event_id == event_id,
            EventPlan.status.in_(("planned", "completed")),
        ))
        # Keep the row as a skip, so removing a like never returns a card to the feed.
        reaction.reaction = "skip"
        if plan is None:
            await self._change_event_tag_weights(user_id, event_id, -LIKE_WEIGHT_DELTA)

    async def cancel_plan(self, user_id: UUID, plan_id: UUID) -> None:
        await self._lock_user(user_id)
        plan = await self.session.get(EventPlan, plan_id)
        if plan is None or plan.user_id != user_id:
            raise OnboardingError("План недоступен")
        if plan.status == "cancelled":
            return
        if plan.status != "planned":
            raise OnboardingError("Этот план уже завершён")
        reaction = await self.session.get(EventReaction, (user_id, plan.event_id))
        remaining = LIKE_WEIGHT_DELTA if reaction is not None and reaction.reaction == "like" else Decimal("0")
        plan.status = "cancelled"
        plan.company_status = "not_looking"
        await self._change_event_tag_weights(user_id, plan.event_id, remaining - WANT_TO_GO_WEIGHT_DELTA)
        await self.session.execute(update(CompanionInterest).where(or_(
            CompanionInterest.sender_plan_id == plan.id,
            CompanionInterest.recipient_plan_id == plan.id,
        )).values(status="withdrawn"))
        await self.session.execute(update(Match).where(
            Match.event_id == plan.event_id,
            or_(Match.first_user_id == user_id, Match.second_user_id == user_id),
        ).values(status="closed"))

    async def set_company_search(self, user_id: UUID, plan_id: UUID, *, looking: bool) -> None:
        user = await self._lock_user(user_id)
        if looking and user.profile_status != "active":
            raise OnboardingError("Для поиска компании сначала заполни анкету")
        plan = await self.session.get(EventPlan, plan_id)
        if plan is None or plan.user_id != user_id or plan.status != "planned":
            raise OnboardingError("Этот план уже неактуален")
        plan.company_status = "looking" if looking else "not_looking"

    async def save_image_attachment(self, image_id: UUID, attachment: dict[str, Any]) -> None:
        image = await self.session.get(EventImage, image_id)
        if image is not None:
            image.max_attachment = attachment

    async def _candidate_cards(
        self,
        user_id: UUID,
        *,
        event_ids: Iterable[UUID] | None = None,
        include_reacted: bool = False,
        feed_only: bool = True,
        excluded_event_ids: Iterable[UUID] = (),
    ) -> tuple[list[EventCard], dict[str, Decimal], int, list[frozenset[str]]]:
        user = await self._require_user(user_id)
        conditions = [Event.city_id == user.city_id]
        if feed_only:
            conditions.extend(
                [
                    Event.data_status.in_(("current", "uncertain")),
                    Event.tagging_status == "done",
                ]
            )
            if user.age is not None:
                conditions.append(or_(Event.age_min.is_(None), Event.age_min <= user.age))
        if event_ids is not None:
            conditions.append(Event.id.in_(tuple(event_ids)))
        excluded_ids = tuple(excluded_event_ids)
        if excluded_ids:
            conditions.append(Event.id.not_in(excluded_ids))
        if not include_reacted:
            conditions.append(
                ~exists(
                    select(EventReaction.user_id).where(
                        EventReaction.user_id == user.id,
                        EventReaction.event_id == Event.id,
                    )
                )
            )
        rows = (
            await self.session.execute(
                select(Event, Place, EventSource, City)
                .outerjoin(Place, Place.id == Event.place_id)
                .join(EventSource, and_(EventSource.event_id == Event.id, EventSource.is_primary.is_(True)))
                .join(City, City.id == Event.city_id)
                .where(*conditions)
            )
        ).all()
        if not rows:
            return [], {}, 0, []

        events = {event.id: (event, place, source, city) for event, place, source, city in rows}
        event_ids_tuple = tuple(events)
        tags_by_event = await self._tags_by_event(event_ids_tuple)
        images = await self._images_by_event(event_ids_tuple)
        schedules = await self._schedules_by_event(event_ids_tuple)
        weights = await self._weights(user.id)
        liked_ids = set((await self.session.scalars(select(EventReaction.event_id).where(
            EventReaction.user_id == user.id, EventReaction.event_id.in_(event_ids_tuple),
            EventReaction.reaction == "like",
        ))).all()) if include_reacted else set()
        now = datetime.now(timezone.utc)
        cards: list[EventCard] = []
        for event_id, (event, place, source, city) in events.items():
            event_schedules = schedules.get(event_id, [])
            timing = event_timing(event_schedules, now, city.timezone)
            if feed_only and timing is None:
                continue
            tagged = tags_by_event.get(event_id, [])
            primary = frozenset(code for code, kind in tagged if kind == "primary")
            if not primary:
                continue
            image = images.get(event_id)
            cards.append(
                EventCard(
                    id=event.id,
                    title=event.title,
                    description=event.description,
                    city_timezone=city.timezone,
                    place_name=place.name if place else None,
                    place_address=place.address if place else None,
                    price_text=event.price_text,
                    is_free=event.is_free,
                    data_status=event.data_status,
                    source_url=source.source_url,
                    starts_at=timing.starts_at if timing else None,
                    ends_at=timing.ends_at if timing else None,
                    schedule_state=timing.state if timing else "unknown",
                    image_url=image[1] if image else None,
                    image_id=image[0] if image else None,
                    max_attachment=image[2] if image else None,
                    primary_codes=primary,
                    tag_codes=frozenset(code for code, _ in tagged),
                    tag_kinds=tuple(tagged),
                    is_liked=event.id in liked_ids,
                )
            )
        cards = [replace(card, score=score_card(card, weights)) for card in cards]
        reaction_count = await self.session.scalar(
            select(func.count()).select_from(EventReaction).where(EventReaction.user_id == user.id)
        )
        history = await self._recent_primary_tags(user.id)
        return cards, weights, int(reaction_count or 0), history

    async def _tags_by_event(self, event_ids: tuple[UUID, ...]) -> dict[UUID, list[tuple[str, str]]]:
        rows = (
            await self.session.execute(
                select(EventTag.event_id, Tag.code, EventTag.kind)
                .join(Tag, Tag.id == EventTag.tag_id)
                .where(EventTag.event_id.in_(event_ids))
            )
        ).all()
        result: dict[UUID, list[tuple[str, str]]] = {}
        for event_id, code, kind in rows:
            result.setdefault(event_id, []).append((code, kind))
        return result

    async def _images_by_event(
        self,
        event_ids: tuple[UUID, ...],
    ) -> dict[UUID, tuple[UUID, str, dict[str, Any] | None]]:
        rows = (
            await self.session.execute(
                select(EventSource.event_id, EventImage.id, EventImage.url, EventImage.max_attachment)
                .join(EventImage, EventImage.event_source_id == EventSource.id)
                .where(EventSource.event_id.in_(event_ids))
                .order_by(EventSource.event_id, EventImage.position)
            )
        ).all()
        result: dict[UUID, tuple[UUID, str, dict[str, Any] | None]] = {}
        for event_id, image_id, url, max_attachment in rows:
            result.setdefault(event_id, (image_id, url, max_attachment))
        return result

    async def _schedules_by_event(self, event_ids: tuple[UUID, ...]) -> dict[UUID, list[EventSchedule]]:
        rows = (
            await self.session.execute(
                select(EventSource.event_id, EventSchedule)
                .join(EventSchedule, EventSchedule.event_source_id == EventSource.id)
                .where(EventSource.event_id.in_(event_ids))
            )
        ).all()
        result: dict[UUID, list[EventSchedule]] = {}
        for event_id, schedule in rows:
            result.setdefault(event_id, []).append(schedule)
        return result

    async def _weights(self, user_id: UUID) -> dict[str, Decimal]:
        rows = (
            await self.session.execute(
                select(Tag.code, UserTagWeight.initial_weight, UserTagWeight.reaction_weight)
                .join(Tag, Tag.id == UserTagWeight.tag_id)
                .where(UserTagWeight.user_id == user_id)
            )
        ).all()
        return {code: Decimal(initial) + Decimal(reaction) for code, initial, reaction in rows}

    async def _recent_primary_tags(self, user_id: UUID) -> list[frozenset[str]]:
        event_ids = list(
            (
                await self.session.scalars(
                    select(EventReaction.event_id)
                    .where(EventReaction.user_id == user_id)
                    .order_by(EventReaction.updated_at.desc())
                    .limit(2)
                )
            ).all()
        )
        if not event_ids:
            return []
        tagged = await self._tags_by_event(tuple(event_ids))
        return [
            frozenset(code for code, kind in tagged.get(event_id, []) if kind == "primary")
            for event_id in reversed(event_ids)
        ]

    async def _change_event_tag_weights(self, user_id: UUID, event_id: UUID, delta: Decimal) -> None:
        tag_ids = list(
            (
                await self.session.scalars(select(EventTag.tag_id).where(EventTag.event_id == event_id))
            ).all()
        )
        for tag_id in tag_ids:
            await self.session.execute(
                insert(UserTagWeight)
                .values(
                    user_id=user_id,
                    tag_id=tag_id,
                    initial_weight=Decimal("0"),
                    reaction_weight=delta,
                )
                .on_conflict_do_update(
                    index_elements=(UserTagWeight.user_id, UserTagWeight.tag_id),
                    set_={
                        "reaction_weight": UserTagWeight.reaction_weight + delta,
                        "updated_at": func.now(),
                    },
                )
            )

    async def _require_user(self, user_id: UUID) -> User:
        user = await self.session.get(User, user_id)
        if user is None:
            raise OnboardingError("Сначала пройди /start")
        return user

    async def _lock_user(self, user_id: UUID) -> User:
        """Serialize state-changing callbacks for one user until the transaction commits."""
        user = await self.session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            raise OnboardingError("Сначала пройди /start")
        return user


@dataclass(frozen=True, slots=True)
class EventTiming:
    starts_at: datetime | None
    ends_at: datetime | None
    state: str


def event_timing(
    schedules: list[EventSchedule],
    now: datetime,
    timezone_name: str,
) -> EventTiming | None:
    """Return the next concrete session or the current active period."""
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = timezone.utc
    candidates = [
        timing
        for schedule in schedules
        if (timing := _schedule_timing(schedule, now, zone)) is not None
    ]
    if not candidates:
        return None
    active = [timing for timing in candidates if timing.state == "ongoing"]
    if active:
        return min(active, key=lambda timing: timing.ends_at or datetime.max.replace(tzinfo=timezone.utc))
    return min(candidates, key=lambda timing: timing.starts_at or datetime.max.replace(tzinfo=timezone.utc))


def _schedule_timing(schedule: EventSchedule, now: datetime, zone: ZoneInfo) -> EventTiming | None:
    if isinstance(schedule.recurrence, list) and schedule.recurrence:
        recurring = _recurring_timing(schedule, now, zone)
        if recurring is not None:
            return recurring

    starts_at, ends_at = schedule.starts_at, schedule.ends_at
    is_open_ended = schedule.is_endless or (ends_at is not None and ends_at.year >= 2100)
    if is_open_ended:
        ends_at = None
    if ends_at is not None and ends_at < now:
        return None
    if starts_at is None:
        return EventTiming(None, None, "ongoing") if schedule.is_startless and is_open_ended else None
    if starts_at > now:
        state = "period" if ends_at and ends_at.date() > starts_at.date() else "upcoming"
        return EventTiming(starts_at, ends_at, state)
    if is_open_ended or ends_at is not None:
        return EventTiming(starts_at, ends_at, "ongoing")
    return None


def _recurring_timing(schedule: EventSchedule, now: datetime, zone: ZoneInfo) -> EventTiming | None:
    local_now = now.astimezone(zone)
    period_start = schedule.starts_at.astimezone(zone).date() if schedule.starts_at else None
    period_end = None if schedule.is_endless or schedule.ends_at is None or schedule.ends_at.year >= 2100 else schedule.ends_at.astimezone(zone)
    candidates: list[EventTiming] = []
    for rule in schedule.recurrence or []:
        if not isinstance(rule, dict):
            continue
        days = {day for day in rule.get("days_of_week", []) if isinstance(day, int) and 1 <= day <= 7}
        start_time = _rule_time(rule.get("start_time")) or schedule.start_time
        end_time = _rule_time(rule.get("end_time")) or schedule.end_time
        if not days or start_time is None:
            continue
        for offset in range(8):
            occurrence_date = local_now.date() + timedelta(days=offset)
            if occurrence_date.isoweekday() not in days or (period_start and occurrence_date < period_start):
                continue
            starts_local = datetime.combine(occurrence_date, start_time, tzinfo=zone)
            ends_local = datetime.combine(occurrence_date, end_time, tzinfo=zone) if end_time else None
            if ends_local is not None and ends_local <= starts_local:
                ends_local += timedelta(days=1)
            starts_at = starts_local.astimezone(timezone.utc)
            ends_at = ends_local.astimezone(timezone.utc) if ends_local else None
            if period_end is not None and starts_at > period_end.astimezone(timezone.utc):
                continue
            if ends_at is not None and starts_at <= now <= ends_at:
                return EventTiming(starts_at, ends_at, "ongoing")
            if starts_at > now:
                candidates.append(EventTiming(starts_at, ends_at, "recurring"))
                break
    return min(candidates, key=lambda timing: timing.starts_at or datetime.max.replace(tzinfo=timezone.utc), default=None)


def _rule_time(value: object) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


def score_card(card: EventCard, weights: dict[str, Decimal]) -> Decimal:
    return sum(
        (
            weights.get(code, Decimal("0"))
            * (Decimal("1") if kind == "primary" else SECONDARY_TAG_MULTIPLIER)
            for code, kind in card.tag_kinds
        ),
        start=Decimal("0"),
    )


def rank_cards(
    cards: list[EventCard],
    *,
    weights: dict[str, Decimal],
    reaction_count: int,
    prior_primary_tags: list[frozenset[str]],
    rng: random.Random | None = None,
) -> list[EventCard]:
    """Diversify a ranked list and make every fourth card exploratory."""
    pool = sorted(cards, key=lambda card: (-card.score, str(card.id)))
    history = list(prior_primary_tags[-2:])
    result: list[EventCard] = []
    generator = rng or random.Random()
    while pool:
        allowed = [
            card for card in pool
            if len(history) < 2 or not (card.primary_codes & history[-1] & history[-2])
        ]
        candidates = allowed or pool
        position = reaction_count + len(result) + 1
        selected = generator.choice(candidates) if position % 4 == 0 else candidates[0]
        pool.remove(selected)
        result.append(selected)
        history.append(selected.primary_codes)
    return result
