"""MAX handlers that render event cards and save feed reactions."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from maxapi import Bot, Dispatcher, F
from maxapi.enums import AttachmentType, UploadType
from maxapi.types import AttachmentUpload, ButtonsPayload, CallbackButton, Command, InputMediaBuffer, LinkButton, MessageCallback, MessageCreated
from maxapi.types.attachments.attachment import Attachment, OtherAttachmentPayload
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infrastructure.cache.feed_buffer import FeedBufferStore
from infrastructure.db.repositories import CompanionRepository, FeedRepository, OnboardingError, OnboardingRepository
from infrastructure.db.repositories.feed import EventCard
from .navigation import menu, menu_rows, profile_offer
from .notifications import send_match_notifications

MAX_EVENT_IMAGE_BYTES = 5 * 1024 * 1024
FEED_BUFFER_SIZE = 6
# Start the next query immediately after the fifth card of a six-card page.
FEED_REFILL_TRIGGER_REMAINING = 1
PLACEHOLDER_IMAGE_PATH = Path(__file__).resolve().parents[1] / "assets" / "images" / "event-no-image.jpg"
LOGGER = logging.getLogger(__name__)

_placeholder_attachment: AttachmentUpload | None = None
_placeholder_attachment_lock = asyncio.Lock()


def _payload_parts(payload: str | None) -> tuple[str, str, str] | None:
    parts = payload.split(":", maxsplit=2) if payload else []
    return tuple(parts) if len(parts) == 3 and parts[0] == "feed" else None


def _buttons(
    card: EventCard,
    *,
    mode: str,
    index: int = 0,
    total: int = 1,
) -> list:
    if mode == "liked":
        rows = [
            [CallbackButton(text="Пойду", payload=f"feed:want:{card.id}|liked|{index}")],
            [CallbackButton(text="Убрать лайк", payload=f"feed:unlike:{card.id}|liked|{index}")],
        ]
    elif mode == "plans":
        rows = [
            [CallbackButton(text="Смотреть компанию" if card.company_status == "looking" else "Найти компанию",
                            payload=f"feed:company:yes|{card.plan_id}|plans|{index}")],
            [CallbackButton(text="Отменить поход", payload=f"feed:cancel:{card.plan_id}|plans|{index}")],
        ]
        if card.company_status == "looking":
            rows.append([CallbackButton(text="Больше не ищу компанию", payload=f"feed:company:no|{card.plan_id}|plans|{index}")])
        if card.is_liked:
            rows.append([CallbackButton(text="Убрать лайк", payload=f"feed:unlike:{card.id}|plans|{index}")])
    else:
        rows = [
            [
                CallbackButton(text="Нравится", payload=f"feed:like:{card.id}"),
                CallbackButton(text="Не интересно", payload=f"feed:skip:{card.id}"),
            ],
            [CallbackButton(text="Пойду", payload=f"feed:want:{card.id}")],
        ]
    rows.append([LinkButton(text="Подробнее", url=card.source_url)])
    if mode != "feed":
        navigation = []
        if index > 0:
            navigation.append(CallbackButton(text="← Назад", payload=f"feed:browse:{mode}|{index - 1}"))
        if index + 1 < total:
            navigation.append(CallbackButton(text="Дальше →", payload=f"feed:browse:{mode}|{index + 1}"))
        if navigation:
            rows.append(navigation)
    return [ButtonsPayload(buttons=rows + menu_rows()).pack()]


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
    return "\n\n".join(parts)


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


def _cached_image_attachment(value: dict | None) -> AttachmentUpload | None:
    if not value:
        return None
    try:
        attachment = AttachmentUpload.model_validate(value)
    except (TypeError, ValueError):
        return None
    return attachment if attachment.type == UploadType.IMAGE else None


async def _placeholder_image_attachment(bot: Bot) -> AttachmentUpload:
    global _placeholder_attachment
    if _placeholder_attachment is not None:
        return _placeholder_attachment
    async with _placeholder_attachment_lock:
        if _placeholder_attachment is None:
            _placeholder_attachment = await bot.upload_media(
                InputMediaBuffer(
                    buffer=PLACEHOLDER_IMAGE_PATH.read_bytes(),
                    filename=PLACEHOLDER_IMAGE_PATH.name,
                    type=UploadType.IMAGE,
                )
            )
    return _placeholder_attachment


async def _event_image_attachment(
    card: EventCard,
    *,
    bot: Bot,
) -> AttachmentUpload | Attachment:
    cached = _cached_image_attachment(card.max_attachment)
    if cached is not None:
        return cached
    if card.image_url:
        return Attachment(
            type=AttachmentType.IMAGE,
            payload=OtherAttachmentPayload(url=card.image_url),
        )
    return await _placeholder_image_attachment(bot)


async def _attachments(
    card: EventCard,
    *,
    mode: str,
    index: int = 0,
    total: int = 1,
    bot: Bot,
) -> list:
    attachments = _buttons(
        card,
        mode=mode,
        index=index,
        total=total,
    )
    image = await _event_image_attachment(card, bot=bot)
    if image is not None:
        attachments.insert(0, image)
    return attachments


def register_feed_handlers(
    dispatcher: Dispatcher,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_buffer = FeedBufferStore.from_env()
    refill_tasks: dict[UUID, asyncio.Task[None]] = {}

    async def current_user(max_user_id: int):
        async with session_factory() as session:
            return await OnboardingRepository(session).get_user(max_user_id)

    async def user_uuid(max_user_id: int) -> UUID | None:
        user = await current_user(max_user_id)
        return user.id if user and user.city_id is not None and user.onboarding_step != "consent" else None

    async def refill_feed(user_id: UUID) -> None:
        if not await feed_buffer.acquire_refill_lock(user_id):
            return
        try:
            excluded_event_ids = await feed_buffer.excluded_ids(user_id)
            async with session_factory() as session:
                cards = await FeedRepository(session).next_cards(
                    user_id,
                    limit=FEED_BUFFER_SIZE,
                    excluded_event_ids=excluded_event_ids,
                )
            await feed_buffer.append(user_id, [card.id for card in cards])
        except Exception:
            LOGGER.exception("Could not prepare feed buffer for user %s", user_id)
        finally:
            await feed_buffer.release_refill_lock(user_id)

    def start_refill(user_id: UUID) -> asyncio.Task[None]:
        active_task = refill_tasks.get(user_id)
        if active_task is not None and not active_task.done():
            return active_task
        task = asyncio.create_task(refill_feed(user_id))
        refill_tasks[user_id] = task

        def clear_finished_task(completed: asyncio.Task[None]) -> None:
            if refill_tasks.get(user_id) is completed:
                refill_tasks.pop(user_id, None)

        task.add_done_callback(clear_finished_task)
        return task

    async def next_buffered_card(user_id: UUID) -> EventCard | None:
        event_id = await feed_buffer.pop(user_id)
        if event_id is None:
            await start_refill(user_id)
            event_id = await feed_buffer.pop(user_id)

        while event_id is not None:
            async with session_factory() as session:
                card = await FeedRepository(session).buffered_card(user_id, event_id)
            if card is not None:
                if await feed_buffer.pending_count(user_id) == FEED_REFILL_TRIGGER_REMAINING:
                    start_refill(user_id)
                return card
            event_id = await feed_buffer.pop(user_id)
        return None

    async def render_event_card(
        answer,
        card: EventCard,
        *,
        bot: Bot,
        mode: str,
        index: int = 0,
        total: int = 1,
        heading: str | None = None,
    ) -> None:
        text = card_text(card)
        if heading is not None:
            text = f"{heading} · {index + 1}/{total}\n\n{text}"
        await answer(
            text,
            attachments=await _attachments(
                card,
                mode=mode,
                index=index,
                total=total,
                bot=bot,
            ),
        )

    async def show_next(answer, user_id: UUID, bot: Bot) -> None:
        card = await next_buffered_card(user_id)
        if card is None:
            await answer("Пока нет новых подходящих мероприятий. Каталог обновляется два раза в день.", attachments=menu())
            return
        await render_event_card(answer, card, bot=bot, mode="feed")

    async def browse(answer, user_id: UUID, bot: Bot, mode: str, index: int = 0) -> None:
        if mode == "feed":
            await show_next(answer, user_id, bot)
            return
        if mode not in {"liked", "plans"}:
            raise OnboardingError("Неизвестный раздел")
        async with session_factory() as session:
            repository = FeedRepository(session)
            cards = await repository.liked_cards(user_id) if mode == "liked" else await repository.planned_cards(user_id)
        title = "Понравившиеся" if mode == "liked" else "Мои планы"
        if not cards:
            await answer(f"{title}: пока пусто. Выбирай мероприятия в афише.", attachments=menu())
            return
        index = min(max(index, 0), len(cards) - 1)
        card = cards[index]
        await render_event_card(
            answer,
            card,
            bot=bot,
            mode=mode,
            index=index,
            total=len(cards),
            heading=title,
        )

    async def show_companion(answer, user_id: UUID, plan_id: UUID) -> None:
        async with session_factory() as session:
            card = await CompanionRepository(session).next_candidate(user_id, plan_id)
            if card is None:
                await answer(
                    "Пока нет подходящих людей для этого события. Попробуй открыть поиск компании позже.",
                    attachments=menu(),
                )
                return
            await render_companion(answer, card)
            # If sending fails, the session rolls back the reserved view.
            await session.commit()

    async def render_companion(answer, card) -> None:
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
                ] + menu_rows()
            ).pack()
        ]
        image = await _image_attachment(card.photo_url)
        if image is not None:
            attachments.insert(0, image)
        await answer("\n\n".join(details), attachments=attachments)

    async def ask_about_company(answer, plan_id: UUID, mode: str = "feed", index: int = 0) -> None:
        await answer(
            "Добавили событие в планы. Хочешь найти компанию?",
            attachments=[
                ButtonsPayload(
                    buttons=[
                        [CallbackButton(text="Да, ищу компанию", payload=f"feed:company:yes|{plan_id}|{mode}|{index}")],
                        [CallbackButton(text="Пока нет", payload=f"feed:company:no|{plan_id}|{mode}|{index}")],
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
        await show_next(event.message.answer, user_id, event.bot)

    @dispatcher.message_created(Command("liked"))
    async def on_liked(event: MessageCreated) -> None:
        sender = event.message.sender
        if sender is None:
            return
        user_id = await user_uuid(sender.user_id)
        if user_id is None:
            await event.message.answer("Сначала пройди старт: /start")
            return
        await browse(event.message.answer, user_id, event.bot, "liked")

    @dispatcher.message_created(Command("plans"))
    async def on_plans(event: MessageCreated) -> None:
        sender = event.message.sender
        if sender is None:
            return
        user_id = await user_uuid(sender.user_id)
        if user_id is None:
            await event.message.answer("Сначала пройди старт: /start")
            return
        await browse(event.message.answer, user_id, event.bot, "plans")

    @dispatcher.message_callback(F.callback.payload.startswith("feed:"))
    async def on_feed_callback(event: MessageCallback) -> None:
        parts = _payload_parts(event.callback.payload)
        if parts is None:
            return
        _, action, value = parts
        user_id = await user_uuid(event.callback.user.user_id)
        if user_id is None:
            await event.ack("Сначала пройди /start")
            return

        async def edit_current(text=None, *, attachments=None, format=None) -> None:
            if event.message is None:
                await event.edit(text, attachments=attachments, format=format)
                return
            message = event._require_message()
            await asyncio.gather(
                event.ack(),
                message.edit(text, attachments=attachments, format=format, notify=False),
            )

        try:
            if action == "browse":
                mode, index = value.split("|")
                await browse(edit_current, user_id, event.bot, mode, int(index))
            elif action in {"like", "skip"}:
                async with session_factory() as session:
                    repository = FeedRepository(session)
                    await repository.record_reaction(user_id, UUID(value), action)
                    await session.commit()
                await show_next(edit_current, user_id, event.bot)
            elif action == "want":
                values = value.split("|")
                event_id = UUID(values[0])
                mode, index = (values[1], int(values[2])) if len(values) == 3 else ("feed", 0)
                async with session_factory() as session:
                    result = await FeedRepository(session).want_to_go(user_id, event_id)
                    await session.commit()
                await ask_about_company(edit_current, result.plan_id, mode, index)
            elif action in {"unlike", "cancel"}:
                target, mode, index = value.split("|")
                async with session_factory() as session:
                    repository = FeedRepository(session)
                    if action == "unlike":
                        await repository.remove_like(user_id, UUID(target))
                    else:
                        await repository.cancel_plan(user_id, UUID(target))
                    await session.commit()
                await browse(edit_current, user_id, event.bot, mode, int(index))
            elif action == "plan_company":
                await ask_about_company(edit_current, UUID(value))
            elif action == "company":
                values = value.split("|")
                choice, plan_raw = values[:2]
                mode, index = (values[2], int(values[3])) if len(values) == 4 else ("plans", 0)
                if choice not in {"yes", "no"}:
                    raise OnboardingError("Unknown company choice")
                user = await current_user(event.callback.user.user_id)
                if choice == "yes" and user.profile_status != "active":
                    await edit_current("План сохранён. Для поиска компании нужна анкета. Хочешь её создать?",
                                       attachments=profile_offer())
                    return
                async with session_factory() as session:
                    repository = FeedRepository(session)
                    await repository.set_company_search(user_id, UUID(plan_raw), looking=choice == "yes")
                    await session.commit()
                if choice == "yes":
                    await show_companion(edit_current, user_id, UUID(plan_raw))
                else:
                    await browse(edit_current, user_id, event.bot, mode, index)
            elif action in {"person_like", "person_skip"}:
                async with session_factory() as session:
                    result = await CompanionRepository(session).react(
                        user_id,
                        UUID(value),
                        liked=action == "person_like",
                    )
                    await session.commit()
                if result.match_id is not None:
                    await send_match_notifications(event.bot, session_factory, result.match_id)
                    await edit_current(
                        "У вас мэтч! Мы отправили вам обоим ссылки на профили MAX.",
                        attachments=menu(),
                    )
                else:
                    await show_companion(edit_current, user_id, result.owner_plan_id)
            elif action == "liked":
                # Old messages remain navigable after deploying the card browser.
                await browse(edit_current, user_id, event.bot, "liked")
        except (OnboardingError, ValueError) as exc:
            await event.ack(str(exc))
