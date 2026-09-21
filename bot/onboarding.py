"""MAX message handlers for consent and profile onboarding."""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from maxapi import Dispatcher, F
from maxapi.types import BotStarted, ButtonsPayload, CallbackButton, Command, MessageCallback, MessageCreated
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infrastructure.db.models import City, Tag
from infrastructure.db.repositories import OnboardingError, OnboardingRepository
from .navigation import menu, menu_rows, profile_offer

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

    async def show_profile(answer, uid: UUID) -> None:
        async with session_factory() as session:
            repo = OnboardingRepository(session)
            user = await repo.require_user(uid)
            if user.profile_status == "active" and (user.onboarding_step or "").startswith("edit_"):
                await repo.finish_edit(uid)
                await session.commit()
            city = await session.get(City, user.city_id) if user.city_id else None
            tags = await repo.list_interest_tags()
            selected = await repo.selected_interest_ids(uid)
        if user.profile_status != "active":
            await answer("Для поиска компании нужна анкета. Хочешь её создать? Твои планы и интересы сохранятся.", attachments=profile_offer())
            return
        fields = (("name", "Имя"), ("gender", "Пол"), ("age", "Возраст"),
                  ("description", "Описание"), ("photo", "Фото"), ("city", "Город"), ("interests", "Интересы"))
        gender = {"male": "Мужской", "female": "Женский", "other": "Другой"}.get(user.gender, user.gender)
        text = (f"Твоя анкета\n\n{user.name}, {user.age}\nПол: {gender}\n"
                f"Город: {city.name if city else 'не выбран'}\n"
                f"О себе: {user.description or 'не заполнено'}\n"
                f"Фото: {'добавлено' if user.photo_url or user.photo_attachment else 'не добавлено'}\n"
                f"Интересы: {', '.join(tag.name for tag in tags if tag.id in selected)}\n\nЧто изменить?")
        rows = [[CallbackButton(text=label, payload=f"onboarding:edit:{field}") for field, label in fields[i:i+2]]
                for i in range(0, len(fields), 2)]
        await answer(text, attachments=keyboard(rows + menu_rows()))

    async def resume(event: MessageCreated | BotStarted | MessageCallback, answer) -> None:
        sender = (event.callback.user if isinstance(event, MessageCallback) else
                  event.message.sender if isinstance(event, MessageCreated) else event.user)
        if sender is None:
            return
        uid = await user_id(sender.user_id, sender.username)
        async with session_factory() as session:
            repo = OnboardingRepository(session)
            user = await repo.require_user(uid)
            consent = await repo.has_consent(uid, consent_version)
            cities = await repo.list_cities() if consent and (user.city_id is None or user.onboarding_step == "edit_city") else []
            step = (user.onboarding_step or "").removeprefix("edit_")
        editing = (user.onboarding_step or "").startswith("edit_")

        async def prompt(text: str, rows: list | None = None) -> None:
            rows = list(rows or [])
            if editing:
                rows.append([CallbackButton(text="Назад", payload="onboarding:edit:back")])
            await answer(text, attachments=keyboard(rows) if rows else [])

        if not consent:
            await answer(
                "ДвижМАКС помогает найти событие и компанию для него.\n\n"
                "Мы сохраняем твой MAX ID, город, выбранные интересы и данные анкеты, если ты решишь её создать. "
                "Это нужно для рекомендаций, поиска компании и общения.\n\n"
                "Продолжая, ты соглашаешься на обработку этих данных.",
                attachments=keyboard([[CallbackButton(text="Согласен", payload="onboarding:consent:accept")], [CallbackButton(text="Не согласен", payload="onboarding:consent:decline")]]),
            )
        elif user.city_id is None or step == "city":
            await prompt("Выбери город." if cities else "Каталог городов ещё обновляется. Попробуй чуть позже.",
                         [[CallbackButton(text=city.name, payload=f"onboarding:city:{city.id}")] for city in cities])
        elif step == "profile_choice":
            await answer("Город сохранён.\n\nХочешь создать профиль? С профилем можно искать компанию на мероприятия. Без него доступна только афиша.", attachments=keyboard([[CallbackButton(text="Создать профиль", payload="onboarding:profile:create")], [CallbackButton(text="Только афиша", payload="onboarding:profile:guest")]]))
        elif step == "name":
            await prompt("Как тебя зовут? Это имя увидят другие люди.")
        elif step == "gender":
            await prompt("Выбери пол.", [[CallbackButton(text="Мужской", payload="onboarding:gender:male")], [CallbackButton(text="Женский", payload="onboarding:gender:female")], [CallbackButton(text="Другой", payload="onboarding:gender:other")]])
        elif step == "age":
            await prompt("Сколько тебе лет? Напиши число.")
        elif step == "description":
            await prompt("Расскажи о себе в паре фраз. Это увидят люди, которые ищут компанию.", [[CallbackButton(text="Очистить описание" if editing else "Пропустить", payload="onboarding:description:skip")]])
        elif step == "photo":
            await prompt("Прикрепи новое фото для анкеты." if editing else "Добавь фото для анкеты — так тебя будет проще узнать. Его можно пропустить.", [[CallbackButton(text="Удалить фото" if editing else "Пропустить", payload="onboarding:photo:skip")]])
        elif step == "interests":
            await interest_categories(answer, uid)
        elif step == "complete":
            if user.profile_status == "active":
                await show_profile(answer, uid)
            else:
                await answer("Ты уже в ДвижМАКС. Выбирай мероприятия.", attachments=menu())

    @dispatcher.bot_started()
    async def on_bot_started(event: BotStarted) -> None:
        await resume(event, lambda text, **kwargs: event.bot.send_message(chat_id=event.chat_id, text=text, **kwargs))

    @dispatcher.message_created(Command("start"))
    async def on_start(event: MessageCreated) -> None:
        await resume(event, event.message.answer)

    @dispatcher.message_created(Command("profile"))
    async def on_profile(event: MessageCreated) -> None:
        sender = event.message.sender
        if sender is None:
            return
        uid = await user_id(sender.user_id, sender.username)
        async with session_factory() as session:
            user = await OnboardingRepository(session).require_user(uid)
        if user.city_id is None:
            await resume(event, event.message.answer)
        else:
            await show_profile(event.message.answer, uid)

    @dispatcher.message_callback(F.callback.payload.startswith("onboarding:"))
    async def on_callback(event: MessageCallback) -> None:
        parts = _callback_parts(event.callback.payload)
        if parts is None:
            return
        _, action, value = parts
        uid = await user_id(event.callback.user.user_id, event.callback.user.username)
        try:
            if action == "edit":
                async with session_factory() as session:
                    repo = OnboardingRepository(session)
                    if value == "back":
                        await repo.finish_edit(uid)
                    else:
                        await repo.begin_edit(uid, value)
                    await session.commit()
                await resume(event, event.edit)
                return
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
                if value == "show":
                    await show_profile(event.edit, uid)
                    return
                async with session_factory() as session:
                    repo = OnboardingRepository(session)
                    if value == "guest":
                        await repo.continue_as_guest(uid)
                    elif value == "create":
                        await repo.begin_profile(uid)
                    else:
                        return
                    await session.commit()
                await resume(event, event.edit)
                return
            if action == "gender":
                async with session_factory() as session:
                    await OnboardingRepository(session).set_gender(uid, value)
                    await session.commit()
                await resume(event, event.edit)
                return
            if action == "description" and value == "skip":
                async with session_factory() as session:
                    await OnboardingRepository(session).set_description(uid, None)
                    await session.commit()
                await resume(event, event.edit)
                return
            if action == "photo" and value == "skip":
                async with session_factory() as session:
                    await OnboardingRepository(session).skip_photo(uid)
                    await session.commit()
                await resume(event, event.edit)
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
                        await event.edit("Профиль сохранён. Для поиска компании открой «Мои планы» и выбери событие.", attachments=menu())
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

    @dispatcher.message_created(~F.message.body.text.regexp(r"^\s*/"))
    async def on_message(event: MessageCreated) -> None:
        sender = event.message.sender
        if sender is None or (event.message.body.text or "").strip().startswith("/"):
            return
        uid = await user_id(sender.user_id, sender.username)
        text = (event.message.body.text or "").strip()
        async with session_factory() as session:
            repo = OnboardingRepository(session)
            step = ((await repo.require_user(uid)).onboarding_step or "").removeprefix("edit_")
            try:
                if step == "name":
                    await repo.set_name(uid, text)
                elif step == "age":
                    if not text.isdecimal():
                        raise OnboardingError("Укажи возраст числом от 14 до 120")
                    await repo.set_age(uid, int(text))
                elif step == "description":
                    await repo.set_description(uid, text)
                elif step == "photo":
                    image = _photo(event)
                    if image is None:
                        await event.message.answer("Прикрепи изображение или нажми «Пропустить».", attachments=keyboard([[CallbackButton(text="Пропустить", payload="onboarding:photo:skip")]]))
                        return
                    await repo.set_photo(uid, photo_url=image[0], photo_attachment=image[1])
                else:
                    return
                await session.commit()
            except (OnboardingError, ValueError) as exc:
                await session.rollback()
                await event.message.answer(str(exc))
                return
        await resume(event, event.message.answer)
