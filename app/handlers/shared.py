from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.callbacks import CallbackAction, CallbackOrigin, CallbackTarget, parse_callback
from app.db.models import OccurrenceState, Reminder, ReminderOccurrence
from app.keyboards.shared import revoke_invite_kb, revoke_membership_kb
from app.services import shared_reminder_service
from app.services.reminder_service import (
    MAX_REMINDER_TEXT_LENGTH,
    delivery_at_utc,
    format_state,
)
from app.services.timezone_service import get_or_create_user
from app.utils.datetime_utils import from_utc_to_user
from app.workers.reminder_worker import _escape_bounded, reminder_actions_kb

router = Router()


def _private_chat(message: Message) -> bool:
    return getattr(message.chat, "type", None) == "private"


def _ids(message: Message) -> tuple[int, int]:
    if message.from_user is None:
        raise ValueError("Не удалось определить пользователя")
    return message.from_user.id, message.chat.id


def _shared_reminder_text(
    reminder: Reminder,
    timezone_name: str,
    *,
    display_state: str | None = None,
    display_at_utc: datetime | None = None,
    text_budget: int | None = None,
) -> str:
    local_dt = from_utc_to_user(display_at_utc or delivery_at_utc(reminder), timezone_name)
    prefix = (
        f"ID: {reminder.id}\n"
        f"Состояние: {format_state(display_state or reminder.state)}\n"
        f"Когда: {local_dt.strftime('%d.%m.%Y %H:%M')}\n"
        "Текст: "
    )
    rendered_text = (
        escape(reminder.text)
        if text_budget is None
        else _escape_bounded(reminder.text, max(0, text_budget))
    )
    return prefix + rendered_text


def _entry_markup(
    entry: shared_reminder_service.SharedReminderView,
    occurrence: ReminderOccurrence | None,
) -> InlineKeyboardMarkup | None:
    reminder = entry.reminder
    if occurrence is not None:
        state = OccurrenceState.DELIVERED.value
        occurrence_id = occurrence.id
        revision = occurrence.action_revision
    elif entry.is_owner:
        state = reminder.state
        occurrence_id = None
        revision = reminder.action_revision
    else:
        return None
    return reminder_actions_kb(
        reminder.id,
        occurrence_id=occurrence_id,
        revision=revision,
        state=state,
        recurrence_type=reminder.recurrence_type,
        include_snooze=entry.is_owner or occurrence is not None,
        origin=CallbackOrigin.SHARED,
        mode=reminder.mode,
        reminder_kind=reminder.kind,
        deadline_plan_state=reminder.deadline_plan_state,
        shared_participant=not entry.is_owner,
        membership_id=entry.membership_id if not entry.is_owner else None,
        membership_revision=entry.membership_revision if not entry.is_owner else None,
    )


async def _require_private(message: Message) -> bool:
    if _private_chat(message):
        return True
    await message.answer("Общие напоминания доступны только в личном чате с ботом.")
    return False


def _parse_shared_page(command: CommandObject | None) -> int:
    value = ((command.args if command is not None else None) or "").strip().split(maxsplit=1)
    if not value:
        return 1
    try:
        return min(max(1, int(value[0])), shared_reminder_service.MAX_SHARED_PAGE_NUMBER)
    except ValueError:
        return 1


