from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from app.config import get_settings

settings = get_settings()

PUBLIC_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="help", description="Как пользоваться ботом"),
    BotCommand(command="timezone", description="Установить часовой пояс"),
    BotCommand(command="mytimezone", description="Показать мой часовой пояс"),
    BotCommand(command="suggestions", description="Включить или выключить подсказки"),
    BotCommand(command="digest", description="Включить или выключить дайджесты"),
    BotCommand(command="remind", description="Создать напоминание"),
    BotCommand(command="deadline", description="Создать напоминание с защитой дедлайна"),
    BotCommand(command="list", description="Мои активные напоминания"),
    BotCommand(command="cancel", description="Удалить напоминание или отменить действие"),
]

ADMIN_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="help", description="Как пользоваться ботом"),
    BotCommand(command="timezone", description="Установить часовой пояс"),
    BotCommand(command="mytimezone", description="Показать мой часовой пояс"),
    BotCommand(command="suggestions", description="Включить или выключить подсказки"),
    BotCommand(command="digest", description="Включить или выключить дайджесты"),
    BotCommand(command="remind", description="Создать напоминание"),
    BotCommand(command="deadline", description="Создать напоминание с защитой дедлайна"),
    BotCommand(command="list", description="Мои активные напоминания"),
    BotCommand(command="cancel", description="Удалить напоминание или отменить действие"),
    BotCommand(command="stats", description="Статистика бота"),
    BotCommand(command="failed", description="Ошибки отправки"),
]


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        commands=PUBLIC_COMMANDS,
        scope=BotCommandScopeDefault(),
    )

    for admin_id in settings.admin_ids:
        await bot.set_my_commands(
            commands=ADMIN_COMMANDS,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )
