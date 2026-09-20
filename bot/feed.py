"""MAX handlers that render event cards and save feed reactions."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from maxapi import Dispatcher
from maxapi.enums import UploadType
from maxapi.types import ButtonsPayload, CallbackButton, Command, InputMediaBuffer, MessageCallback, MessageCreated
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infrastructure.db.repositories import CompanionRepository, FeedRepository, OnboardingError, OnboardingRepository
from infrastructure.db.repositories.feed import EventCard

MAX_EVENT_IMAGE_BYTES = 5 * 1024 * 1024


def _payload_parts(payload: str | None) -> tuple[str, str, str] | None:
    parts = payload.split(":", maxsplit=2) if payload else []
    return tuple(parts) if len(parts) == 3 and parts[0] == "feed" else None


def _buttons(card: EventCard, *, mode: str) -> list:
    if mode == "liked":
        rows = [
            [CallbackButton(text="Пойду", payload=f"feed:want:{card.id}")],
            [CallbackButton(text="← К понравившимся", payload="feed:liked:back")],
        ]
    else:
        rows = [
            [
                CallbackButton(text="Нравится", payload=f"feed:like:{card.id}"),
                CallbackButton(text="Не интересно", payload=f"feed:skip:{card.id}"),
            ],
            [CallbackButton(text="Пойду", payload=f"feed:want:{card.id}")],
        ]
    return [ButtonsPayload(buttons=rows).pack()]


def card_text(card: EventCard) -> str:
    parts = [card.title]
    if card.starts_at is not None:
        try:
            local = card.starts_at.astimezone(ZoneInfo(card.city_timezone))
        except Exception:
            local = card.starts_at
        parts.append(f"🗓 {local:%d.%m.%Y %H:%M}")
    if card.place_name:
        place = card.place_name
        if card.place_address:
            place = f"{place}, {card.place_address}"
        parts.append(f"📍 {place}")
    if card.is_free:
        parts.append("💸 Бесплатно")
    elif card.price_text:
        parts.append(f"💸 {card.price_text}")
    else:
        parts.append("💸 Цена уточняется")
    if card.data_status == "uncertain":
        parts.append("⚠️ Данные могут быть неактуальны — проверь их по ссылке.")
    if card.description:
        description = card.description.strip()
        parts.append(description[:500] + ("…" if len(description) > 500 else ""))
    parts.append(f"Подробнее: {card.source_url}")
    return "\n\n".join(parts)


def plans_text(cards: list[EventCard]) -> str:
    lines = ["Мои планы:"]
    for card in cards[:10]:
        details = [card.title]
        if card.starts_at is not None:
            try:
                local = card.starts_at.astimezone(ZoneInfo(card.city_timezone))
            except Exception:
                local = card.starts_at
            details.append(f"🗓 {local:%d.%m.%Y %H:%M}")
        if card.place_name:
            details.append(f"📍 {card.place_name}")
        if card.data_status != "current":
            details.append("⚠️ Данные могут быть неактуальны.")
        details.append(card.source_url)
        lines.append("\n".join(details))
    if len(cards) > 10:
        lines.append(f"Показаны первые 10 из {len(cards)}.")
    return "\n\n".join(lines)


def plans_buttons(cards: list[EventCard]) -> list:
    rows = [
        [CallbackButton(text=f"Найти компанию: {card.title[:25]}", payload=f"feed:plan_company:{card.plan_id}")]
        for card in cards[:10]
        if card.plan_id is not None
    ]
    return [ButtonsPayload(buttons=rows).pack()] if rows else []


async def _image_attachment(image_url: str | None) -> InputMediaBuffer | None:
    if not image_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(image_url)
            response.raise_for_status()
            content = response.content
        if not content or len(content) > MAX_EVENT_IMAGE_BYTES:
            return None
        filename = Path(urlparse(image_url).path).name or "image.jpg"
        return InputMediaBuffer(buffer=content, filename=filename, type=UploadType.IMAGE)
    except httpx.HTTPError:
        return None


async def _attachments(card: EventCard, *, mode: str) -> list:
    attachments = _buttons(card, mode=mode)
    image = await _image_attachment(card.image_url)
    if image is not None:
        attachments.insert(0, image)
    return attachments


def register_feed_handlers(
    dispatcher: Dispatcher,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def current_user(max_user_id: int):
        async with session_factory() as session:
            return await OnboardingRepository(session).get_user(max_user_id)

    async def user_uuid(max_user_id: int) -> UUID | None:
        user = await current_user(max_user_id)
        return user.id if user else None

    async def show_next(answer, user_id: UUID) -> None:
        async with session_factory() as session:
            card = await FeedRepository(session).next_card(user_id)
        if card is None:
            await answer("Пока нет новых подходящих мероприятий. Каталог обновляется два раза в день.", attachments=[])
            return
        await answer(card_text(card), attachments=await _attachments(card, mode="feed"))

    def liked_list_text(cards: list[EventCard]) -> str:
        lines = ["Понравившиеся мероприятия:"]
        for index, card in enumerate(cards[:10], start=1):
            details = [f"{index}. {card.title}"]
            if card.starts_at is not None:
                try:
                    local = card.starts_at.astimezone(ZoneInfo(card.city_timezone))
                except Exception:
                    local = card.starts_at
                details.append(f"🗓 {local:%d.%m.%Y %H:%M}")
            if card.place_name:
                details.append(f"📍 {card.place_name}")
            lines.append("\n".join(details))
        if len(cards) > 10:
            lines.append(f"Показаны первые 10 из {len(cards)}.")
        return "\n\n".join(lines)

    def liked_list_buttons(cards: list[EventCard]) -> list:
        rows = [
            [CallbackButton(text=f"{index}. {card.title[:35]}", payload=f"feed:liked:{card.id}")]
            for index, card in enumerate(cards[:10], start=1)
        ]
        return [ButtonsPayload(buttons=rows).pack()]

    async def show_liked_list(answer, user_id: UUID) -> None:
        async with session_factory() as session:
            cards = await FeedRepository(session).liked_cards(user_id)
        if not cards:
            await answer("В понравившихся пока нет мероприятий без планов.", attachments=[])
            return
        await answer(liked_list_text(cards), attachments=liked_list_buttons(cards))

    async def show_liked_card(answer, user_id: UUID, event_id: UUID) -> None:
        async with session_factory() as session:
            card = await FeedRepository(session).liked_card(user_id, event_id)
        if card is None:
            await answer("Этого события больше нет среди понравившихся.", attachments=[])
            return
        await answer(card_text(card), attachments=await _attachments(card, mode="liked"))

    async def show_companion(answer, user_id: UUID, plan_id: UUID) -> None:
        async with session_factory() as session:
            card = await CompanionRepository(session).next_candidate(user_id, plan_id)
        if card is None:
            await answer(
                "Пока нет подходящих людей для этого события. Попробуй открыть поиск компании позже.",
                attachments=[],
            )
            return
        details = [card.name]
        if card.age is not None:
            details.append(f"{card.age} лет")
        if card.description:
            details.append(card.description)
        attachments = [
            ButtonsPayload(
                buttons=[
                    [
                        CallbackButton(text="Нравится", payload=f"feed:person_like:{card.plan_id}"),
                        CallbackButton(text="Дальше", payload=f"feed:person_skip:{card.plan_id}"),
                    ]
                ]
            ).pack()
        ]
        image = await _image_attachment(card.photo_url)
        if image is not None:
            attachments.insert(0, image)
        await answer("\n\n".join(details), attachments=attachments)

    async def ask_about_company(answer, plan_id: UUID) -> None:
        await answer(
            "Добавили событие в планы. Хочешь найти компанию?",
            attachments=[
                ButtonsPayload(
                    buttons=[
                        [CallbackButton(text="Да, ищу компанию", payload=f"feed:company:yes|{plan_id}")],
                        [CallbackButton(text="Пока нет", payload=f"feed:company:no|{plan_id}")],
                    ]
                ).pack()
            ],
        )

    @dispatcher.message_created(Command("feed"))
    async def on_feed(event: MessageCreated) -> None:
        sender = event.message.sender
        if sender is None:
            return
        user_id = await user_uuid(sender.user_id)
        if user_id is None:
            await event.message.answer("Сначала пройди старт: /start")
            return
        await show_next(event.message.answer, user_id)

    @dispatcher.message_created(Command("liked"))
    async def on_liked(event: MessageCreated) -> None:
        sender = event.message.sender
        if sender is None:
            return
        user_id = await user_uuid(sender.user_id)
        if user_id is None:
            await event.message.answer("Сначала пройди старт: /start")
            return
        await show_liked_list(event.message.answer, user_id)

    @dispatcher.message_created(Command("plans"))
    async def on_plans(event: MessageCreated) -> None:
        sender = event.message.sender
        if sender is None:
            return
        user_id = await user_uuid(sender.user_id)
        if user_id is None:
            await event.message.answer("Сначала пройди старт: /start")
            return
        async with session_factory() as session:
            cards = await FeedRepository(session).planned_cards(user_id)
        if not cards:
            await event.message.answer("В планах пока нет мероприятий. Добавляй их через /liked.")
            return
        await event.message.answer(plans_text(cards), attachments=plans_buttons(cards))

    @dispatcher.message_callback()
    async def on_feed_callback(event: MessageCallback) -> None:
        parts = _payload_parts(event.callback.payload)
        if parts is None:
            return
        _, action, value = parts
        user_id = await user_uuid(event.callback.user.user_id)
        if user_id is None:
            await event.ack("Сначала пройди /start")
            return
        try:
            if action in {"like", "skip"}:
                async with session_factory() as session:
                    repository = FeedRepository(session)
                    await repository.record_reaction(user_id, UUID(value), action)
                    await session.commit()
                await show_next(event.edit, user_id)
            elif action == "want":
                async with session_factory() as session:
                    result = await FeedRepository(session).want_to_go(user_id, UUID(value))
                    await session.commit()
                await ask_about_company(event.edit, result.plan_id)
            elif action == "plan_company":
                await ask_about_company(event.edit, UUID(value))
            elif action == "company":
                choice, plan_raw = value.split("|", maxsplit=1)
                if choice not in {"yes", "no"}:
                    raise OnboardingError("Unknown company choice")
                async with session_factory() as session:
                    repository = FeedRepository(session)
                    await repository.set_company_search(user_id, UUID(plan_raw), looking=choice == "yes")
                    await session.commit()
                if choice == "yes":
                    await show_companion(event.edit, user_id, UUID(plan_raw))
                else:
                    await event.edit("Хорошо, событие осталось в твоих планах.", attachments=[])
            elif action in {"person_like", "person_skip"}:
                async with session_factory() as session:
                    result = await CompanionRepository(session).react(
                        user_id,
                        UUID(value),
                        liked=action == "person_like",
                    )
                    await session.commit()
                if result.match_id is not None:
                    await event.edit(
                        "У вас взаимный интерес! Скоро здесь добавим общий чат для договорённости.",
                        attachments=[],
                    )
                else:
                    await show_companion(event.edit, user_id, result.owner_plan_id)
            elif action == "liked":
                if value == "back":
                    await show_liked_list(event.edit, user_id)
                else:
                    await show_liked_card(event.edit, user_id, UUID(value))
        except (OnboardingError, ValueError) as exc:
            await event.ack(str(exc))
