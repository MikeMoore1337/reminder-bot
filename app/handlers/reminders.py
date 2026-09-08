from __future__ import annotations

import logging
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.callbacks import (
    CallbackAction,
    CallbackOrigin,
    CallbackTarget,
    ReminderCallback,
    parse_callback,
)
from app.db.models import Reminder, User, VoiceReminderDraft
from app.keyboards.voice import voice_draft_kb
from app.services import reminder_service
from app.services.clarification_service import (
    CLARIFICATION_ORIGIN_VOICE,
    cancel_clarification,
    consume_clarification_and_create_reminder,
    create_clarification,
    get_active_clarification,
)
from app.services.message_context import (
    MessageContextSnapshot,
    build_context_clarification,
    context_reminder_text,
    deserialize_context_snapshot,
    extract_message_context,
    format_context_for_delivery,
    is_contextual_message,
    parse_context_clarification_answer,
    parse_context_reminder_input,
)
from app.services.reminder_parser import (
    ClarificationRequest,
    ParsedReminder,
    parse_clarification_answer,
    parse_reminder_input,
)
from app.services.reminder_service import (
    apply_custom_snooze_draft,
    apply_edit_draft,
    calculate_snooze_target,
    cancel_active_action_drafts,
    cancel_reminder,
    complete_reminder,
    create_action_draft,
    create_reminder,
    delivery_at_utc,
    disable_persistent_reminder,
    get_owned_occurrence_target,
    get_owned_reminder,
    parse_edit_schedule,
    pause_reminder,
    resume_reminder,
    snooze_reminder,
    update_action_draft,
)
from app.services.timezone_service import get_or_create_user
from app.services.voice_service import (
    VoiceProcessingError,
    bind_voice_preview_message,
    cancel_voice_reminder_draft,
    confirm_voice_draft,
    consume_voice_clarification_to_draft,
    discard_voice_draft,
    format_voice_draft_preview,
    process_voice_message,
)
from app.utils.datetime_utils import from_utc_to_user, now_in_timezone, utc_now
from app.workers.reminder_worker import snooze_presets_kb

logger = logging.getLogger(__name__)
router = Router()

REMINDER_FORMAT_HINT = (
    "Не понял формат. Используй, например:\n"
    "/remind 2026-03-31 18:30 Купить молоко\n"
    "напомни завтра в 9 созвон\n"
    "напомни через 2 часа выключить духовку\n"
    "напомни каждые 10 минут проверить сервер\n"
    "напомни каждый день в 9 выпить витамины\n"
    "напомни важное завтра в 9 позвонить"
)

STALE_FEEDBACK = "Это действие уже неактуально"
CLARIFICATION_STALE_FEEDBACK = "Это уточнение уже обработано или истекло"


def _message_ids(message: Message) -> tuple[int, int] | None:
    if message.from_user is None:
        return None
    return message.from_user.id, message.chat.id


async def _safe_remove_keyboard(message: Message) -> None:
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.info(
            "Unable to invalidate Telegram action keyboard",
            extra={"extra_data": f"error_type={type(exc).__name__[:80]}"},
        )


def _message_input_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


def _context_response(context: MessageContextSnapshot) -> str:
    return (
        "Контекст сохранён.\n"
        f"{escape(format_context_for_delivery(context))}\n\n"
        "Когда напомнить? Например: «завтра в 9» или «через 2 часа»."
    )


def _saved_reminder_response(
    reminder: Reminder,
    user: User,
    *,
    prefix: str,
    context: MessageContextSnapshot | None = None,
) -> str:
    local_dt = from_utc_to_user(reminder.remind_at_utc, user.timezone)
    mode_line = (
        f"Режим: {reminder_service.format_mode(reminder)}\n"
        if reminder_service.is_persistent_mode(reminder.mode)
        else ""
    )
    response = (
        f"{prefix}\n"
        f"ID: {reminder.id}\n"
        f"Когда: {local_dt.strftime('%d.%m.%Y %H:%M')}\n"
        f"Повтор: {reminder_service.format_recurrence(reminder)}\n"
        f"{mode_line}"
        f"Текст: {escape(reminder.text)}\n"
        f"Часовой пояс: {escape(user.timezone)}"
    )
    if context is not None:
        response += f"\n\n{escape(format_context_for_delivery(context))}"
    return response