@router.message(Command("share"))
async def cmd_share(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await _require_private(message):
        return
    telegram_user_id, chat_id = _ids(message)
    owner = await get_or_create_user(telegram_user_id, chat_id)
    raw_id = (command.args or "").strip()
    try:
        reminder_id = int(raw_id)
    except ValueError:
        reminder_id = 0
    if reminder_id < 1 or raw_id != str(reminder_id):
        await message.answer("Используй: <code>/share ID</code>")
        return
    try:
        identity = await bot.get_me()
        username = getattr(identity, "username", None)
        if not isinstance(username, str) or not username:
            await message.answer("❌ У бота не настроено имя для ссылок-приглашений.")
            return
        invite = await shared_reminder_service.create_invite(owner, reminder_id)
    except shared_reminder_service.SharedReminderError as exc:
        await message.answer(f"❌ {exc}")
        return
    except Exception:
        await message.answer("❌ Не удалось подготовить приглашение. Попробуй позже.")
        return
    link = f"https://t.me/{username}?start={shared_reminder_service.invite_start_payload(invite.token)}"
    await message.answer(
        "🔗 <b>Приглашение создано</b>\n\n"
        "Передай эту ссылку одному человеку. Она одноразовая и действует 24 часа:\n"
        f"<code>{escape(link)}</code>\n\n"
        "Отозвать ссылку или доступ можно командой /shared.",
        parse_mode="HTML",
    )


def _render_shared_header(entry: shared_reminder_service.SharedReminderView) -> str:
    role = "владелец" if entry.is_owner else "участник"
    return (
        f"🤝 <b>Общее напоминание · {role}</b>\n"
        f"Участников: <code>{entry.participant_count + 1}</code>"
    )


def _render_owner_controls(
    entry: shared_reminder_service.SharedReminderView,
    timezone_name: str,
) -> str:
    if not entry.is_owner:
        return ""

    members = ", ".join(f"#{member.membership_id}" for member in entry.members) or "нет"
    lines = [f"Участники: <code>{escape(members)}</code>"]
    if entry.pending_invites:
        lines.append("Активные ссылки:")
        lines.extend(
            f"<code>#{invite.invite_id}</code> до <code>{from_utc_to_user(invite.expires_at, timezone_name).strftime('%d.%m.%Y %H:%M')}</code>"
            for invite in entry.pending_invites
        )
    else:
        lines.append("Активных ссылок: нет")
    return "\n" + "\n".join(lines)


def _shared_entry_markup(
    entry: shared_reminder_service.SharedReminderView,
    occurrence: ReminderOccurrence | None,
) -> InlineKeyboardMarkup | None:
    base_markup = _entry_markup(entry, occurrence)
    rows = list(base_markup.inline_keyboard) if base_markup is not None else []
    if entry.is_owner:
        for member in entry.members:
            rows.extend(
                revoke_membership_kb(
                    member.membership_id,
                    member.revision,
                    button_text=f"Отозвать доступ #{member.membership_id}",
                ).inline_keyboard
            )
        for invite in entry.pending_invites:
            rows.extend(
                revoke_invite_kb(
                    invite.invite_id,
                    invite.revision,
                    button_text=f"Отозвать ссылку #{invite.invite_id}",
                ).inline_keyboard
            )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.message(Command("shared"))
async def cmd_shared(message: Message, command: CommandObject | None = None) -> None:
    if not await _require_private(message):
        return
    telegram_user_id, chat_id = _ids(message)
    user = await get_or_create_user(telegram_user_id, chat_id)
    page = await shared_reminder_service.list_shared_reminders_page(
        user,
        page=_parse_shared_page(command),
    )
    entries = page.items
    if not entries:
        if page.has_previous:
            await message.answer(
                f"🤝 На странице <code>{page.page}</code> общих напоминаний нет. "
                f"Открой <code>/shared {page.page - 1}</code>.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "🤝 Общих напоминаний пока нет.\n\n"
                "Владелец может создать ссылку командой <code>/share ID</code>.",
                parse_mode="HTML",
            )
        return

    timezone_name = user.timezone
    # One card contains all management controls for that reminder. Together
    # with one optional navigation message this bounds the Bot API fan-out to
    # SHARED_PAGE_SIZE + 1 calls per command invocation.
    displayed_count = 0
    for entry in entries[: shared_reminder_service.SHARED_PAGE_SIZE]:
        async with shared_reminder_service.authorize_shared_card_send(
            user,
            entry.reminder.id,
        ) as card:
            if card is None:
                continue
            entry = card.entry
            occurrence = card.occurrence
            display_state = OccurrenceState.DELIVERED.value if occurrence is not None else None
            header = _render_shared_header(entry)
            owner_controls = _render_owner_controls(entry, timezone_name)
            card_prefix = f"{header}\n\n"
            body_prefix = _shared_reminder_text(
                entry.reminder,
                timezone_name,
                display_state=display_state,
                display_at_utc=occurrence.delivery_at_utc if occurrence is not None else None,
                text_budget=0,
            )
            text_budget = max(
                0,
                MAX_REMINDER_TEXT_LENGTH
                - len(card_prefix)
                - len(body_prefix)
                - len(owner_controls),
            )
            card_text = (
                card_prefix
                + _shared_reminder_text(
                    entry.reminder,
                    timezone_name,
                    display_state=display_state,
                    display_at_utc=occurrence.delivery_at_utc if occurrence is not None else None,
                    text_budget=text_budget,
                )
                + owner_controls
            )
            await message.answer(
                card_text,
                reply_markup=_shared_entry_markup(entry, occurrence),
                parse_mode="HTML",
            )
            displayed_count += 1
    if page.has_previous or page.has_next:
        navigation: list[str] = []
        if page.has_previous:
            navigation.append(f"предыдущая: /shared {page.page - 1}")
        if page.has_next:
            navigation.append(f"следующая: /shared {page.page + 1}")
        if displayed_count:
            start = (page.page - 1) * page.page_size + 1
            end = start + displayed_count - 1
            page_summary = f"Показаны {start}–{end}"
        else:
            page_summary = "На этой странице нет доступных карточек"
        await message.answer(
            f"{page_summary} · {'; '.join(navigation)}",
        )


async def _remove_keyboard(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            return


async def _revoke_callback(callback: CallbackQuery) -> None:
    parsed = parse_callback(callback.data)
    if (
        parsed is None
        or parsed.action != CallbackAction.REVOKE
        or parsed.origin != CallbackOrigin.SHARED
        or not isinstance(callback.message, Message)
        or callback.from_user is None
        or getattr(callback.message.chat, "type", None) != "private"
    ):
        await callback.answer("Устарело", show_alert=False)
        return
    owner = await get_or_create_user(
        telegram_user_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
    )
    if parsed.target == CallbackTarget.MEMBERSHIP:
        revoked = await shared_reminder_service.revoke_membership(
            owner,
            parsed.target_id,
            expected_revision=parsed.revision,
        )
        text = "Доступ отозван" if revoked else "Ссылка или доступ уже недействительны"
    elif parsed.target == CallbackTarget.INVITE:
        revoked = await shared_reminder_service.revoke_invite(
            owner,
            parsed.target_id,
            expected_revision=parsed.revision,
        )
        text = "Ссылка отозвана" if revoked else "Ссылка уже недействительна"
    else:
        await callback.answer("Устарело", show_alert=False)
        return
    await callback.answer(text, show_alert=False)
    if revoked:
        await _remove_keyboard(callback)


@router.callback_query(F.data.startswith("r1:revoke:m:"))
async def revoke_membership_callback(callback: CallbackQuery) -> None:
    await _revoke_callback(callback)


@router.callback_query(F.data.startswith("r1:revoke:i:"))
async def revoke_invite_callback(callback: CallbackQuery) -> None:
    await _revoke_callback(callback)
