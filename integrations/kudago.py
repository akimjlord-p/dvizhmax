"""Async KudaGo client and conversion to our catalog input structures.

The client knows the KudaGo transport format.  The normalizer knows our
catalog input format.  Neither part writes to PostgreSQL; persistence belongs
to a repository/import service.
"""
from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, Iterable
from uuid import UUID

import httpx


KUDAGO_BASE_URL = "https://kudago.com/public-api/v1.4"
DEFAULT_EVENT_FIELDS = (
    "id,title,short_title,dates,place,description,body_text,location,"
    "categories,tagline,age_restriction,price,is_free,images,site_url,tags"
)
DEFAULT_EVENT_EXPAND = "dates,place,location"


class KudaGoError(RuntimeError):
    """Base error raised by the KudaGo integration."""


class KudaGoHTTPError(KudaGoError):
    """An HTTP response that cannot be used as an API result."""

    def __init__(self, status_code: int, url: str, body: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.body = body[:500]
        super().__init__(f"KudaGo returned HTTP {status_code} for {url}")


@dataclass(frozen=True, slots=True)
class PlaceDraft:
    source: str
    external_id: str
    name: str
    address: str | None
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True, slots=True)
class ImageDraft:
    url: str
    position: int
    credit_name: str | None
    credit_url: str | None


@dataclass(frozen=True, slots=True)
class ScheduleDraft:
    starts_at: datetime | None
    ends_at: datetime | None
    start_time: time | None
    end_time: time | None
    is_startless: bool
    is_endless: bool
    use_place_schedule: bool
    recurrence: list[dict[str, Any]] | None
    raw_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EventDraft:
    """A normalized event ready for an importer/repository."""

    source: str
    external_id: str
    source_url: str
    title: str
    description: str | None
    city_id: UUID
    place: PlaceDraft | None
    price_text: str | None
    price_min: Decimal | None
    currency: str | None
    is_free: bool | None
    age_min: int | None
    images: tuple[ImageDraft, ...]
    schedules: tuple[ScheduleDraft, ...]
    raw_payload: dict[str, Any]


