"""MAX handlers that render event cards and save feed reactions."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from maxapi import Bot, Dispatcher, F
from maxapi.enums import AttachmentType, UploadType
from maxapi.exceptions.max import MaxApiError
from maxapi.types import AttachmentUpload, ButtonsPayload, CallbackButton, Command, InputMediaBuffer, LinkButton, MessageCallback, MessageCreated
from maxapi.types.attachments.attachment import Attachment, OtherAttachmentPayload
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from integrations.kudago import format_price_text
from infrastructure.cache.feed_buffer import FeedBufferStore
from infrastructure.db.repositories import CompanionRepository, DemoRepository, FeedRepository, OnboardingError, OnboardingRepository
from infrastructure.db.repositories.feed import EventCard
from .navigation import menu, menu_rows, plans_and_feed, profile_offer, report_error
from .notifications import send_match_notifications

MAX_EVENT_IMAGE_BYTES = 5 * 1024 * 1024
FEED_BUFFER_SIZE = 6
# Start the next query immediately after the fifth card of a six-card page.
FEED_REFILL_TRIGGER_REMAINING = 1
PLACEHOLDER_IMAGE_PATH = Path(__file__).resolve().parents[1] / "assets" / "images" / "event-no-image.jpg"
DEMO_PROFILE_ASSETS_DIR = PLACEHOLDER_IMAGE_PATH.parent
LOGGER = logging.getLogger(__name__)
NO_CANDIDATES_TEXT = "Других анкет для этого события пока нет. Попробуй поиск позже."
PERSON_LIKED_STATUS = "👍 Лайк отправлен. Если интерес будет взаимным — сообщим о мэтче."

_placeholder_attachment: AttachmentUpload | None = None
_placeholder_attachment_lock = asyncio.Lock()
_demo_profile_attachments: dict[str, AttachmentUpload] = {}
_demo_profile_attachments_lock = asyncio.Lock()


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
            [CallbackButton(text="Хочу пойти", payload=f"feed:want:{card.id}|liked|{index}")],
            [CallbackButton(text="Убрать лайк", payload=f"feed:unlike:{card.id}|liked|{index}")],
        ]
    elif mode == "plans":
        rows = [
            [CallbackButton(text="Смотреть компанию" if card.company_status == "looking" else "Найти компанию",
                            payload=f"feed:company:yes|{card.plan_id}|plans|{index}")],
            [CallbackButton(text="Отменить поход", payload=f"feed:cancel:{card.plan_id}|plans|{index}")],
        ]
        if card.company_status == "looking" and card.pending_likes:
            rows.insert(1, [CallbackButton(text=f"Тебя лайкнули · {card.pending_likes}", payload=f"feed:likers:{card.plan_id}")])
        if card.company_status == "looking":
            rows.append([CallbackButton(text="Больше не ищу компанию", payload=f"feed:company:no|{card.plan_id}|plans|{index}")])
        if card.is_liked:
            rows.append([CallbackButton(text="Убрать лайк", payload=f"feed:unlike:{card.id}|plans|{index}")])
    else:
        rows = [
            [
                CallbackButton(text="👍 Нравится", payload=f"feed:like:{card.id}"),
                CallbackButton(text="Не моё", payload=f"feed:skip:{card.id}"),
            ],
            [CallbackButton(text="Хочу пойти", payload=f"feed:want:{card.id}")],
        ]
    rows.append([LinkButton(text=_source_label(card.source_url), url=card.source_url)])
    if mode != "feed":
        navigation = []
        if index > 0:
            navigation.append(CallbackButton(text="← Назад", payload=f"feed:browse:{mode}|{index - 1}"))
        if index + 1 < total:
            navigation.append(CallbackButton(text="Дальше →", payload=f"feed:browse:{mode}|{index + 1}"))
        if navigation:
            rows.append(navigation)
    return [ButtonsPayload(buttons=rows + menu_rows()).pack()]


def _source_label(url: str) -> str:
    host = urlparse(url).hostname or ""
    return "Подробнее в KudaGo ↗" if host == "kudago.com" or host.endswith(".kudago.com") else "Подробнее ↗"


def _without_buttons(attachments: list | None, payloads: set[str]) -> list:
    """Copy message attachments, dropping inline buttons with the given payloads."""
    result = []
    for item in attachments or []:
        if str(item.type) == "inline_keyboard" and isinstance(item.payload, ButtonsPayload):
            rows = [[button for button in row if getattr(button, "payload", None) not in payloads]
                    for row in item.payload.buttons]
            result.append(ButtonsPayload(buttons=[row for row in rows if row]).pack())
        else:
            result.append(item)
    return result


def card_text(card: EventCard) -> str:
    parts = [card.title]
    if card.schedule_state == "ongoing":
        if card.ends_at is not None:
            try:
                local_end = card.ends_at.astimezone(ZoneInfo(card.city_timezone))
            except Exception:
                local_end = card.ends_at
            parts.append(f"🗓 Идёт сейчас · до {local_end:%d.%m.%Y %H:%M}")
        else:
            parts.append("🗓 Идёт сейчас")
    elif card.schedule_state == "recurring" and card.starts_at is not None:
        try:
            local = card.starts_at.astimezone(ZoneInfo(card.city_timezone))
        except Exception:
            local = card.starts_at
        parts.append(f"🗓 Ближайший сеанс: {local:%d.%m.%Y %H:%M}")
    elif card.schedule_state == "period" and card.starts_at is not None and card.ends_at is not None:
        try:
            local_start = card.starts_at.astimezone(ZoneInfo(card.city_timezone))
            local_end = card.ends_at.astimezone(ZoneInfo(card.city_timezone))
        except Exception:
            local_start, local_end = card.starts_at, card.ends_at
        parts.append(f"🗓 Период: {local_start:%d.%m.%Y} — {local_end:%d.%m.%Y}")
    elif card.starts_at is not None:
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
        parts.append(f"💸 {format_price_text(card.price_text)}")
    else:
        parts.append("💸 Цена уточняется")
    if card.data_status == "uncertain":
        parts.append("⚠️ Информация могла измениться. Проверь дату и условия у источника.")
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


async def _profile_image_attachment(
    photo_url: str | None,
    photo_attachment: dict | None,
    *,
    bot: Bot,
) -> Attachment | AttachmentUpload | None:
    """Render user photos by URL and fixed demo placeholders from local assets."""
    if photo_url and photo_url.startswith("asset://"):
        asset_name = photo_url.removeprefix("asset://")
        asset_path = DEMO_PROFILE_ASSETS_DIR / asset_name
        if asset_path.name != asset_name or not asset_path.is_file():
            return None
        cached = _demo_profile_attachments.get(asset_name)
        if cached is not None:
            return cached
        async with _demo_profile_attachments_lock:
            cached = _demo_profile_attachments.get(asset_name)
            if cached is None:
                cached = await bot.upload_media(InputMediaBuffer(
                    buffer=asset_path.read_bytes(),
                    filename=asset_name,
                    type=UploadType.IMAGE,
                ))
                _demo_profile_attachments[asset_name] = cached
        return cached
    url = photo_url or (photo_attachment or {}).get("url")
    if not isinstance(url, str) or not url:
        return None
    return Attachment(type=AttachmentType.IMAGE, payload=OtherAttachmentPayload(url=url))


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
        try:
            await answer(
                text,
                attachments=await _attachments(card, mode=mode, index=index, total=total, bot=bot),
            )
        except MaxApiError as exc:
            if card.image_url is None and card.max_attachment is None:
                raise
            # MAX sometimes cannot fetch a KudaGo photo by URL. Show the card anyway.
            LOGGER.warning("MAX rejected the photo of event %s: %s", card.id, exc)
            fallback = replace(card, image_url=None, max_attachment=None)
            await answer(
                text,
                attachments=await _attachments(fallback, mode=mode, index=index, total=total, bot=bot),
            )

    async def show_next(answer, user_id: UUID, bot: Bot) -> None:
        card = await next_buffered_card(user_id)
        if card is None:
            await answer(
                "Новых событий пока нет — ты уже посмотрел доступную подборку. Загляни позже.",
                attachments=menu(),
            )
            return
        try:
            await render_event_card(answer, card, bot=bot, mode="feed")
        except Exception:
            # The card was taken from the queue; put it back so it is not lost.
            await feed_buffer.requeue(user_id, card.id)
            raise

    async def browse(answer, user_id: UUID, bot: Bot, mode: str, index: int = 0) -> None:
        if mode == "feed":
            await show_next(answer, user_id, bot)
            return
        if mode not in {"liked", "plans"}:
            raise OnboardingError("Неизвестный раздел")
        async with session_factory() as session:
            repository = FeedRepository(session)
            cards = await repository.liked_cards(user_id) if mode == "liked" else await repository.planned_cards(user_id)
            if mode == "plans":
                counts = await CompanionRepository(session).pending_liker_counts(
                    user_id, [card.plan_id for card in cards if card.company_status == "looking"],
                )
                cards = [replace(card, pending_likes=counts.get(card.plan_id, 0)) for card in cards]
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

    async def show_companion(answer, user_id: UUID, plan_id: UUID, bot: Bot) -> None:
        async with session_factory() as session:
            card = await CompanionRepository(session).next_candidate(user_id, plan_id)
            if card is None:
                await answer(NO_CANDIDATES_TEXT, attachments=plans_and_feed())
                return
            await render_companion(answer, card, bot=bot)
            # If sending fails, the session rolls back the reserved view.
            await session.commit()

    async def show_liker(answer, user_id: UUID, plan_id: UUID, bot: Bot) -> None:
        async with session_factory() as session:
            card = await CompanionRepository(session).next_liker(user_id, plan_id)
            if card is None:
                await answer(
                    "Новых лайков на это событие больше нет.",
                    attachments=[ButtonsPayload(buttons=[
                        [CallbackButton(text="Смотреть компанию", payload=f"feed:company:yes|{plan_id}|plans|0")],
                    ] + menu_rows()).pack()],
                )
                return
            await render_companion(answer, card, bot=bot, likers=True)
            await session.commit()

    async def render_companion(answer, card, *, bot: Bot, likers: bool = False) -> None:
        details = ["❤️ Этот человек хочет пойти с тобой"] if likers else []
        heading = card.name if card.age is None else f"{card.name}, {card.age}"
        if card.gender:
            gender = {"male": "Мужчина", "female": "Женщина"}.get(card.gender, card.gender)
            heading = f"{heading}\n{gender}"
        details.append(heading)
        if card.description:
            details.append(card.description)
        if card.common_interests:
            details.append(f"Общие интересы: {', '.join(card.common_interests)}")
        suffix = "|likers" if likers else ""
        attachments = [
            ButtonsPayload(
                buttons=[
                    [
                        CallbackButton(text="👍 Пойти вместе", payload=f"feed:person_like:{card.plan_id}{suffix}"),
                        CallbackButton(text="Дальше", payload=f"feed:person_skip:{card.plan_id}{suffix}"),
                    ]
                ] + menu_rows()
            ).pack()
        ]
        image = await _profile_image_attachment(card.photo_url, card.photo_attachment, bot=bot)
        if image is None:
            await answer("\n\n".join(details), attachments=attachments)
            return
        try:
            await answer("\n\n".join(details), attachments=[image, *attachments])
        except MaxApiError as exc:
            LOGGER.warning("MAX rejected the profile photo of user %s: %s", card.user_id, exc)
            await answer("\n\n".join(details), attachments=attachments)

    async def ask_about_company(answer, plan_id: UUID, mode: str = "feed", index: int = 0) -> None:
        await answer(
            "Событие добавлено в планы. Хочешь найти людей, которые тоже собираются пойти?",
            attachments=[
                ButtonsPayload(
                    buttons=[
                        [CallbackButton(text="Найти компанию", payload=f"feed:company:yes|{plan_id}|{mode}|{index}")],
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

    @dispatcher.message_created(Command("demo"))
    async def on_demo(event: MessageCreated) -> None:
        sender = event.message.sender
        if sender is None:
            return
        user_id = await user_uuid(sender.user_id)
        if user_id is None:
            await event.message.answer("Сначала пройди старт: /start")
            return
        user = await current_user(sender.user_id)
        if user is None or user.profile_status != "active":
            await event.message.answer("Для демо мэтча сначала создай анкету через /profile.")
            return
        async with session_factory() as session:
            event_id = await DemoRepository(session).reset_for_user(user_id)
            card = (
                await FeedRepository(session).buffered_card(user_id, event_id, include_reacted=True)
                if event_id else None
            )
            await session.commit()
        if card is None:
            await event.message.answer("Демо ещё не подготовлено. Запусти seed на сервере.", attachments=menu())
            return
        await render_event_card(event.message.answer, card, bot=event.bot, mode="feed", heading="Демо мэтча")

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
            await event.edit(text, attachments=attachments, format=format, notify=False)

        callback_answered = False
        # Set by a reaction: status line and buttons to remove from the rated card.
        reacted_card: tuple[str, set[str]] | None = None

        async def send_next_card(text=None, *, attachments=None, format=None) -> None:
            """Keep every event or profile card in chat and send the next one separately."""
            nonlocal callback_answered
            if event.message is None:
                await edit_current(text, attachments=attachments, format=format)
                return
            original = event._require_message()
            send = event.send(text, attachments=attachments, format=format, notify=False)
            if callback_answered:
                # A retry after a failed send must not answer the same callback twice.
                await send
                return
            original_text, original_attachments = original.body.text, original.body.attachments
            if reacted_card is not None:
                status, removed = reacted_card
                original_text = f"{original_text}\n\n{status}" if original_text else status
                original_attachments = _without_buttons(original_attachments, removed)
            answered, sent = await asyncio.gather(
                event.edit(
                    original_text,
                    attachments=original_attachments,
                    notify=False,
                ),
                send,
                return_exceptions=True,
            )
            if isinstance(answered, BaseException):
                raise answered
            callback_answered = True
            if isinstance(sent, BaseException):
                raise sent

        async def mark_current(status: str, removed: set[str], extra_rows: list | None = None) -> None:
            nonlocal callback_answered
            if event.message is None:
                rows = (extra_rows or []) + menu_rows()
                await edit_current(status, attachments=[ButtonsPayload(buttons=rows).pack()])
                callback_answered = True
                return
            original = event._require_message()
            attachments = _without_buttons(original.body.attachments, removed)
            if extra_rows:
                attachments.append(ButtonsPayload(buttons=extra_rows).pack())
            text_value = f"{original.body.text}\n\n{status}" if original.body.text else status
            await event.edit(text_value, attachments=attachments, notify=False)
            callback_answered = True

        try:
            if action == "menu":
                await send_next_card("Главное меню", attachments=menu())
            elif action == "browse":
                mode, index = value.split("|")
                await browse(send_next_card, user_id, event.bot, mode, int(index))
            elif action in {"like", "skip"}:
                async with session_factory() as session:
                    repository = FeedRepository(session)
                    was_recorded = await repository.record_reaction(user_id, UUID(value), action)
                    await session.commit()
                if not was_recorded:
                    await event.ack("Эта карточка уже оценена")
                    return
                removed = {f"feed:like:{value}", f"feed:skip:{value}"}
                if action == "skip":
                    removed.add(f"feed:want:{value}")
                reacted_card = ("✅ Нравится" if action == "like" else "✖️ Не моё", removed)
                await show_next(send_next_card, user_id, event.bot)
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
                await browse(send_next_card, user_id, event.bot, mode, int(index))
            elif action == "plan_company":
                await ask_about_company(edit_current, UUID(value))
            elif action == "company":
                values = value.split("|")
                choice, plan_raw = values[:2]
                mode, index = (values[2], int(values[3])) if len(values) == 4 else ("plans", 0)
                if choice not in {"yes", "no"}:
                    raise OnboardingError("Неизвестный вариант ответа")
                user = await current_user(event.callback.user.user_id)
                if choice == "yes" and user.profile_status != "active":
                    await edit_current("План сохранён. Для поиска компании нужна анкета. Хочешь её создать?",
                                       attachments=profile_offer())
                    return
                async with session_factory() as session:
                    repository = FeedRepository(session)
                    await repository.set_company_search(user_id, UUID(plan_raw), looking=choice == "yes")
                    if choice == "yes":
                        await DemoRepository(session).ensure_candidates_for_plan(user_id=user_id, user_plan_id=UUID(plan_raw))
                    await session.commit()
                if choice == "yes":
                    await show_companion(send_next_card, user_id, UUID(plan_raw), event.bot)
                else:
                    await browse(send_next_card, user_id, event.bot, mode, index)
            elif action == "likers":
                await show_liker(send_next_card, user_id, UUID(value), event.bot)
            elif action in {"person_like", "person_skip"}:
                candidate_raw, _, source = value.partition("|")
                from_likers = source == "likers"
                async with session_factory() as session:
                    result = await CompanionRepository(session).react(
                        user_id,
                        UUID(candidate_raw),
                        liked=action == "person_like",
                    )
                    await session.commit()
                if not result.was_applied:
                    await event.ack("Эта анкета уже оценена")
                    return
                removed = {f"feed:person_like:{value}", f"feed:person_skip:{value}"}
                if result.created_match and result.match_id is not None:
                    await send_match_notifications(event.bot, session_factory, result.match_id)
                    extra_rows = (
                        [[CallbackButton(text="Кто ещё лайкнул", payload=f"feed:likers:{result.owner_plan_id}")]]
                        if from_likers else None
                    )
                    await mark_current("🎉 Есть мэтч! Подробности — в сообщении ниже.", removed, extra_rows)
                    return
                reacted_card = (PERSON_LIKED_STATUS if action == "person_like" else "➡️ Пропущено", removed)
                if from_likers:
                    await show_liker(send_next_card, user_id, result.owner_plan_id, event.bot)
                else:
                    await show_companion(send_next_card, user_id, result.owner_plan_id, event.bot)
            elif action == "liked":
                # Old messages remain navigable after deploying the card browser.
                await browse(send_next_card, user_id, event.bot, "liked")
        except (OnboardingError, ValueError) as exc:
            await event.ack(str(exc))
        except Exception:
            LOGGER.exception("Feed callback %r failed", event.callback.payload)
            # A reaction may already be saved; retrying it would only say "already rated".
            retry = {
                "like": "feed:browse:feed|0",
                "skip": "feed:browse:feed|0",
                "person_like": "feed:browse:plans|0",
                "person_skip": "feed:browse:plans|0",
            }.get(action, event.callback.payload)
            await report_error(event, retry, answered=callback_answered)
