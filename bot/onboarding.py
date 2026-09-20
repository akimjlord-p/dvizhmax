"""MAX message handlers for consent and profile onboarding."""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from maxapi import Dispatcher
from maxapi.types import BotStarted, ButtonsPayload, CallbackButton, Command, MessageCallback, MessageCreated
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infrastructure.db.models import City, Tag
from infrastructure.db.repositories import OnboardingError, OnboardingRepository

CONSENT_VERSION = "2026-09-20"
MIN_INTERESTS = 3
GROUPS = (
    ("music_stage", "Музыка и сцена", frozenset({"concert", "theater", "cinema", "standup", "party", "dance", "festival"})),
    ("learning_creative", "Обучение и творчество", frozenset({"lecture", "masterclass", "exhibition", "conference"})),
    ("activity_games", "Активности и игры", frozenset({"sport", "board_games", "quiz", "quest", "excursion"})),
    ("social_food", "Общение и еда", frozenset({"meetup", "market", "food_event"})),
)


def keyboard(rows: list[list[CallbackButton]]) -> list[Any]:
    return [ButtonsPayload(buttons=rows).pack()]


def _callback_parts(payload: str | None) -> tuple[str, str, str] | None:
    parts = payload.split(":", maxsplit=2) if payload else []
    return tuple(parts) if len(parts) == 3 and parts[0] == "onboarding" else None


def _group_tags(tags: list[Tag], code: str) -> list[Tag]:
    group = next((group for group in GROUPS if group[0] == code), None)
    if group:
        return [tag for tag in tags if tag.code in group[2]]
    known = frozenset().union(*(group[2] for group in GROUPS))
    return [tag for tag in tags if tag.code not in known]


def city_keyboard(cities: list[City]) -> list[Any]:
    return keyboard([[CallbackButton(text=city.name, payload=f"onboarding:city:{city.id}")] for city in cities])


def categories_keyboard(tags: list[Tag], selected: set[UUID]) -> list[Any]:
    rows = []
    for code, name, _ in GROUPS:
        group_tags = _group_tags(tags, code)
        if group_tags:
            count = sum(tag.id in selected for tag in group_tags)
            rows.append([CallbackButton(text=f"{name}{f' · {count}' if count else ''}", payload=f"onboarding:interest_group:{code}")])
    if _group_tags(tags, "other"):
        rows.append([CallbackButton(text="Другое", payload="onboarding:interest_group:other")])
    rows.append([CallbackButton(text=f"Продолжить · {len(selected)}", payload="onboarding:interest:finish")])
    return keyboard(rows)


def interests_keyboard(tags: list[Tag], selected: set[UUID], group_code: str) -> list[Any]:
    group_tags = _group_tags(tags, group_code)
    rows = [
        [CallbackButton(text=f"{'✓ ' if tag.id in selected else ''}{tag.name}", payload=f"onboarding:interest:{tag.id}") for tag in group_tags[i:i + 2]]
        for i in range(0, len(group_tags), 2)
    ]
    rows.append([CallbackButton(text="← К категориям", payload="onboarding:interest_group:back")])
    return keyboard(rows)


def _photo(event: MessageCreated) -> tuple[str | None, dict[str, Any]] | None:
    for item in event.message.body.attachments or []:
        if str(item.type) == "image" and item.payload is not None:
            return getattr(item.payload, "url", None), item.payload.model_dump(mode="json", exclude_none=True)
    return None


