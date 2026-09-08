from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.callbacks import CallbackOrigin
from app.db.models import OccurrenceState, Reminder, User
from app.keyboards.reply import get_main_keyboard, get_timezone_keyboard
from app.services.persistent_policy import is_persistent_mode
from app.services.reminder_service import (
    format_reminder_for_user,
    get_latest_occurrence,
    list_active_reminders,
)
from app.services.timezone_service import (
    get_or_create_user,
    get_user_timezone,
    set_user_timezone,
)
from app.workers.reminder_worker import reminder_actions_kb

router = Router()

START_TEXT = (
    "👋 <b>Привет! Я бот-напоминалка.</b>\n\n"
    "Помогаю не забывать важное:\n"
    "- напоминания на дату и время\n"
    "- напоминания через время\n"
    "- повторяющиеся напоминания по календарным правилам\n\n"
    "📌 <b>Примеры:</b>\n"
    "- напомни через 30 минут проверить духовку\n"
    "- напомни завтра в 9 созвон\n"
    "- напомни каждый день в 10 выпить витамины\n\n"
    "🔔 Важное напоминание: «напомни важное завтра в 9 позвонить».\n\n"
    "Выбери действие кнопкой ниже или просто напиши напоминание текстом."
)

HELP_TEXT = (
    "❓ <b>Как пользоваться ботом</b>\n\n"
    "🕒 <b>Разовые напоминания</b>\n"
    "- напомни 31.03.2026 18:30 купить молоко\n"
    "- напомни завтра в 9 созвон\n"
    "- напомни сегодня в 20 вынести мусор\n\n"
    "⏱ <b>Через время</b>\n"
    "- напомни через 15 минут выключить духовку\n"
    "- напомни через 2 часа выйти на созвон\n\n"
    "🔁 <b>Повторяющиеся</b>\n"
    "- напомни каждый день в 10 выпить витамины\n"
    "- напомни каждый понедельник и четверг в 9 отправить отчёт\n"
    "- напомни по будням в 18 проверить задачи\n"
    "- напомни каждый второй вторник месяца в 10 оплатить счёт\n"
    "- напомни в последнюю пятницу месяца в 18 получить зарплату\n"
    "- напомни каждый год 15 марта в 9 годовщина\n"
    "- напомни завтра в 9 отчёт, через 3 дня после выполнения\n"
    "- повторяющиеся правила могут иметь границу «до 2026-12-31»\n\n"
    "🔔 <b>Важный режим</b>\n"
    "- добавь «важное» после «напомни» или [важное] в конце строки\n"
    "- бот повторяет доставку с ограничением и учитывает тихие часы\n"
    "- остановить повторы можно кнопкой «Выключить повторы», «Готово», «Отложить» или «Удалить»\n\n"
    "📎 <b>Контекст Telegram</b>\n"
    "Ответь на сообщение: «напомни об этом завтра в 9».\n"
    "Можно также переслать ссылку, фото, документ или сообщение без времени — бот сохранит\n"
    "короткий контекст и спросит, когда напомнить.\n\n"
    "⚠️ Если время неоднозначно, бот сначала попросит уточнение.\n\n"
    "📋 <b>Команды</b>\n"
    "/list - список активных напоминаний\n"
    "/cancel ID - удалить напоминание; /cancel - отменить действие\n"
    "/timezone Europe/Moscow - установить часовой пояс\n"
    "/mytimezone - показать текущий часовой пояс\n\n"
    "💡 <b>Подсказка</b>\n"
    "Чем естественнее формулировка - тем удобнее пользоваться ботом."
)

CREATE_REMINDER_HINT = (
    "➕ <b>Создание напоминания</b>\n\n"
    "Просто отправь сообщение в одном из форматов:\n\n"
    "- напомни завтра в 9 созвон\n"
    "- напомни через 30 минут проверить духовку\n"
    "- напомни каждый день в 10 выпить витамины\n"
    "- напомни каждый понедельник и четверг в 9 отправить отчёт\n"
    "- напомни завтра в 9 отчёт, через 3 дня после выполнения\n"
    "- напомни важное завтра в 9 позвонить\n"
    "- /remind 2026-03-31 18:30 Купить молоко"
)