async def _create_and_answer(
    message: Message,
    *,
    show_hint: bool = False,
    context: MessageContextSnapshot | None = None,
) -> None:
    ids = _message_ids(message)
    if ids is None:
        await message.answer("Не удалось определить пользователя")
        return

    telegram_user_id, chat_id = ids
    user = await get_or_create_user(telegram_user_id=telegram_user_id, chat_id=chat_id)

    if context is None and is_contextual_message(message):
        context = extract_message_context(message)

    raw_text = _message_input_text(message)
    now_local = now_in_timezone(user.timezone)
    parsed = (
        parse_context_reminder_input(raw_text, now_local=now_local)
        if context is not None
        else parse_reminder_input(raw_text, now_local=now_local)
    )
    if isinstance(parsed, ClarificationRequest):
        await create_clarification(user, parsed, context_snapshot=context)
        await message.answer(
            f"{parsed.prompt}\n\n{escape(format_context_for_delivery(context))}"
            if context is not None
            else parsed.prompt
        )
        return
    if parsed is None:
        if context is not None:
            request = build_context_clarification()
            await create_clarification(user, request, context_snapshot=context)
            await message.answer(_context_response(context))
            return
        if show_hint or raw_text.strip().lower().startswith("напомни"):
            await message.answer(REMINDER_FORMAT_HINT)
        return
    if not isinstance(parsed, ParsedReminder):
        return

    if context is not None and parsed.text == "__telegram_context__":
        parsed.text = context_reminder_text(context)

    await cancel_active_action_drafts(user)
    await cancel_clarification(user)
    await cancel_voice_reminder_draft(user)
    try:
        reminder = await create_reminder(
            user=user,
            local_dt=parsed.local_dt,
            text=parsed.text,
            recurrence_type=parsed.recurrence_type,
            recurrence_interval=parsed.recurrence_interval,
            datetime_semantics=parsed.datetime_semantics,
            recurrence_rule=parsed.recurrence_rule,
            recurrence_day_of_month=parsed.recurrence_day_of_month,
            context=context,
            mode=parsed.mode,
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return

    await message.answer(
        _saved_reminder_response(
            reminder,
            user,
            prefix="Напоминание сохранено.",
            context=context,
        )
    )


async def _handle_clarification(
    message: Message,
    user: User,
    *,
    now_utc: datetime | None = None,
) -> bool:
    current_time = now_utc or utc_now()
    clarification = await get_active_clarification(user, now_utc=current_time)
    if clarification is None:
        return False

    raw_value = _message_input_text(message)
    context = deserialize_context_snapshot(clarification.context_snapshot)
    parsed = (
        parse_context_clarification_answer(
            clarification.raw_text,
            raw_value,
            now_local=from_utc_to_user(current_time, user.timezone),
        )
        if context is not None
        else parse_clarification_answer(
            clarification.raw_text,
            raw_value,
            now_local=from_utc_to_user(current_time, user.timezone),
        )
    )
    if not isinstance(parsed, ParsedReminder):
        await message.answer(clarification.prompt)
        return True

    if context is not None and parsed.text == "__telegram_context__":
        parsed.text = context_reminder_text(context)

    if clarification.origin == CLARIFICATION_ORIGIN_VOICE:
        try:
            draft = await consume_voice_clarification_to_draft(
                user=user,
                clarification_id=clarification.id,
                raw_text=clarification.raw_text,
                parsed=parsed,
                now_utc=current_time,
            )
        except ValueError as exc:
            await message.answer(str(exc))
            return True
        if draft is None:
            await message.answer(CLARIFICATION_STALE_FEEDBACK)
            return True
        await _send_voice_preview(message, user, draft, now_utc=current_time)
        return True

    try:
        reminder = await consume_clarification_and_create_reminder(
            user=user,
            clarification_id=clarification.id,
            raw_text=clarification.raw_text,
            parsed=parsed,
            now_utc=current_time,
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return True

    if reminder is None:
        await message.answer(CLARIFICATION_STALE_FEEDBACK)
        return True

    await message.answer(
        _saved_reminder_response(
            reminder,
            user,
            prefix="Напоминание сохранено после уточнения.",
            context=context,
        )
    )
    return True


async def _handle_action_draft(message: Message, user: User) -> bool:
    draft = await reminder_service.get_active_action_draft(user)
    if draft is None:
        return False

    raw_value = (message.text or "").strip()
    if draft.action_type == "snooze":
        try:
            reminder = await apply_custom_snooze_draft(user, draft, raw_value)
        except ValueError as exc:
            await message.answer(str(exc))
            return True
        if reminder is None:
            await message.answer(STALE_FEEDBACK)
        else:
            local_dt = from_utc_to_user(delivery_at_utc(reminder), user.timezone)
            await message.answer(f"Отложено до {local_dt.strftime('%d.%m.%Y %H:%M')}")
        return True

    if draft.action_type != "edit":
        return False

    if draft.current_step == "text":
        if not raw_value or len(raw_value) > reminder_service.MAX_REMINDER_TEXT_LENGTH:
            await message.answer("Текст должен содержать от 1 до 4096 символов")
            return True
        payload = reminder_service._payload_dict(draft.payload)
        payload["text"] = raw_value
        await update_action_draft(
            user,
            draft.id,
            current_step="schedule",
            payload=payload,
        )
        await message.answer(
            "Текст сохранён в черновике. Теперь отправь новое время/расписание "
            "или напиши «без изменений»."
        )
        return True

    if draft.current_step == "schedule":
        parsed = parse_edit_schedule(raw_value, now_local=now_in_timezone(user.timezone))
        if (
            raw_value.lower() not in {"без изменений", "без изменения", "оставить"}
            and parsed is None
        ):
            await message.answer(
                "Не понял расписание. Пример: 2026-09-08 18:00 или «каждый день в 9»."
            )
            return True
        try:
            reminder = await apply_edit_draft(
                user,
                draft,
                local_dt=parsed.local_dt if parsed is not None else None,
                recurrence_type=parsed.recurrence_type if parsed is not None else None,
                recurrence_interval=parsed.recurrence_interval if parsed is not None else None,
                datetime_semantics=parsed.datetime_semantics
                if parsed is not None
                else "wall_clock",
                recurrence_rule=parsed.recurrence_rule if parsed is not None else None,
                recurrence_day_of_month=parsed.recurrence_day_of_month
                if parsed is not None
                else None,
            )
        except ValueError as exc:
            await message.answer(str(exc))
            return True
        await message.answer("Изменения сохранены" if reminder is not None else STALE_FEEDBACK)
        return True

    return False


@router.message(Command("remind"))
async def cmd_remind(message: Message) -> None:
    await _create_and_answer(message, show_hint=True)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    ids = _message_ids(message)
    if ids is None:
        await message.answer("Не удалось определить пользователя")
        return
    telegram_user_id, chat_id = ids
    user = await get_or_create_user(telegram_user_id=telegram_user_id, chat_id=chat_id)

    if len(parts) == 1:
        cancelled_actions = await cancel_active_action_drafts(user)
        cancelled_clarification = await cancel_clarification(user)
        cancelled_voice = await cancel_voice_reminder_draft(user)
        await message.answer(
            "Текущий сценарий отменён"
            if cancelled_actions or cancelled_clarification or cancelled_voice
            else "Нет активного сценария"
        )
        return

    if not parts[1].isdigit():
        await message.answer("Используй так: /cancel 12 или /cancel для отмены сценария")
        return

    reminder_id = int(parts[1])
    cancelled = await cancel_reminder(user=user, reminder_id=reminder_id)
    await message.answer(
        f"Напоминание {reminder_id} удалено" if cancelled else "Напоминание с таким id не найдено"
    )


def _voice_saved_text(reminder: Reminder, timezone_name: str) -> str:
    local_dt = from_utc_to_user(reminder.remind_at_utc, timezone_name)
    mode_line = (
        f"Режим: {reminder_service.format_mode(reminder)}\n"
        if reminder_service.is_persistent_mode(reminder.mode)
        else ""
    )
    response = (
        "Напоминание сохранено из голосового сообщения.\n"
        f"ID: {reminder.id}\n"
        f"Когда: {local_dt.strftime('%d.%m.%Y %H:%M')}\n"
        f"Повтор: {reminder_service.format_recurrence(reminder)}\n"
        f"{mode_line}"
        f"Текст: {escape(reminder.text)}\n"
        f"Часовой пояс: {escape(timezone_name)}"
    )
    return response


async def _send_voice_preview(
    message: Message,
    user: User,
    draft: VoiceReminderDraft,
    *,
    now_utc: datetime | None = None,
) -> bool:
    sent = await message.answer(
        format_voice_draft_preview(draft),
        reply_markup=voice_draft_kb(draft.id, draft.action_revision),
    )
    preview_message_id = getattr(sent, "message_id", None)
    if not isinstance(preview_message_id, int) or preview_message_id <= 0:
        return False
    bound = await bind_voice_preview_message(
        user,
        draft.id,
        revision=draft.action_revision,
        message_id=preview_message_id,
        now_utc=now_utc,
    )
    if not bound:
        await _safe_remove_keyboard(sent)
    return bound


async def _handle_voice_callback(callback: CallbackQuery, parsed: ReminderCallback) -> None:
    if parsed.origin != CallbackOrigin.VOICE or parsed.action not in {
        CallbackAction.CREATE,
        CallbackAction.CANCEL,
    }:
        await callback.answer(STALE_FEEDBACK, show_alert=False)
        return
    if not isinstance(callback.message, Message) or callback.from_user is None:
        await callback.answer(STALE_FEEDBACK, show_alert=False)
        return

    callback_message = callback.message
    user = await get_or_create_user(
        telegram_user_id=callback.from_user.id,
        chat_id=callback_message.chat.id,
    )
    if parsed.action == CallbackAction.CREATE:
        try:
            reminder = await confirm_voice_draft(
                user,
                parsed.target_id,
                expected_revision=parsed.revision,
                expected_message_id=callback_message.message_id,
            )
        except ValueError:
            await callback.answer("Не удалось сохранить", show_alert=False)
            await callback_message.answer(
                "Не удалось сохранить это напоминание. Отправь новое голосовое сообщение "
                "с будущей датой и временем."
            )
            return
        if reminder is None:
            await callback.answer(STALE_FEEDBACK, show_alert=False)
            return
        await callback.answer("Напоминание сохранено", show_alert=False)
        await _safe_remove_keyboard(callback_message)
        await callback_message.answer(_voice_saved_text(reminder, user.timezone))
        return

    cancelled = await discard_voice_draft(
        user,
        parsed.target_id,
        expected_revision=parsed.revision,
        expected_message_id=callback_message.message_id,
    )
    await callback.answer("Черновик отменён" if cancelled else STALE_FEEDBACK, show_alert=False)
    if cancelled:
        await _safe_remove_keyboard(callback_message)


@router.message(F.voice)
async def voice_reminder_handler(message: Message, bot: Bot) -> None:
    ids = _message_ids(message)
    if ids is None or message.voice is None:
        await message.answer("Не удалось определить голосовое сообщение")
        return

    user = await get_or_create_user(telegram_user_id=ids[0], chat_id=ids[1])
    try:
        result = await process_voice_message(
            bot,
            user,
            message.voice,
            source_message_id=message.message_id,
        )
    except VoiceProcessingError as exc:
        await message.answer(exc.public_message)
        return

    if result.draft is None:
        if result.message:
            await message.answer(result.message)
        return

    await _send_voice_preview(message, user, result.draft)


async def _resolve_callback_target(
    parsed: ReminderCallback,
    user: User,
) -> tuple[int, int | None, datetime | None, int | None] | None:
    if parsed.target == CallbackTarget.REMINDER:
        if await get_owned_reminder(user, parsed.target_id) is None:
            return None
        return parsed.target_id, None, None, None
    target = await get_owned_occurrence_target(user, parsed.target_id)
    if target is None:
        return None
    reminder_id, occurrence_at_utc, message_id = target
    return reminder_id, parsed.target_id, occurrence_at_utc, message_id


def _expected_delivery_message_id(
    parsed: ReminderCallback,
    occurrence_id: int | None,
    callback_message_id: int,
) -> int | None:
    """Use Telegram message identity only for controls on delivery messages.

    ``/list`` renders a new control message for an existing occurrence. Its
    message id is not part of the persisted delivery identity and must not be
    compared with the original delivery message.
    """

    if occurrence_id is None or parsed.origin != CallbackOrigin.DELIVERY:
        return None
    return callback_message_id


@router.callback_query(F.data)
async def reminder_callback(callback: CallbackQuery) -> None:
    parsed = parse_callback(callback.data)
    if parsed is None:
        reminder_service.record_malformed_callback()
        await callback.answer(STALE_FEEDBACK, show_alert=False)
        return

    if parsed.target == CallbackTarget.VOICE_DRAFT:
        await _handle_voice_callback(callback, parsed)
        return

    if not isinstance(callback.message, Message):
        await callback.answer(STALE_FEEDBACK, show_alert=False)
        return

    callback_message = callback.message
    user = await get_or_create_user(
        telegram_user_id=callback.from_user.id,
        chat_id=callback_message.chat.id,
    )
    target = await _resolve_callback_target(parsed, user)
    if target is None:
        reminder_service.record_unauthorized_callback()
        await callback.answer(STALE_FEEDBACK, show_alert=False)
        return
    reminder_id, occurrence_id, occurrence_at_utc, _stored_message_id = target
    expected_message_id = _expected_delivery_message_id(
        parsed,
        occurrence_id,
        callback_message.message_id,
    )

    if parsed.action == CallbackAction.SNOOZE:
        if not await reminder_service.validate_action_target(
            user,
            reminder_id,
            action=parsed.action.value,
            expected_revision=parsed.revision,
            expected_occurrence_id=occurrence_id,
            expected_occurrence_at_utc=occurrence_at_utc,
            expected_message_id=expected_message_id,
        ):
            await callback.answer(STALE_FEEDBACK, show_alert=False)
            return
        await callback.answer("Выбери время", show_alert=False)
        await callback_message.edit_reply_markup(
            reply_markup=snooze_presets_kb(
                reminder_id,
                occurrence_id=occurrence_id,
                revision=parsed.revision,
                origin=parsed.origin,
            )
        )
        return

    if parsed.action in {
        CallbackAction.SNOOZE_10,
        CallbackAction.SNOOZE_1H,
        CallbackAction.SNOOZE_EVENING,
        CallbackAction.SNOOZE_TOMORROW,
    }:
        preset = {
            CallbackAction.SNOOZE_10: "10m",
            CallbackAction.SNOOZE_1H: "1h",
            CallbackAction.SNOOZE_EVENING: "evening",
            CallbackAction.SNOOZE_TOMORROW: "tomorrow",
        }[parsed.action]
        target_at_utc = calculate_snooze_target(
            preset,
            now_utc=reminder_service.utc_now(),
            timezone_name=user.timezone,
        )
        reminder = await snooze_reminder(
            user,
            reminder_id,
            expected_message_id=expected_message_id,
            expected_revision=parsed.revision,
            expected_occurrence_id=occurrence_id,
            expected_occurrence_at_utc=occurrence_at_utc,
            target_at_utc=target_at_utc,
        )
        if reminder is None:
            await callback.answer(STALE_FEEDBACK, show_alert=False)
            return
        local_dt = from_utc_to_user(target_at_utc, user.timezone)
        await callback.answer("Отложено", show_alert=False)
        await _safe_remove_keyboard(callback_message)
        await callback_message.answer(f"Отложено до {local_dt.strftime('%d.%m.%Y %H:%M')}")
        return

    if parsed.action == CallbackAction.SNOOZE_CUSTOM:
        payload = {"occurrence_id": occurrence_id} if occurrence_id is not None else {}
        draft = await create_action_draft(
            user,
            reminder_id,
            action_type="snooze",
            expected_action_revision=parsed.revision,
            expected_occurrence_at_utc=occurrence_at_utc,
            expected_message_id=expected_message_id,
            expected_occurrence_id=occurrence_id,
            current_step="time",
            payload=payload,
        )
        if draft is None:
            await callback.answer(STALE_FEEDBACK, show_alert=False)
            return
        await callback.answer("Жду дату и время", show_alert=False)
        await _safe_remove_keyboard(callback_message)
        await callback_message.answer(
            "Отправь дату и время, например: 2026-09-08 18:00 или «завтра в 9».\n"
            "Для отмены: /cancel"
        )
        return

    if parsed.action == CallbackAction.DONE:
        if occurrence_id is None:
            await callback.answer(STALE_FEEDBACK, show_alert=False)
            return
        completed = await complete_reminder(
            user,
            reminder_id,
            expected_revision=parsed.revision,
            expected_occurrence_id=occurrence_id,
            expected_message_id=expected_message_id,
        )
        await callback.answer("Готово" if completed else STALE_FEEDBACK, show_alert=False)
        if completed:
            await _safe_remove_keyboard(callback_message)
        return

    if parsed.action == CallbackAction.EDIT:
        payload = {"occurrence_id": occurrence_id} if occurrence_id is not None else {}
        draft = await create_action_draft(
            user,
            reminder_id,
            action_type="edit",
            expected_action_revision=parsed.revision,
            expected_occurrence_at_utc=occurrence_at_utc,
            expected_message_id=expected_message_id,
            expected_occurrence_id=occurrence_id,
            current_step="text",
            payload=payload,
        )
        if draft is None:
            await callback.answer(STALE_FEEDBACK, show_alert=False)
            return
        await callback.answer("Жду новый текст", show_alert=False)
        await _safe_remove_keyboard(callback_message)
        await callback_message.answer(
            "Отправь новый текст напоминания. Следующим сообщением можно будет изменить время.\n"
            "Для отмены: /cancel"
        )
        return

    if parsed.action == CallbackAction.PAUSE:
        paused = await pause_reminder(
            user,
            reminder_id,
            expected_revision=parsed.revision,
            expected_occurrence_id=occurrence_id,
            expected_occurrence_at_utc=occurrence_at_utc,
            expected_message_id=expected_message_id,
        )
        await callback.answer(
            "Напоминание приостановлено" if paused else STALE_FEEDBACK,
            show_alert=False,
        )
        if paused:
            await _safe_remove_keyboard(callback_message)
        return

    if parsed.action == CallbackAction.RESUME:
        resumed = await resume_reminder(user, reminder_id, expected_revision=parsed.revision)
        await callback.answer(
            "Напоминание продолжено" if resumed else STALE_FEEDBACK,
            show_alert=False,
        )
        if resumed:
            await _safe_remove_keyboard(callback_message)
        return

    if parsed.action == CallbackAction.DISABLE_PERSISTENT:
        disabled = await disable_persistent_reminder(
            user,
            reminder_id,
            expected_revision=parsed.revision,
            expected_occurrence_id=occurrence_id,
            expected_occurrence_at_utc=occurrence_at_utc,
            expected_message_id=expected_message_id,
        )
        await callback.answer(
            "Повторы выключены" if disabled else STALE_FEEDBACK,
            show_alert=False,
        )
        if disabled:
            await _safe_remove_keyboard(callback_message)
        return

    if parsed.action == CallbackAction.DELETE:
        cancelled = await cancel_reminder(
            user,
            reminder_id,
            expected_revision=parsed.revision,
            expected_occurrence_id=occurrence_id,
            expected_occurrence_at_utc=occurrence_at_utc,
            expected_message_id=expected_message_id,
        )
        await callback.answer(
            "Напоминание удалено" if cancelled else STALE_FEEDBACK, show_alert=False
        )
        if cancelled:
            await _safe_remove_keyboard(callback_message)
        return

    await callback.answer(STALE_FEEDBACK, show_alert=False)


@router.message()
async def text_reminder_handler(message: Message) -> None:
    ids = _message_ids(message)
    if ids is not None:
        user = await get_or_create_user(telegram_user_id=ids[0], chat_id=ids[1])
        if await _handle_action_draft(message, user):
            return
        if await _handle_clarification(message, user):
            return
    if is_contextual_message(message):
        await _create_and_answer(message, context=extract_message_context(message))
        return
    await _create_and_answer(message)
