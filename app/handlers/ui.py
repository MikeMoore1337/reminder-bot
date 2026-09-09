from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile, Message

from app.callbacks import CallbackOrigin
from app.db.models import OccurrenceState, Reminder, User
from app.handlers.guards import require_private_chat
from app.keyboards.adaptive import suggestion_kb
from app.keyboards.reply import get_main_keyboard, get_timezone_keyboard
from app.services import shared_reminder_service
from app.services.adaptive_service import (
    AdaptivePreferences,
    format_suggestion,
    get_adaptive_preferences,
    get_pending_suggestions,
    set_adaptive_preferences,
)
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
from app.telegram_metadata import (
    HELP_TEXT,
    START_TEXT,
    WELCOME_IMAGE_PATH,
)
from app.workers.reminder_worker import reminder_actions_kb

router = Router()
LIST_PAGE_SIZE = 20
logger = logging.getLogger(__name__)

CREATE_REMINDER_HINT = (
    "➕ <b>Создание напоминания</b>\n\n"
    "Просто отправь сообщение в одном из форматов:\n\n"
    "- напомни завтра в 9 созвон\n"
    "- напомни через 30 минут проверить духовку\n"
    "- напомни каждый день в 10 выпить витамины\n"
    "- напомни каждый понедельник и четверг в 9 отправить отчёт\n"
    "- напомни завтра в 9 отчёт, через 3 дня после выполнения\n"
    "- напомни важное завтра в 9 позвонить\n"
    "- /deadline 2026-09-10 18:00 оплатить счёт | за день, за час, в срок\n"
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


async def _send_start_screen(message: Message, text: str) -> None:
    try:
        await message.answer_photo(
            photo=FSInputFile(WELCOME_IMAGE_PATH),
            caption=text,
            reply_markup=get_main_keyboard(),
            parse_mode="HTML",
        )
    except Exception as exc:
        # The welcome image is enhancement-only.  Keep the first interaction
        # usable when the file or Telegram media request is unavailable, and
        # never expose the provider error or its payload to the user/logs.
        logger.warning(
            "Welcome image delivery failed; using text fallback",
            extra={"extra_data": f"error={type(exc).__name__}"},
        )
        await message.answer(
            text,
            reply_markup=get_main_keyboard(),
            parse_mode="HTML",
        )


def _list_page_bounds(total: int, requested_page: int) -> tuple[int, int, int, int]:
    total_pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = min(max(1, requested_page), total_pages)
    start = (page - 1) * LIST_PAGE_SIZE
    return page, total_pages, start, min(start + LIST_PAGE_SIZE, total)


def _parse_list_page(command: CommandObject) -> int:
    value = (command.args or "").strip().split(maxsplit=1)
    if not value:
        return 1
    try:
        return max(1, int(value[0]))
    except ValueError:
        return 1


def _render_reminders(
    reminders: list[Reminder],
    timezone_name: str,
    *,
    page: int = 1,
) -> str:
    current_page, total_pages, start, end = _list_page_bounds(len(reminders), page)
    rendered = "\n\n".join(
        f"{index}. {format_reminder_for_user(reminder, timezone_name)}"
        for index, reminder in enumerate(reminders[start:end], start=start + 1)
    )
    if len(reminders) > LIST_PAGE_SIZE:
        navigation: list[str] = []
        if current_page > 1:
            navigation.append(f"предыдущая: /list {current_page - 1}")
        if current_page < total_pages:
            navigation.append(f"следующая: /list {current_page + 1}")
        rendered += f"\n\nПоказаны {start + 1}–{end} из {len(reminders)} · {'; '.join(navigation)}"
    return rendered


def _adaptive_status_text(preferences: AdaptivePreferences) -> str:
    suggestions = "включены" if preferences.suggestions_enabled else "выключены"
    digests = "включены" if preferences.digests_enabled else "выключены"
    return (
        "💡 <b>Настройки подсказок и дайджестов</b>\n\n"
        f"Подсказки по переносам: <b>{suggestions}</b>\n"
        f"Дайджесты: <b>{digests}</b>\n"
        f"Утро: <code>{preferences.digest_morning_time}</code>, "
        f"вечер: <code>{preferences.digest_evening_time}</code>\n"
        "Тихие часы: "
        f"<code>{preferences.digest_quiet_hours_start}–{preferences.digest_quiet_hours_end}</code>\n\n"
        "Включить: <code>/suggestions on</code> или <code>/digest on</code>.\n"
        "Выключить: <code>/suggestions off</code> или <code>/digest off</code>."
    )


async def _handle_adaptive_toggle(
    message: Message,
    command: CommandObject,
    *,
    feature: str,
) -> None:
    if not await require_private_chat(message):
        return
    telegram_user_id, chat_id = _get_ids(message)
    user = await get_or_create_user(telegram_user_id, chat_id)
    value = (command.args or "").strip().lower()
    enabled: bool | None
    if value in {"on", "вкл", "включить", "да"}:
        enabled = True
    elif value in {"off", "выкл", "выключить", "нет"}:
        enabled = False
    elif value in {"", "status", "статус"}:
        enabled = None
    else:
        await message.answer(
            f"Используй: /{feature} on, /{feature} off или /{feature} status",
            reply_markup=get_main_keyboard(),
        )
        return

    if enabled is None:
        preferences = await get_adaptive_preferences(user)
    elif feature == "suggestions":
        preferences = await set_adaptive_preferences(user, suggestions_enabled=enabled)
    else:
        preferences = await set_adaptive_preferences(user, digests_enabled=enabled)
    if preferences is None:
        await message.answer("Не удалось загрузить настройки")
        return
    await message.answer(_adaptive_status_text(preferences), parse_mode="HTML")


async def _send_actionable_reminders(
    message: Message,
    user: User,
    reminders: list[Reminder],
    timezone_name: str,
    *,
    page: int = 1,
) -> None:
    current_page, total_pages, start, end = _list_page_bounds(len(reminders), page)
    for reminder in reminders[start:end]:
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
                or (
                    reminder.kind == "deadline"
                    and reminder.state in {"scheduled", "snoozed", "paused"}
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
                reminder_kind=reminder.kind,
                deadline_plan_state=reminder.deadline_plan_state,
            ),
            parse_mode="HTML",
        )
    if len(reminders) > LIST_PAGE_SIZE:
        navigation: list[str] = []
        if current_page > 1:
            navigation.append(f"предыдущая: /list {current_page - 1}")
        if current_page < total_pages:
            navigation.append(f"следующая: /list {current_page + 1}")
        await message.answer(
            f"Показаны {start + 1}–{end} из {len(reminders)} · {'; '.join(navigation)}"
        )


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject) -> None:
    payload = (command.args or "").strip()
    if (
        payload.startswith(shared_reminder_service.SHARED_INVITE_PREFIX)
        and getattr(message.chat, "type", None) != "private"
    ):
        await message.answer("❌ Приглашение можно принять только в личном чате с ботом.")
        return
    if not await require_private_chat(message):
        return

    telegram_user_id, chat_id = _get_ids(message)
    user = await get_or_create_user(telegram_user_id, chat_id)
    timezone_name = await get_user_timezone(telegram_user_id, chat_id)

    text = f"{START_TEXT}\n\n🕒 <b>Твой часовой пояс:</b> <code>{timezone_name}</code>"

    if payload.startswith(shared_reminder_service.SHARED_INVITE_PREFIX):
        acceptance = await shared_reminder_service.accept_invite(user, payload)
        if acceptance.accepted:
            text = "✅ Доступ к общему напоминанию предоставлен. Открой /shared.\n\n" + text
        elif acceptance.already_member:
            text = "ℹ️ Ты уже участник этого общего напоминания. Открой /shared.\n\n" + text
        else:
            text = "❌ Приглашение недействительно, отозвано или истекло.\n\n" + text

    await _send_start_screen(message, text)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        HELP_TEXT,
        reply_markup=get_main_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("mytimezone"))
