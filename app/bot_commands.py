from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from app.config import get_settings
from app.telegram_metadata import (
    ADMIN_COMMAND_DEFINITIONS,
    BOT_METADATA,
    METADATA_LANGUAGE_CODES,
    PUBLIC_COMMAND_DEFINITIONS,
    CommandDefinition,
)

settings = get_settings()
logger = logging.getLogger(__name__)

METADATA_OPERATION_TIMEOUT_SECONDS = 10
METADATA_SYNC_TIMEOUT_SECONDS = 30


def _to_bot_commands(definitions: Iterable[CommandDefinition]) -> list[BotCommand]:
    return [
        BotCommand(command=definition.command, description=definition.description)
        for definition in definitions
    ]


PUBLIC_COMMANDS = _to_bot_commands(PUBLIC_COMMAND_DEFINITIONS)
ADMIN_COMMANDS = _to_bot_commands(ADMIN_COMMAND_DEFINITIONS)

MetadataOperation = Callable[[], Awaitable[object]]


async def _run_metadata_operation(name: str, operation: MetadataOperation) -> None:
    try:
        async with asyncio.timeout(METADATA_OPERATION_TIMEOUT_SECONDS):
            await operation()
    except Exception as exc:
        # Keep Telegram errors and configuration values out of logs.  The next
        # restart retries the idempotent sync without making metadata a
        # reason for a polling/webhook restart loop.
        logger.warning(
            "Telegram bot metadata sync operation failed",
            extra={"extra_data": f"operation={name} error={type(exc).__name__}"},
        )


async def _sync_profile_field(
    name: str,
    value: str,
    method: Callable[..., Awaitable[object]],
) -> None:
    for language_code in METADATA_LANGUAGE_CODES:

        async def operation(
            language_code: str = language_code,
            method: Callable[..., Awaitable[object]] = method,
            name: str = name,
            value: str = value,
        ) -> object:
            return await method(
                **{name: value},
                language_code=language_code,
                request_timeout=METADATA_OPERATION_TIMEOUT_SECONDS,
            )

        await _run_metadata_operation(
            f"{name}.{language_code or 'fallback'}",
            operation,
        )


async def _sync_commands(
    bot: Bot,
    *,
    scope: BotCommandScopeDefault | BotCommandScopeChat,
    scope_name: str,
) -> None:
    definitions = (
        ADMIN_COMMAND_DEFINITIONS
        if isinstance(scope, BotCommandScopeChat)
        else PUBLIC_COMMAND_DEFINITIONS
    )
    commands = _to_bot_commands(definitions)
    for language_code in METADATA_LANGUAGE_CODES:

        async def operation(
            language_code: str = language_code,
            scope: BotCommandScopeDefault | BotCommandScopeChat = scope,
            commands: list[BotCommand] = commands,
        ) -> object:
            return await bot.set_my_commands(
                commands=commands,
                scope=scope,
                language_code=language_code,
                request_timeout=METADATA_OPERATION_TIMEOUT_SECONDS,
            )

        await _run_metadata_operation(
            f"setMyCommands.{scope_name}.{language_code or 'fallback'}",
            operation,
        )


async def sync_bot_metadata(bot: Bot, *, admin_ids: Iterable[int] | None = None) -> None:
    """Synchronize profile copy and scoped commands without blocking startup."""

    selected_admin_ids = settings.admin_ids if admin_ids is None else set(admin_ids)
    try:
        async with asyncio.timeout(METADATA_SYNC_TIMEOUT_SECONDS):
            await _sync_profile_field("name", BOT_METADATA.name, bot.set_my_name)
            await _sync_profile_field(
                "short_description",
                BOT_METADATA.short_description,
                bot.set_my_short_description,
            )
            await _sync_profile_field(
                "description", BOT_METADATA.description, bot.set_my_description
            )
            await _sync_commands(
                bot,
                scope=BotCommandScopeDefault(),
                scope_name="public",
            )
            for admin_id in sorted(selected_admin_ids):
                await _sync_commands(
                    bot,
                    scope=BotCommandScopeChat(chat_id=admin_id),
                    scope_name="admin",
                )
    except Exception as exc:
        logger.warning(
            "Telegram bot metadata sync timed out or failed",
            extra={"extra_data": f"operation=sync error={type(exc).__name__}"},
        )


async def setup_bot_commands(bot: Bot) -> None:
    # Keep the old entry point for the startup lifecycle and tests; metadata
    # now includes profile fields as well as command scopes.
    await sync_bot_metadata(bot)
