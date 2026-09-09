import asyncio
import logging
import os

import pytest
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot_commands import (
    ADMIN_COMMANDS,
    METADATA_OPERATION_TIMEOUT_SECONDS,
    PUBLIC_COMMANDS,
    sync_bot_metadata,
)
from app.telegram_metadata import (
    ADMIN_COMMAND_DEFINITIONS,
    BOT_METADATA,
    COMMAND_DEFINITIONS,
    HELP_TEXT,
    METADATA_LANGUAGE_CODES,
    PUBLIC_COMMAND_DEFINITIONS,
)


def test_deadline_command_is_in_both_canonical_command_surfaces() -> None:
    assert any(command.command == "deadline" for command in PUBLIC_COMMANDS)
    assert any(command.command == "deadline" for command in ADMIN_COMMANDS)


def test_telegram_metadata_stays_within_bot_api_limits() -> None:
    assert 0 < len(BOT_METADATA.name) <= 64
    assert 0 < len(BOT_METADATA.short_description) <= 120
    assert 0 < len(BOT_METADATA.description) <= 512

    for command in ADMIN_COMMANDS:
        assert 1 <= len(command.command) <= 32
        assert command.command == command.command.lower()
        assert 1 <= len(command.description) <= 256

    assert len(ADMIN_COMMANDS) <= 100


def test_public_and_admin_commands_are_derived_from_one_catalogue() -> None:
    public_names = {definition.command for definition in PUBLIC_COMMAND_DEFINITIONS}
    admin_only_names = {
        definition.command for definition in COMMAND_DEFINITIONS if definition.admin_only
    }

    assert public_names == {command.command for command in PUBLIC_COMMANDS}
    assert {definition.command for definition in ADMIN_COMMAND_DEFINITIONS} == {
        command.command for command in ADMIN_COMMANDS
    }
    assert public_names.isdisjoint(admin_only_names)
    assert admin_only_names == {"stats", "failed"}
    assert admin_only_names.isdisjoint(public_names)


class _MetadataBot:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def set_my_name(self, **kwargs) -> bool:
        self.calls.append(("set_my_name", kwargs))
        if self.failure is not None:
            raise self.failure
        return True

    async def set_my_short_description(self, **kwargs) -> bool:
        self.calls.append(("set_my_short_description", kwargs))
        return True

    async def set_my_description(self, **kwargs) -> bool:
        self.calls.append(("set_my_description", kwargs))
        return True

    async def set_my_commands(self, **kwargs) -> bool:
        self.calls.append(("set_my_commands", kwargs))
        return True


@pytest.mark.asyncio
async def test_metadata_sync_calls_profile_methods_and_scoped_commands() -> None:
    bot = _MetadataBot()

    await sync_bot_metadata(bot, admin_ids={1002, 1001})

    profile_calls = [call for call in bot.calls if call[0] != "set_my_commands"]
    assert [name for name, _ in profile_calls] == [
        "set_my_name",
        "set_my_name",
        "set_my_short_description",
        "set_my_short_description",
        "set_my_description",
        "set_my_description",
    ]
    assert [kwargs["language_code"] for _, kwargs in profile_calls] == list(
        METADATA_LANGUAGE_CODES
    ) * 3
    assert profile_calls[0][1]["name"] == BOT_METADATA.name
    assert profile_calls[2][1]["short_description"] == BOT_METADATA.short_description
    assert profile_calls[4][1]["description"] == BOT_METADATA.description
    assert all(
        kwargs["request_timeout"] == METADATA_OPERATION_TIMEOUT_SECONDS
        for _, kwargs in profile_calls
    )

    command_calls = [kwargs for name, kwargs in bot.calls if name == "set_my_commands"]
    assert len(command_calls) == 2 + (2 * 2)
    public_calls = [
        kwargs for kwargs in command_calls if isinstance(kwargs["scope"], BotCommandScopeDefault)
    ]
    admin_calls = [
        kwargs for kwargs in command_calls if isinstance(kwargs["scope"], BotCommandScopeChat)
    ]
    assert len(public_calls) == 2
    assert {kwargs["language_code"] for kwargs in public_calls} == set(METADATA_LANGUAGE_CODES)
    assert all(
        {command.command for command in kwargs["commands"]}
        == {definition.command for definition in PUBLIC_COMMAND_DEFINITIONS}
        for kwargs in public_calls
    )
    assert len(admin_calls) == 4
    assert {kwargs["scope"].chat_id for kwargs in admin_calls} == {1001, 1002}
    assert {kwargs["language_code"] for kwargs in admin_calls} == set(METADATA_LANGUAGE_CODES)
    assert all(
        {command.command for command in kwargs["commands"]}
        == {definition.command for definition in ADMIN_COMMAND_DEFINITIONS}
        for kwargs in admin_calls
    )


@pytest.mark.asyncio
async def test_metadata_sync_failure_is_non_fatal_and_does_not_log_payload(caplog) -> None:
    bot = _MetadataBot(failure=RuntimeError("BOT_TOKEN=should-not-appear"))
    caplog.set_level(logging.WARNING, logger="app.bot_commands")

    await sync_bot_metadata(bot, admin_ids=set())

    assert [name for name, _ in bot.calls] == [
        "set_my_name",
        "set_my_name",
        "set_my_short_description",
        "set_my_short_description",
        "set_my_description",
        "set_my_description",
        "set_my_commands",
        "set_my_commands",
    ]
    assert "should-not-appear" not in caplog.text
    assert any(
        record.__dict__.get("extra_data", "").endswith("error=RuntimeError")
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_metadata_sync_timeout_is_bounded_and_non_fatal(monkeypatch) -> None:
    class _SlowMetadataBot(_MetadataBot):
        async def set_my_name(self, **kwargs) -> bool:
            self.calls.append(("set_my_name", kwargs))
            await asyncio.sleep(0.05)
            return True

    monkeypatch.setattr("app.bot_commands.METADATA_OPERATION_TIMEOUT_SECONDS", 0.001)
    bot = _SlowMetadataBot()

    await asyncio.wait_for(sync_bot_metadata(bot, admin_ids=set()), timeout=1)


def test_help_mentions_every_public_command_and_hides_admin_only_commands() -> None:
    for definition in PUBLIC_COMMAND_DEFINITIONS:
        assert f"/{definition.command}" in HELP_TEXT
    assert "/stats" not in HELP_TEXT
    assert "/failed" not in HELP_TEXT
