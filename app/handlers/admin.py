from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import get_settings
from app.services.message_context import get_context_metrics
from app.services.reminder_service import get_failed_reminders, get_stats
from app.services.voice_service import get_voice_metrics

router = Router()
settings = get_settings()


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    stats = await get_stats()
    voice = get_voice_metrics()
    context = get_context_metrics()
    voice_stt_failures = sum(
        count for key, count in voice.items() if key.startswith("failure_stt_")
    )

    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👤 Пользователей: <b>{stats['total_users']}</b>\n"
        f"📝 Всего напоминаний: <b>{stats['total_reminders']}</b>\n"
        f"⏳ Ожидают: <b>{stats['pending_reminders']}</b>\n"
        f"❌ Ошибок: <b>{stats['failed_reminders']}</b>\n"
        f"🔁 Повторяющихся: <b>{stats['recurring_reminders']}</b>\n"
        f"✅ Отправлено за 24 часа: <b>{stats['sent_last_24h']}</b>\n"
        f"🎙 Голосовой STT: <b>{voice['stt_success']}</b> успешно / "
        f"<b>{voice_stt_failures}</b> ошибок\n"
        f"🛠 Исправлено голосом: <b>{voice['parse_correction']}</b>\n"
        f"✅ Подтверждено голосом: <b>{voice['confirmation_success']}</b>\n"
        f"📎 Context fallback: <b>{sum(value for key, value in context.items() if key.startswith('context_delivery_fallback_'))}</b>\n"
        f"🧹 Context cleanup: <b>{sum(value for key, value in context.items() if key.startswith('context_cleanup_') and key != 'context_cleanup_failures')}</b>"
    )

    await message.answer(text, parse_mode="HTML")


@router.message(Command("failed"))
async def cmd_failed(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    reminders = await get_failed_reminders()

    if not reminders:
        await message.answer("✅ Ошибок отправки сейчас нет.")
        return

    parts: list[str] = ["❌ <b>Последние failed-напоминания</b>\n"]

    for reminder in reminders:
        reminder_text = escape(reminder.text[:300])
        error_text = escape((reminder.error_text or "-")[:500])
        parts.append(
            f"ID: <b>{reminder.id}</b>\n"
            f"user_id: <code>{reminder.user_id}</code>\n"
            f"chat_id: <code>{reminder.chat_id}</code>\n"
            f"when_utc: <code>{reminder.remind_at_utc.strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
            f"text: {reminder_text}\n"
            f"error: <code>{error_text}</code>\n"
        )

    await message.answer("\n".join(parts), parse_mode="HTML")
