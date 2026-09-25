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


ERROR_TEXT = "Не получилось выполнить действие. Попробуй ещё раз — твой профиль и планы сохранены."
MENU_PAYLOAD = "feed:menu:show"


def plans_and_feed() -> list:
    return [ButtonsPayload(buttons=[
        [CallbackButton(text="Мои планы", payload="feed:browse:plans|0"),
         CallbackButton(text="Афиша", payload="feed:browse:feed|0")],
    ]).pack()]


def error_actions(retry_payload: str | None) -> list:
    rows = [[CallbackButton(text="Повторить", payload=retry_payload)]] if retry_payload else []
    rows.append([CallbackButton(text="В меню", payload=MENU_PAYLOAD)])
    return [ButtonsPayload(buttons=rows).pack()]


async def report_error(event, retry_payload: str | None, *, answered: bool) -> None:
    """Tell the user an action failed and let them retry instead of a silent button."""
    try:
        if not answered:
            await event.ack()
        await event.bot.send_message(
            user_id=event.callback.user.user_id,
            text=ERROR_TEXT,
            attachments=error_actions(retry_payload),
        )
    except Exception:
        if not answered:
            try:
                await event.ack(ERROR_TEXT)
            except Exception:
                pass


def profile_offer() -> list:
    return [ButtonsPayload(buttons=[
        [CallbackButton(text="Создать / продолжить профиль", payload="onboarding:profile:create")],
        [CallbackButton(text="Пока нет, к планам", payload="feed:browse:plans|0")],
    ]).pack()]