async def cmd_mytimezone(message: Message) -> None:
    if not await require_private_chat(message):
        return
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
    if not await require_private_chat(message):
        return
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


@router.message(Command("suggestions"))
async def cmd_suggestions(message: Message, command: CommandObject) -> None:
    await _handle_adaptive_toggle(message, command, feature="suggestions")


@router.message(Command("digest"))
async def cmd_digest(message: Message, command: CommandObject) -> None:
    await _handle_adaptive_toggle(message, command, feature="digest")


@router.message(Command("list"))
async def cmd_list(message: Message, command: CommandObject) -> None:
    if not await require_private_chat(message):
        return
    telegram_user_id, chat_id = _get_ids(message)
    user = await get_or_create_user(telegram_user_id, chat_id)
    reminders = await list_active_reminders(user)
    page = _parse_list_page(command)
    current_page, _, _, _ = _list_page_bounds(len(reminders), page)

    if not reminders:
        await message.answer(
            "📭 У тебя пока нет активных напоминаний.\n\n"
            "Нажми «➕ Создать напоминание» или просто напиши его текстом.",
            reply_markup=get_main_keyboard(),
        )
        return

    timezone_name = await get_user_timezone(telegram_user_id, chat_id)
    await message.answer("📋 <b>Твои активные напоминания:</b>", parse_mode="HTML")
    await _send_actionable_reminders(message, user, reminders, timezone_name, page=page)
    if current_page == 1:
        for suggestion in await get_pending_suggestions(user, limit=5):
            reminder = next((item for item in reminders if item.id == suggestion.reminder_id), None)
            await message.answer(
                format_suggestion(suggestion, reminder=reminder),
                reply_markup=suggestion_kb(suggestion.id, suggestion.revision),
                parse_mode="HTML",
            )
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
    if not await require_private_chat(message):
        return
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
    for suggestion in await get_pending_suggestions(user, limit=5):
        reminder = next((item for item in reminders if item.id == suggestion.reminder_id), None)
        await message.answer(
            format_suggestion(suggestion, reminder=reminder),
            reply_markup=suggestion_kb(suggestion.id, suggestion.revision),
            parse_mode="HTML",
        )
    await message.answer(
        "Выбери действие кнопкой выше или обнови список командой /list.",
        reply_markup=get_main_keyboard(),
    )


@router.message(F.text == "🌍 Часовой пояс")
async def btn_timezone(message: Message) -> None:
    if not await require_private_chat(message):
        return
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
    if not await require_private_chat(message):
        return
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
    if not await require_private_chat(message):
        return
    telegram_user_id, chat_id = _get_ids(message)
    await get_or_create_user(telegram_user_id, chat_id)
    timezone_name = await get_user_timezone(telegram_user_id, chat_id)

    text = f"{START_TEXT}\n\n🕒 <b>Твой часовой пояс:</b> <code>{timezone_name}</code>"

    await _send_start_screen(message, text)
