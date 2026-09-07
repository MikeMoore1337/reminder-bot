from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.services.reminder_parser import parse_reminder_input
from app.services.reminder_service import (
    cancel_reminder,
    create_reminder,
    delivery_at_utc,
    format_recurrence,
    snooze_reminder,
)
from app.services.timezone_service import get_or_create_user
from app.utils.datetime_utils import from_utc_to_user, now_in_timezone

router = Router()

REMINDER_FORMAT_HINT = (
    "Не понял формат. Используй, например:\n"
    "/remind 2026-03-31 18:30 Купить молоко\n"
    "напомни завтра в 9 созвон\n"
    "напомни через 2 часа выключить духовку\n"
    "напомни каждые 10 минут проверить сервер\n"
    "напомни каждый день в 9 выпить витамины"
)


def _message_ids(message: Message) -> tuple[int, int] | None:
    if message.from_user is None:
        return None
    return message.from_user.id, message.chat.id


async def _create_and_answer(message: Message, *, show_hint: bool = False) -> None:
    ids = _message_ids(message)
    if ids is None:
        await message.answer("Не удалось определить пользователя")
        return

    telegram_user_id, chat_id = ids
    user = await get_or_create_user(telegram_user_id=telegram_user_id, chat_id=chat_id)

    raw_text = message.text or ""
    now_local = now_in_timezone(user.timezone)
    parsed = parse_reminder_input(raw_text, now_local=now_local)
    if parsed is None:
        if show_hint or raw_text.strip().lower().startswith("напомни"):
            await message.answer(REMINDER_FORMAT_HINT)
        return

    try:
        reminder = await create_reminder(
            user=user,
            local_dt=parsed.local_dt,
            text=parsed.text,
            recurrence_type=parsed.recurrence_type,
            recurrence_interval=parsed.recurrence_interval,
            datetime_semantics=parsed.datetime_semantics,
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return

    local_dt = from_utc_to_user(reminder.remind_at_utc, user.timezone)
    await message.answer(
        "Напоминание сохранено.\n"
        f"ID: {reminder.id}\n"
        f"Когда: {local_dt.strftime('%d.%m.%Y %H:%M')}\n"
        f"Повтор: {format_recurrence(reminder)}\n"
        f"Текст: {escape(reminder.text)}\n"
        f"Часовой пояс: {user.timezone}"
    )


@router.message(Command("remind"))
async def cmd_remind(message: Message) -> None:
    await _create_and_answer(message, show_hint=True)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Используй так: /cancel 12")
        return

    ids = _message_ids(message)
    if ids is None:
        await message.answer("Не удалось определить пользователя")
        return

    reminder_id = int(parts[1])
    telegram_user_id, chat_id = ids
    user = await get_or_create_user(telegram_user_id=telegram_user_id, chat_id=chat_id)
    deleted = await cancel_reminder(user=user, reminder_id=reminder_id)
    if deleted:
        await message.answer(f"Напоминание {reminder_id} удалено")
    else:
        await message.answer("Напоминание с таким id не найдено")


@router.callback_query(F.data.startswith("reminder:"))
async def reminder_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    parts = data.split(":", maxsplit=2)
    if len(parts) != 3:
        await callback.answer("Некорректная команда", show_alert=True)
        return

    _, action, reminder_id_raw = parts
    if not reminder_id_raw.isdigit():
        await callback.answer("Некорректный id", show_alert=True)
        return

    if not isinstance(callback.message, Message):
        await callback.answer("Сообщение больше недоступно", show_alert=True)
        return

    reminder_id = int(reminder_id_raw)
    callback_message = callback.message
    user = await get_or_create_user(
        telegram_user_id=callback.from_user.id,
        chat_id=callback_message.chat.id,
    )

    if action == "delete":
        deleted = await cancel_reminder(user=user, reminder_id=reminder_id)
        await callback.answer("Удалено" if deleted else "Уже удалено", show_alert=False)
        await callback_message.edit_reply_markup(reply_markup=None)
        return

    if action == "snooze":
        reminder = await snooze_reminder(user=user, reminder_id=reminder_id, minutes=10)
        if reminder is None:
            await callback.answer("Напоминание не найдено", show_alert=True)
            return

        local_dt = from_utc_to_user(delivery_at_utc(reminder), user.timezone)
        await callback.answer("Отложено на 10 минут", show_alert=False)
        await callback_message.edit_reply_markup(reply_markup=None)
        await callback_message.answer(
            f"Напоминание {reminder.id} отложено до {local_dt.strftime('%d.%m.%Y %H:%M')}"
        )
        return

    await callback.answer("Неизвестное действие", show_alert=True)


@router.message()
async def text_reminder_handler(message: Message) -> None:
    await _create_and_answer(message)