TIMEZONE_HINT = (
    "🌍 <b>Настройка часового пояса</b>\n\n"
    "Выбери популярный вариант кнопкой ниже\n"
    "или отправь свой вручную, например:\n"
    "<code>/timezone Europe/Moscow</code>"
)


def _get_ids(message: Message) -> tuple[int, int]:
    if message.from_user is None:
        raise ValueError("Не удалось определить пользователя")
    return message.from_user.id, message.chat.id


def _render_reminders(reminders: list[Reminder], timezone_name: str) -> str:
    rendered = "\n\n".join(
        f"{index}. {format_reminder_for_user(reminder, timezone_name)}"
        for index, reminder in enumerate(reminders[:20], start=1)
    )
    if len(reminders) > 20:
        rendered += f"\n\nПоказаны первые 20 из {len(reminders)}"
    return rendered


async def _send_actionable_reminders(
    message: Message,
    user: User,
    reminders: list[Reminder],
    timezone_name: str,
) -> None:
    for reminder in reminders[:20]:
        occurrence = None
        latest_occurrence = await get_latest_occurrence(user, reminder.id)
        if (
            latest_occurrence is not None
            and latest_occurrence.status == OccurrenceState.DELIVERED.value
            and latest_occurrence.message_id is not None
            and latest_occurrence.message_id == reminder.last_message_id
            and reminder.last_delivery_occurrence_utc is not None
            and (
                reminder.state == "delivered"
                or (
                    reminder.recurrence_type != "none"
                    and reminder.state in {"scheduled", "snoozed"}
                )
                or (
                    is_persistent_mode(reminder.mode) and reminder.state in {"scheduled", "snoozed"}
                )
            )
        ):
            occurrence = latest_occurrence
        display_state = (
            OccurrenceState.DELIVERED.value if occurrence is not None else reminder.state
        )
        await message.answer(
            format_reminder_for_user(
                reminder,
                timezone_name,
                display_state=display_state,
                display_at_utc=occurrence.delivery_at_utc if occurrence is not None else None,
            ),
            reply_markup=reminder_actions_kb(
                reminder.id,
                occurrence_id=occurrence.id if occurrence is not None else None,
                revision=(
                    occurrence.action_revision
                    if occurrence is not None
                    else reminder.action_revision
                ),
                state=display_state,
                recurrence_type=reminder.recurrence_type,
                origin=CallbackOrigin.LIST,
                mode=reminder.mode,
            ),
            parse_mode="HTML",
        )
    if len(reminders) > 20:
        await message.answer(f"Показаны первые 20 из {len(reminders)}")


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    telegram_user_id, chat_id = _get_ids(message)
    await get_or_create_user(telegram_user_id, chat_id)
    timezone_name = await get_user_timezone(telegram_user_id, chat_id)

    text = f"{START_TEXT}\n\n🕒 <b>Твой часовой пояс:</b> <code>{timezone_name}</code>"

    await message.answer(
        text,
        reply_markup=get_main_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        HELP_TEXT,
        reply_markup=get_main_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("mytimezone"))
async def cmd_mytimezone(message: Message) -> None:
    telegram_user_id, chat_id = _get_ids(message)
    await get_or_create_user(telegram_user_id, chat_id)
    timezone_name = await get_user_timezone(telegram_user_id, chat_id)

    await message.answer(
        f"🌍 Твой текущий часовой пояс: <code>{timezone_name}</code>",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("timezone"))
