"""Persistence operations for the MAX user onboarding flow."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import City, Tag
from ..social_models import User, UserConsent, UserTagWeight


class OnboardingError(ValueError):
    """The requested onboarding transition is not valid."""


INITIAL_INTEREST_WEIGHT = Decimal("1.0")


class OnboardingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_user(self, *, max_user_id: int, max_username: str | None) -> User:
        user = await self.session.scalar(select(User).where(User.max_user_id == max_user_id))
        if user is None:
            user = User(
                max_user_id=max_user_id,
                max_username=max_username,
                profile_status="guest",
                onboarding_step="consent",
            )
            self.session.add(user)
            await self.session.flush()
        else:
            user.max_username = max_username
        return user

    async def get_user(self, max_user_id: int) -> User | None:
        return await self.session.scalar(select(User).where(User.max_user_id == max_user_id))

    async def has_consent(self, user_id: UUID, document_version: str) -> bool:
        consent = await self.session.scalar(
            select(UserConsent.id).where(
                UserConsent.user_id == user_id,
                UserConsent.document_version == document_version,
                UserConsent.revoked_at.is_(None),
            )
        )
        return consent is not None

    async def accept_consent(self, user_id: UUID, document_version: str) -> None:
        user = await self.require_user(user_id)
        if not await self.has_consent(user_id, document_version):
            self.session.add(UserConsent(user_id=user_id, document_version=document_version))
        if user.onboarding_step == "consent":
            user.onboarding_step = "city"

    async def list_cities(self) -> list[City]:
        return list((await self.session.scalars(select(City).order_by(City.name))).all())

    async def choose_city(self, user_id: UUID, city_id: UUID) -> None:
        if await self.session.get(City, city_id) is None:
            raise OnboardingError("Город больше недоступен")
        user = await self.require_user(user_id)
        if user.onboarding_step == "edit_city" and user.profile_status == "active":
            user.city_id = city_id
            user.onboarding_step = "complete"
            return
        if user.onboarding_step not in {"consent", "city"} or user.city_id is not None:
            raise OnboardingError("Этот шаг уже неактуален")
        user.city_id = city_id
        user.onboarding_step = "profile_choice"

    async def continue_as_guest(self, user_id: UUID) -> None:
        user = await self._require_step(user_id, "profile_choice")
        user.profile_status = "guest"
        user.onboarding_step = "complete"

    async def begin_profile(self, user_id: UUID) -> None:
        user = await self.require_user(user_id)
        if user.profile_status == "draft":
            return
        if user.profile_status != "guest" or user.onboarding_step not in {"profile_choice", "complete"}:
            raise OnboardingError("Этот шаг уже неактуален")
        if user.city_id is None:
            raise OnboardingError("Сначала выбери город")
        user.profile_status = "draft"
        user.onboarding_step = "name"
        user.name = None
        user.description = None
        user.gender = None
        user.age = None
        user.photo_url = None
        user.photo_attachment = None

    async def begin_edit(self, user_id: UUID, field: str) -> None:
        user = await self.require_user(user_id)
        if user.profile_status != "active" or field not in {"name", "gender", "age", "description", "photo", "city", "interests"}:
            raise OnboardingError("Редактирование недоступно")
        user.onboarding_step = f"edit_{field}"

    async def finish_edit(self, user_id: UUID) -> None:
        user = await self.require_user(user_id)
        if user.profile_status != "active":
            raise OnboardingError("Сначала заполни анкету")
        if user.onboarding_step == "edit_interests" and len(await self.selected_interest_ids(user_id)) < 3:
            raise OnboardingError("Выбери хотя бы 3 интереса")
        user.onboarding_step = "complete"

    @staticmethod
    def _advance(user: User, next_step: str) -> None:
        user.onboarding_step = "complete" if (user.onboarding_step or "").startswith("edit_") else next_step

    async def set_name(self, user_id: UUID, value: str) -> None:
        name = value.strip()
        if not 1 <= len(name) <= 100:
            raise OnboardingError("Имя должно быть от 1 до 100 символов")
        user = await self._require_step(user_id, "name")
        user.name = name
        self._advance(user, "gender")

    async def set_gender(self, user_id: UUID, value: str) -> None:
        if value not in {"male", "female", "other"}:
            raise OnboardingError("Некорректный вариант пола")
        user = await self._require_step(user_id, "gender")
        user.gender = value
        self._advance(user, "age")

    async def set_age(self, user_id: UUID, value: int) -> None:
        if not 14 <= value <= 120:
            raise OnboardingError("Укажи возраст числом от 14 до 120")
        user = await self._require_step(user_id, "age")
        user.age = value
        self._advance(user, "description")

    async def set_description(self, user_id: UUID, value: str | None) -> None:
        description = value.strip() if value else None
        if description and len(description) > 500:
            raise OnboardingError("Описание не должно быть длиннее 500 символов")
        user = await self._require_step(user_id, "description")
        user.description = description
        self._advance(user, "photo")

    async def set_photo(
        self,
        user_id: UUID,
        *,
        photo_url: str | None,
        photo_attachment: dict[str, Any],
    ) -> None:
        user = await self._require_step(user_id, "photo")
        user.photo_url = photo_url
        user.photo_attachment = photo_attachment
        self._advance(user, "interests")

    async def skip_photo(self, user_id: UUID) -> None:
        user = await self._require_step(user_id, "photo")
        user.photo_url = None
        user.photo_attachment = None
        self._advance(user, "interests")

    async def list_interest_tags(self) -> list[Tag]:
        return list(
            (
                await self.session.scalars(
                    select(Tag).where(Tag.is_active.is_(True), Tag.show_in_onboarding.is_(True)).order_by(Tag.name)
                )
            ).all()
        )

    async def selected_interest_ids(self, user_id: UUID) -> set[UUID]:
        return set(
            (
                await self.session.scalars(
                    select(UserTagWeight.tag_id).where(
                        UserTagWeight.user_id == user_id,
                        UserTagWeight.initial_weight > 0,
                    )
                )
            ).all()
        )

    async def toggle_interest(self, user_id: UUID, tag_id: UUID) -> set[UUID]:
        user = await self._require_step(user_id, "interests")
        tag = await self.session.get(Tag, tag_id)
        if tag is None or not tag.is_active or not tag.show_in_onboarding:
            raise OnboardingError("Этот интерес больше недоступен")
        weight = await self.session.get(UserTagWeight, (user_id, tag_id))
        if weight is None:
            self.session.add(
                UserTagWeight(
                    user_id=user_id,
                    tag_id=tag_id,
                    initial_weight=INITIAL_INTEREST_WEIGHT,
                )
            )
        elif weight.initial_weight > 0:
            if user.profile_status == "active" and len(await self.selected_interest_ids(user_id)) <= 3:
                raise OnboardingError("Оставь хотя бы 3 интереса. Для замены сначала добавь новый")
            if weight.reaction_weight == 0:
                await self.session.delete(weight)
            else:
                weight.initial_weight = Decimal("0")
        else:
            weight.initial_weight = INITIAL_INTEREST_WEIGHT
        await self.session.flush()
        return await self.selected_interest_ids(user_id)

    async def complete_profile(self, user_id: UUID, *, min_interests: int = 1) -> None:
        user = await self._require_step(user_id, "interests")
        if len(await self.selected_interest_ids(user_id)) < min_interests:
            raise OnboardingError(f"Выбери хотя бы {min_interests} интереса")
        if not all((user.city_id, user.name, user.gender, user.age)):
            raise OnboardingError("Анкета заполнена не полностью")
        user.profile_status = "active"
        user.onboarding_step = "complete"

    async def require_user(self, user_id: UUID) -> User:
        user = await self.session.get(User, user_id)
        if user is None:
            raise OnboardingError("Пользователь не найден")
        return user

    async def _require_step(self, user_id: UUID, expected_step: str) -> User:
        user = await self.require_user(user_id)
        if user.onboarding_step != expected_step and not (
            user.profile_status == "active" and user.onboarding_step == f"edit_{expected_step}"
        ):
            raise OnboardingError("Этот шаг уже неактуален")
        return user
