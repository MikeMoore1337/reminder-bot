from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    command: str
    description: str
    admin_only: bool = False


@dataclass(frozen=True, slots=True)
class TelegramBotMetadata:
    name: str
    short_description: str
    description: str


# This is the only command catalogue.  Bot API registration, help text and
# tests derive their public/admin views from it.
COMMAND_DEFINITIONS: Final[tuple[CommandDefinition, ...]] = (
    CommandDefinition("start", "Открыть первый экран"),
    CommandDefinition("help", "Как пользоваться ботом"),
    CommandDefinition("timezone", "Установить часовой пояс"),
    CommandDefinition("mytimezone", "Показать часовой пояс"),
    CommandDefinition("suggestions", "Включить или выключить подсказки"),
    CommandDefinition("digest", "Включить или выключить дайджесты"),
    CommandDefinition("remind", "Создать напоминание"),
    CommandDefinition("deadline", "Напоминание с защитой дедлайна"),
    CommandDefinition("list", "Мои активные напоминания"),
    CommandDefinition("share", "Пригласить участника"),
    CommandDefinition("shared", "Общие напоминания"),
    CommandDefinition("cancel", "Удалить или отменить действие"),
    CommandDefinition("stats", "Статистика бота", admin_only=True),
    CommandDefinition("failed", "Ошибки отправки", admin_only=True),
)

PUBLIC_COMMAND_DEFINITIONS: Final[tuple[CommandDefinition, ...]] = tuple(
    definition for definition in COMMAND_DEFINITIONS if not definition.admin_only
)
ADMIN_COMMAND_DEFINITIONS: Final[tuple[CommandDefinition, ...]] = COMMAND_DEFINITIONS

BOT_METADATA: Final = TelegramBotMetadata(
    name="Reminder Bot",
    short_description=(
        "Напоминания из обычного текста и голоса: разовые, повторяющиеся, важные и с защитой дедлайна."
    ),
    description=(
        "Напиши или скажи голосом, что и когда напомнить — бот разберёт обычную фразу "
        "и покажет результат перед созданием.\n\n"
        "Примеры:\n"
        "• напомни завтра в 9 созвон\n"
        "• напомни каждый понедельник в 10 отправить отчёт\n"
        "• напомни важное через 30 минут выключить духовку\n\n"
        "Есть повторения, дедлайны, переносы, общие напоминания и часовые пояса."
    ),
)

BOT_NAME: Final[str] = BOT_METADATA.name
BOT_SHORT_DESCRIPTION: Final[str] = BOT_METADATA.short_description
BOT_DESCRIPTION: Final[str] = BOT_METADATA.description

# Empty language_code is Telegram's fallback for users without a dedicated
# translation.  The Russian variant makes the intended locale explicit while
# retaining the same canonical copy for every other language.
METADATA_LANGUAGE_CODES: Final[tuple[str, ...]] = ("", "ru")

TELEGRAM_ASSETS_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "assets" / "telegram"
WELCOME_IMAGE_PATH: Final[Path] = TELEGRAM_ASSETS_DIR / "welcome.png"
AVATAR_IMAGE_PATH: Final[Path] = TELEGRAM_ASSETS_DIR / "avatar.png"


def _public_command_lines() -> str:
    return "\n".join(
        f"/{definition.command} — {definition.description}"
        for definition in PUBLIC_COMMAND_DEFINITIONS
    )


START_TEXT: Final[str] = (
    f"👋 <b>{BOT_NAME}</b>\n"
    "<b>Умная напоминалка без сложных форм.</b>\n\n"
    "Напиши или скажи голосом, что и когда напомнить — "
    "я разберу фразу и покажу понятное подтверждение.\n\n"
    "⚡ <b>Обычный язык</b> — без форм и настроек\n"
    "🔁 <b>Гибкие повторы</b> — дни, недели, месяцы, после выполнения\n"
    "🔔 <b>Важное и дедлайны</b> — не потерять действительно важное\n\n"
    "Попробуй:\n"
    "• напомни завтра в 9 созвон\n"
    "• напомни каждый понедельник в 10 отправить отчёт\n"
    "• напомни важное через 30 минут выключить духовку\n\n"
    "Выбери действие ниже или просто напиши напоминание."
)

HELP_TEXT: Final[str] = (
    f"❓ <b>Как пользоваться {BOT_NAME}</b>\n\n"
    "🚀 <b>Быстрый старт</b>\n"
    "Напиши или скажи голосом, что и когда напомнить. Бот разберёт обычную фразу, "
    "покажет время и попросит подтвердить создание.\n\n"
    "Примеры:\n"
    "• напомни завтра в 9 созвон\n"
    "• напомни через 15 минут выключить духовку\n"
    "• /remind 31.03.2026 18:30 купить молоко\n\n"
    "🔁 <b>Повторы</b>\n"
    "• каждый день, по будням или в выбранные дни недели\n"
    "• каждые X минут/часов/дней, недели и месяцы\n"
    "• каждый второй вторник или последняя пятница месяца\n"
    "• каждый год и до указанной даты\n"
    "• через N дней после фактического выполнения\n\n"
    "🔔 <b>Важные reminders и дедлайны</b>\n"
    "Добавь «важное» или [важное], чтобы бот повторял доставку в заданных лимитах "
    "и с учётом тихих часов. Для плана защиты используй /deadline: бот покажет точки "
    "«за неделю», «за день», «за час», «в срок» и создаст план только после подтверждения.\n\n"
    "🎙 <b>Голос</b>\n"
    "Отправь voice-сообщение с обычной фразой. После расшифровки проверь результат "
    "и нажми «Создать»; исходное аудио не хранится.\n\n"
    "📎 <b>Контекст Telegram</b>\n"
    "Ответь на сообщение: «напомни об этом завтра в 9». Можно переслать ссылку, фото, "
    "документ или сообщение без времени — бот сохранит короткий контекст и спросит, когда напомнить.\n\n"
    "👥 <b>Общие reminders</b>\n"
    "Владелец создаёт ссылку через /share ID и управляет доступом через /shared. "
    "Участник видит только общее напоминание и может выполнить или отложить текущую доставку; "
    "личный контекст владельца остаётся приватным.\n\n"
    "🌍 <b>Настройки и команды</b>\n"
    "Часовой пояс хранится отдельно для каждого пользователя. Подсказки и дайджесты выключены "
    "по умолчанию и включаются только явно.\n\n"
    f"{_public_command_lines()}\n\n"
    "Если время неоднозначно, бот сначала попросит уточнение. /cancel отменяет уточнение, "
    "черновик или удаляет указанное напоминание."
)
