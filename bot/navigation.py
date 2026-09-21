"""Shared inline navigation, independent of handler registration order."""
from maxapi.types import ButtonsPayload, CallbackButton


def menu_rows() -> list[list[CallbackButton]]:
    return [
        [CallbackButton(text="Афиша", payload="feed:browse:feed|0"),
         CallbackButton(text="Понравившиеся", payload="feed:browse:liked|0")],
        [CallbackButton(text="Мои планы", payload="feed:browse:plans|0"),
         CallbackButton(text="Мой профиль", payload="onboarding:profile:show")],
    ]


def menu() -> list:
    return [ButtonsPayload(buttons=menu_rows()).pack()]


def profile_offer() -> list:
    return [ButtonsPayload(buttons=[
        [CallbackButton(text="Создать / продолжить профиль", payload="onboarding:profile:create")],
        [CallbackButton(text="Пока нет, к планам", payload="feed:browse:plans|0")],
    ]).pack()]