async def cmd_timezone(message: Message, command: CommandObject) -> None:
    telegram_user_id, chat_id = _get_ids(message)
    await get_or_create_user(telegram_user_id, chat_id)

    if not command.args:
        await message.answer(
            TIMEZONE_HINT,
            reply_markup=get_timezone_keyboard(),
            parse_mode="HTML",
        )
        return

    timezone_name = command.args.strip()

    try:
        updated_user = await set_user_timezone(telegram_user_id, chat_id, timezone_name)
        await message.answer(
            f"✅ Часовой пояс обновлён: <code>{updated_user.timezone}</code>",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML",
        )
    except ValueError as exc:
        await message.answer(
            f"❌ {exc}\n\nПопробуй, например: <code>/timezone Europe/Moscow</code>",
            reply_markup=get_timezone_keyboard(),
            parse_mode="HTML",
        )


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    telegram_user_id, chat_id = _get_ids(message)
    user = await get_or_create_user(telegram_user_id, chat_id)
    reminders = await list_active_reminders(user)

    if not reminders:
        await message.answer(
            "📭 У тебя пока нет активных напоминаний.\n\n"
            "Нажми «➕ Создать напоминание» или просто напиши его текстом.",
            reply_markup=get_main_keyboard(),
        )
        return

    timezone_name = await get_user_timezone(telegram_user_id, chat_id)
    await message.answer("📋 <b>Твои активные напоминания:</b>", parse_mode="HTML")
    await _send_actionable_reminders(message, user, reminders, timezone_name)
    await message.answer(
        "Выбери действие кнопкой выше или обнови список командой /list.",
        reply_markup=get_main_keyboard(),
    )


@router.message(F.text == "➕ Создать напоминание")
async def btn_create_reminder(message: Message) -> None:
    await message.answer(
        CREATE_REMINDER_HINT,
        reply_markup=get_main_keyboard(),
        parse_mode="HTML",
    )


@router.message(F.text == "📋 Мои напоминания")
async def btn_list(message: Message) -> None:
    telegram_user_id, chat_id = _get_ids(message)
    user = await get_or_create_user(telegram_user_id, chat_id)
    reminders = await list_active_reminders(user)

    if not reminders:
        await message.answer(
            "📭 У тебя пока нет активных напоминаний.",
            reply_markup=get_main_keyboard(),
        )
        return

    timezone_name = await get_user_timezone(telegram_user_id, chat_id)
    await message.answer("📋 <b>Твои активные напоминания:</b>", parse_mode="HTML")
    await _send_actionable_reminders(message, user, reminders, timezone_name)
    await message.answer(
        "Выбери действие кнопкой выше или обнови список командой /list.",
        reply_markup=get_main_keyboard(),
    )


@router.message(F.text == "🌍 Часовой пояс")
async def btn_timezone(message: Message) -> None:
    telegram_user_id, chat_id = _get_ids(message)
    await get_or_create_user(telegram_user_id, chat_id)
    timezone_name = await get_user_timezone(telegram_user_id, chat_id)

    await message.answer(
        f"{TIMEZONE_HINT}\n\nСейчас установлен: <code>{timezone_name}</code>",
        reply_markup=get_timezone_keyboard(),
        parse_mode="HTML",
    )


@router.message(F.text == "❓ Помощь")
async def btn_help(message: Message) -> None:
    await message.answer(
        HELP_TEXT,
        reply_markup=get_main_keyboard(),
        parse_mode="HTML",
    )


@router.message(F.text.in_({"Europe/Moscow", "Europe/Helsinki", "Europe/Berlin", "UTC"}))
async def btn_set_popular_timezone(message: Message) -> None:
    telegram_user_id, chat_id = _get_ids(message)
    await get_or_create_user(telegram_user_id, chat_id)
    timezone_name = (message.text or "").strip()

    try:
        updated_user = await set_user_timezone(telegram_user_id, chat_id, timezone_name)
        await message.answer(
            f"✅ Часовой пояс обновлён: <code>{updated_user.timezone}</code>",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML",
        )
    except ValueError as exc:
        await message.answer(
            f"❌ {exc}",
            reply_markup=get_timezone_keyboard(),
        )


@router.message(F.text == "⬅️ Назад")
async def btn_back(message: Message) -> None:
    telegram_user_id, chat_id = _get_ids(message)
    await get_or_create_user(telegram_user_id, chat_id)
    timezone_name = await get_user_timezone(telegram_user_id, chat_id)

    text = f"{START_TEXT}\n\n🕒 <b>Твой часовой пояс:</b> <code>{timezone_name}</code>"

    await message.answer(
        text,
        reply_markup=get_main_keyboard(),
        parse_mode="HTML",
    )