def register_onboarding_handlers(dispatcher: Dispatcher, session_factory: async_sessionmaker[AsyncSession]) -> None:
    consent_version = os.getenv("PRIVACY_POLICY_VERSION", CONSENT_VERSION)

    async def user_id(max_id: int, username: str | None) -> UUID:
        async with session_factory() as session:
            user = await OnboardingRepository(session).get_or_create_user(max_user_id=max_id, max_username=username)
            await session.commit()
            return user.id

    async def interest_categories(answer, uid: UUID) -> None:
        async with session_factory() as session:
            repo = OnboardingRepository(session)
            tags, selected = await repo.list_interest_tags(), await repo.selected_interest_ids(uid)
        await answer(
            "Какие занятия тебе интересны?\n\n"
            "Открой раздел и выбери сколько угодно интересов. "
            f"Сейчас выбрано: {len(selected)}. Для старта нужно хотя бы {MIN_INTERESTS}.",
            attachments=categories_keyboard(tags, selected),
        )

    async def resume(event: MessageCreated | BotStarted, answer) -> None:
        sender = event.message.sender if isinstance(event, MessageCreated) else event.user
        if sender is None:
            return
        uid = await user_id(sender.user_id, sender.username)
        async with session_factory() as session:
            repo = OnboardingRepository(session)
            user = await repo.require_user(uid)
            consent = await repo.has_consent(uid, consent_version)
            cities = await repo.list_cities() if consent and user.city_id is None else []
            step = user.onboarding_step
        if not consent:
            await answer(
                "ДвижМАКС помогает найти событие и компанию для него.\n\n"
                "Мы сохраняем твой MAX ID, город, выбранные интересы и данные анкеты, если ты решишь её создать. "
                "Это нужно для рекомендаций, поиска компании и общения.\n\n"
                "Продолжая, ты соглашаешься на обработку этих данных.",
                attachments=keyboard([[CallbackButton(text="Согласен", payload="onboarding:consent:accept")], [CallbackButton(text="Не согласен", payload="onboarding:consent:decline")]]),
            )
        elif user.city_id is None:
            await answer("Выбери город." if cities else "Каталог городов ещё обновляется. Попробуй чуть позже.", attachments=city_keyboard(cities) if cities else [])
        elif step == "profile_choice":
            await answer("Город сохранён.\n\nХочешь создать профиль? С профилем можно искать компанию на мероприятия. Без него доступна только афиша.", attachments=keyboard([[CallbackButton(text="Создать профиль", payload="onboarding:profile:create")], [CallbackButton(text="Только афиша", payload="onboarding:profile:guest")]]))
        elif step == "name":
            await answer("Как тебя зовут? Это имя увидят другие люди.")
        elif step == "gender":
            await answer("Выбери пол.", attachments=keyboard([[CallbackButton(text="Мужской", payload="onboarding:gender:male")], [CallbackButton(text="Женский", payload="onboarding:gender:female")], [CallbackButton(text="Другой", payload="onboarding:gender:other")]]))
        elif step == "age":
            await answer("Сколько тебе лет? Напиши число.")
        elif step == "description":
            await answer("Расскажи о себе в паре фраз. Это увидят люди, которые ищут компанию.", attachments=keyboard([[CallbackButton(text="Пропустить", payload="onboarding:description:skip")]]))
        elif step == "photo":
            await answer("Добавь фото для анкеты — так тебя будет проще узнать. Его можно пропустить.", attachments=keyboard([[CallbackButton(text="Пропустить", payload="onboarding:photo:skip")]]))
        elif step == "interests":
            await interest_categories(answer, uid)
        elif step == "complete":
            await answer("Ты уже в ДвижМАКС. Афиша — /feed, понравившиеся — /liked, планы — /plans.")

    @dispatcher.bot_started()
    async def on_bot_started(event: BotStarted) -> None:
        await resume(event, lambda text, **kwargs: event.bot.send_message(chat_id=event.chat_id, text=text, **kwargs))

    @dispatcher.message_created(Command("start"))
    async def on_start(event: MessageCreated) -> None:
        await resume(event, event.message.answer)

    @dispatcher.message_callback()
    async def on_callback(event: MessageCallback) -> None:
        parts = _callback_parts(event.callback.payload)
        if parts is None:
            return
        _, action, value = parts
        uid = await user_id(event.callback.user.user_id, event.callback.user.username)
        try:
            if action == "consent" and value == "decline":
                await event.edit("Без согласия бот не может создать профиль и подобрать мероприятия.", attachments=[])
                return
            if action == "consent" and value == "accept":
                async with session_factory() as session:
                    repo = OnboardingRepository(session)
                    await repo.accept_consent(uid, consent_version)
                    cities = await repo.list_cities()
                    await session.commit()
                await event.edit("Выбери город." if cities else "Каталог городов ещё обновляется. Попробуй чуть позже.", attachments=city_keyboard(cities) if cities else [])
                return
            if action == "city":
                async with session_factory() as session:
                    await OnboardingRepository(session).choose_city(uid, UUID(value))
                    await session.commit()
                await resume(event, event.edit)
                return
            if action == "profile":
                async with session_factory() as session:
                    repo = OnboardingRepository(session)
                    if value == "guest":
                        await repo.continue_as_guest(uid)
                    elif value == "create":
                        await repo.begin_profile(uid)
                    else:
                        return
                    await session.commit()
                await event.edit("Готово. Откроем афишу, когда добавим её экран." if value == "guest" else "Как тебя зовут? Это имя увидят другие люди.", attachments=[])
                return
            if action == "gender":
                async with session_factory() as session:
                    await OnboardingRepository(session).set_gender(uid, value)
                    await session.commit()
                await event.edit("Сколько тебе лет? Напиши число.", attachments=[])
                return
            if action == "description" and value == "skip":
                async with session_factory() as session:
                    await OnboardingRepository(session).set_description(uid, None)
                    await session.commit()
                await event.edit("Добавь фото для анкеты — так тебя будет проще узнать. Его можно пропустить.", attachments=keyboard([[CallbackButton(text="Пропустить", payload="onboarding:photo:skip")]]))
                return
            if action == "photo" and value == "skip":
                async with session_factory() as session:
                    await OnboardingRepository(session).skip_photo(uid)
                    await session.commit()
                await interest_categories(event.edit, uid)
                return
            if action == "interest_group":
                if value == "back":
                    await interest_categories(event.edit, uid)
                    return
                async with session_factory() as session:
                    repo = OnboardingRepository(session)
                    tags, selected = await repo.list_interest_tags(), await repo.selected_interest_ids(uid)
                group_tags = _group_tags(tags, value)
                if not group_tags:
                    raise OnboardingError("В этом разделе пока нет интересов")
                name = next((group[1] for group in GROUPS if group[0] == value), "Другое")
                await event.edit(f"{name}\n\nВыбирай интересы. Отмечено всего: {len(selected)}.", attachments=interests_keyboard(tags, selected, value))
                return
            if action == "interest":
                async with session_factory() as session:
                    repo = OnboardingRepository(session)
                    if value == "finish":
                        await repo.complete_profile(uid, min_interests=MIN_INTERESTS)
                        await session.commit()
                        await event.edit("Профиль готов. Теперь можно подбирать события и компанию.", attachments=[])
                        return
                    selected = await repo.toggle_interest(uid, UUID(value))
                    tags = await repo.list_interest_tags()
                    await session.commit()
                tag_id = UUID(value)
                code = next((group[0] for group in GROUPS if any(tag.id == tag_id and tag.code in group[2] for tag in tags)), "other")
                name = next((group[1] for group in GROUPS if group[0] == code), "Другое")
                await event.edit(f"{name}\n\nВыбирай интересы. Отмечено всего: {len(selected)}.", attachments=interests_keyboard(tags, selected, code))
        except (OnboardingError, ValueError) as exc:
            await event.ack(str(exc))

    @dispatcher.message_created()
    async def on_message(event: MessageCreated) -> None:
        sender = event.message.sender
        if sender is None or (event.message.body.text or "").strip().startswith("/"):
            return
        uid = await user_id(sender.user_id, sender.username)
        text = (event.message.body.text or "").strip()
        async with session_factory() as session:
            repo = OnboardingRepository(session)
            step = (await repo.require_user(uid)).onboarding_step
            try:
                if step == "name":
                    await repo.set_name(uid, text)
                    await session.commit()
                    await event.message.answer("Выбери пол.", attachments=keyboard([[CallbackButton(text="Мужской", payload="onboarding:gender:male")], [CallbackButton(text="Женский", payload="onboarding:gender:female")], [CallbackButton(text="Другой", payload="onboarding:gender:other")]]))
                elif step == "age":
                    await repo.set_age(uid, int(text))
                    await session.commit()
                    await event.message.answer("Расскажи о себе в паре фраз. Это увидят люди, которые ищут компанию.", attachments=keyboard([[CallbackButton(text="Пропустить", payload="onboarding:description:skip")]]))
                elif step == "description":
                    await repo.set_description(uid, text)
                    await session.commit()
                    await event.message.answer("Добавь фото для анкеты — так тебя будет проще узнать. Его можно пропустить.", attachments=keyboard([[CallbackButton(text="Пропустить", payload="onboarding:photo:skip")]]))
                elif step == "photo":
                    image = _photo(event)
                    if image is None:
                        await event.message.answer("Прикрепи изображение или нажми «Пропустить».", attachments=keyboard([[CallbackButton(text="Пропустить", payload="onboarding:photo:skip")]]))
                        return
                    await repo.set_photo(uid, photo_url=image[0], photo_attachment=image[1])
                    await session.commit()
                    await interest_categories(event.message.answer, uid)
            except (OnboardingError, ValueError) as exc:
                await session.rollback()
                await event.message.answer(str(exc))