class KudaGoClient:
    """Small async client for the public KudaGo events API.

    ``iter_events`` follows the API's ``next`` links and yields one raw event
    at a time, so an import does not keep the whole catalog in memory.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = KUDAGO_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 2,
        retry_delay: float = 0.5,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._base_url = base_url.rstrip("/")
        self._allowed_host = httpx.URL(self._base_url).host
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._retry_delay = max(0.0, retry_delay)

    async def __aenter__(self) -> "KudaGoClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def iter_events(
        self,
        *,
        location: str,
        actual_since: int | str | datetime | None = None,
        actual_until: int | str | datetime | None = None,
        page_size: int = 100,
        fields: str = DEFAULT_EVENT_FIELDS,
        expand: str = DEFAULT_EVENT_EXPAND,
        text_format: str = "plain",
        is_free: bool | None = None,
        categories: Iterable[str] | None = None,
        tags: Iterable[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if not 1 <= page_size <= 100:
            raise ValueError("KudaGo page_size must be between 1 and 100")
        params: dict[str, Any] = {
            "location": location,
            "page_size": page_size,
            "fields": fields,
            "expand": expand,
            "text_format": text_format,
        }
        if actual_since is not None:
            params["actual_since"] = _query_timestamp(actual_since)
        if actual_until is not None:
            params["actual_until"] = _query_timestamp(actual_until)
        if is_free is not None:
            params["is_free"] = "1" if is_free else "0"
        if categories:
            params["categories"] = ",".join(categories)
        if tags:
            params["tags"] = ",".join(tags)

        url: str | httpx.URL = f"{self._base_url}/events/"
        first_request = True
        while url:
            self._validate_next_url(url)
            page = await self._get_json(url, params=params if first_request else None)
            first_request = False
            results = page.get("results")
            if not isinstance(results, list):
                raise KudaGoError("KudaGo response has no list field 'results'")
            for event in results:
                if isinstance(event, dict):
                    yield event
            next_url = page.get("next")
            url = next_url if isinstance(next_url, str) else ""

    async def get_event(
        self,
        event_id: int | str,
        *,
        fields: str = DEFAULT_EVENT_FIELDS,
        expand: str = DEFAULT_EVENT_EXPAND,
        text_format: str = "plain",
    ) -> dict[str, Any]:
        url = f"{self._base_url}/events/{event_id}/"
        return await self._get_json(url, params={"fields": fields, "expand": expand, "text_format": text_format})

    async def _get_json(self, url: str | httpx.URL, *, params: dict[str, Any] | None) -> dict[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(url, params=params, timeout=self._timeout)
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise KudaGoError(f"KudaGo request failed: {exc}") from exc
                await asyncio.sleep(self._retry_delay * (2**attempt))
                continue
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise KudaGoError("KudaGo returned invalid JSON") from exc
                if not isinstance(payload, dict):
                    raise KudaGoError("KudaGo returned a non-object JSON response")
                return payload
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self._max_retries:
                await asyncio.sleep(self._retry_delay * (2**attempt))
                continue
            raise KudaGoHTTPError(response.status_code, str(response.url), response.text)
        raise AssertionError("unreachable")

    def _validate_next_url(self, url: str | httpx.URL) -> None:
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or parsed.host != self._allowed_host:
            raise KudaGoError(f"Refusing to follow unexpected KudaGo pagination URL: {url}")


def normalize_event(
    payload: dict[str, Any],
    *,
    city_id: UUID,
    source: str = "kudago",
    currency: str | None = "RUB",
) -> EventDraft:
    """Convert one KudaGo event response into catalog-friendly values."""
    external_id = _required_id(payload)
    title = _clean_text(payload.get("title") or payload.get("short_title"))
    if not title:
        raise ValueError(f"KudaGo event {external_id} has no title")

    place = _normalize_place(payload.get("place"), source)
    images = _normalize_images(payload.get("images"))
    schedules = _normalize_schedules(payload.get("dates"))
    is_free = _optional_bool(payload.get("is_free"))
    source_price_text = _clean_text(payload.get("price"))
    price_text = format_price_text(source_price_text)
    return EventDraft(
        source=source,
        external_id=external_id,
        source_url=str(payload.get("site_url") or ""),
        title=title,
        description=_description(payload),
        city_id=city_id,
        place=place,
        price_text=price_text,
        price_min=None if is_free is True else _minimum_price(source_price_text),
        currency=currency,
        is_free=is_free,
        age_min=_age_min(payload.get("age_restriction")),
        images=images,
        schedules=schedules,
        raw_payload=copy.deepcopy(payload),
    )


def _required_id(payload: dict[str, Any]) -> str:
    value = payload.get("id")
    if value is None or str(value).strip() == "":
        raise ValueError("KudaGo event has no id")
    return str(value)


def _description(payload: dict[str, Any]) -> str | None:
    for key in ("body_text", "description", "tagline"):
        value = _clean_text(payload.get(key))
        if value:
            return value
    return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no"}:
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _age_min(value: Any) -> int | None:
    match = re.search(r"^\s*(\d{1,3})\s*\+", str(value or ""))
    return int(match.group(1)) if match else None


_THOUSANDS_GROUP_RE = re.compile(r"(?<!\d)\d{1,3}(?:[ \u00a0]\d{3})+(?!\d)")
_PRICE_AMOUNT = r"(?:\d{1,3}(?:[ \u00a0.]\d{3})+|\d+)"
_RUBLE_PRICE_RE = re.compile(
    rf"(?<![\d.,])(?P<first>{_PRICE_AMOUNT})"
    rf"(?:\s*(?:до|[-–—])\s*(?P<second>{_PRICE_AMOUNT}))?"
    r"\s*(?:₽|руб\.?\b|рубл(?:ей|я|ь)\b)",
    flags=re.IGNORECASE,
)


def format_price_text(value: str | None) -> str | None:
    """Make price text compact without changing its wording or meaning."""
    if value is None:
        return None
    return _THOUSANDS_GROUP_RE.sub(
        lambda match: match.group().replace(" ", "").replace("\u00a0", ""),
        value,
    )


def _minimum_price(value: str | None) -> Decimal | None:
    if not value or "бесплат" in value.lower():
        return None
    prices: list[Decimal] = []
    for match in _RUBLE_PRICE_RE.finditer(value):
        for amount in (match.group("first"), match.group("second")):
            if amount is None:
                continue
            normalized = amount.replace(" ", "").replace("\u00a0", "").replace(".", "")
            try:
                prices.append(Decimal(normalized))
            except InvalidOperation:
                continue
    return min(prices) if prices else None


def _normalize_place(value: Any, source: str) -> PlaceDraft | None:
    if not isinstance(value, dict):
        return None
    external = value.get("id") or value.get("slug")
    if external is None:
        return None
    coords = value.get("coords") if isinstance(value.get("coords"), dict) else {}
    return PlaceDraft(
        source=source,
        external_id=str(external),
        name=str(value.get("title") or value.get("slug") or ""),
        address=_clean_text(value.get("address")),
        latitude=_number(coords.get("lat")),
        longitude=_number(coords.get("lon")),
    )


def _normalize_images(value: Any) -> tuple[ImageDraft, ...]:
    if not isinstance(value, list):
        return ()
    result: list[ImageDraft] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _clean_text(item.get("image"))
        if not url:
            thumbnails = item.get("thumbnails")
            if isinstance(thumbnails, dict):
                url = _clean_text(thumbnails.get("640x384") or thumbnails.get("original"))
        if not url or url in seen:
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        result.append(ImageDraft(
            url=url,
            position=len(result),
            credit_name=_clean_text(source.get("name")),
            credit_url=_clean_text(source.get("link")),
        ))
        seen.add(url)
    return tuple(result)


def _normalize_schedules(value: Any) -> tuple[ScheduleDraft, ...]:
    if not isinstance(value, list):
        return ()
    result: list[ScheduleDraft] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        starts_at = _timestamp_datetime(raw.get("start"))
        ends_at = _timestamp_datetime(raw.get("end"))
        if starts_at is not None and ends_at is not None and ends_at < starts_at:
            ends_at = None
        recurrence = raw.get("schedules")
        if not isinstance(recurrence, list):
            recurrence = None
        result.append(ScheduleDraft(
            starts_at=starts_at,
            ends_at=ends_at,
            start_time=_parse_time(raw.get("start_time")),
            end_time=_parse_time(raw.get("end_time")),
            is_startless=bool(raw.get("is_startless")),
            is_endless=bool(raw.get("is_endless")),
            use_place_schedule=bool(raw.get("use_place_schedule")),
            recurrence=copy.deepcopy(recurrence),
            raw_payload=copy.deepcopy(raw),
        ))
    return tuple(result)


def _timestamp_datetime(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _parse_time(value: Any) -> time | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _query_timestamp(value: int | str | datetime) -> int | str:
    if isinstance(value, datetime):
        return int((value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp())
    return value
