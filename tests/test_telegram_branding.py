from __future__ import annotations

import logging
import os
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import FSInputFile

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.handlers import ui
from app.telegram_metadata import (
    AVATAR_IMAGE_PATH,
    START_TEXT,
    WELCOME_IMAGE_PATH,
)
from scripts.export_telegram_assets import export_assets


class _StartMessage:
    def __init__(self, *, photo_fails: bool = False, chat_type: str = "private") -> None:
        self.chat = SimpleNamespace(id=2001, type=chat_type)
        self.from_user = SimpleNamespace(id=1001)
        self.photo_fails = photo_fails
        self.photo_calls: list[dict[str, object]] = []
        self.answer_calls: list[dict[str, object]] = []

    async def answer_photo(self, **kwargs) -> None:
        self.photo_calls.append(kwargs)
        if self.photo_fails:
            raise RuntimeError("BOT_TOKEN=should-not-appear")

    async def answer(self, text: str, **kwargs) -> None:
        self.answer_calls.append({"text": text, **kwargs})


def _stub_start_dependencies(monkeypatch) -> AsyncMock:
    user = SimpleNamespace(id=1)
    get_user_timezone = AsyncMock(return_value="Europe/Moscow")
    monkeypatch.setattr(ui, "get_or_create_user", AsyncMock(return_value=user))
    monkeypatch.setattr(ui, "get_user_timezone", get_user_timezone)
    return get_user_timezone


@pytest.mark.asyncio
async def test_start_sends_branded_image_concise_caption_and_keyboard(monkeypatch) -> None:
    _stub_start_dependencies(monkeypatch)
    message = _StartMessage()

    await ui.cmd_start(message, SimpleNamespace(args=None))

    assert len(message.photo_calls) == 1
    assert message.answer_calls == []
    photo_call = message.photo_calls[0]
    assert isinstance(photo_call["photo"], FSInputFile)
    assert photo_call["photo"].path == WELCOME_IMAGE_PATH
    assert photo_call["caption"].startswith("👋 <b>Reminder Bot</b>")
    assert "Умная напоминалка без сложных форм" in photo_call["caption"]
    assert photo_call["reply_markup"].keyboard
    assert photo_call["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_start_falls_back_to_usable_text_when_image_send_fails(caplog, monkeypatch) -> None:
    _stub_start_dependencies(monkeypatch)
    caplog.set_level(logging.WARNING, logger="app.handlers.ui")
    message = _StartMessage(photo_fails=True)

    await ui.cmd_start(message, SimpleNamespace(args=None))

    assert len(message.photo_calls) == 1
    assert len(message.answer_calls) == 1
    fallback = message.answer_calls[0]
    assert fallback["text"].startswith("👋 <b>Reminder Bot</b>")
    assert fallback["text"] == message.photo_calls[0]["caption"]
    assert fallback["reply_markup"].keyboard
    assert fallback["parse_mode"] == "HTML"
    assert "should-not-appear" not in caplog.text


@pytest.mark.asyncio
async def test_unknown_start_payload_keeps_generic_screen_and_does_not_accept_invite(
    monkeypatch,
) -> None:
    _stub_start_dependencies(monkeypatch)
    accept_invite = AsyncMock()
    monkeypatch.setattr(ui.shared_reminder_service, "accept_invite", accept_invite)
    message = _StartMessage()

    await ui.cmd_start(message, SimpleNamespace(args="unknown_payload"))

    assert accept_invite.await_count == 0
    assert message.photo_calls[0]["caption"].startswith(START_TEXT)


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_telegram_assets_are_tracked_and_reproducible(tmp_path) -> None:
    assert WELCOME_IMAGE_PATH.exists()
    assert AVATAR_IMAGE_PATH.exists()
    assert _png_dimensions(WELCOME_IMAGE_PATH) == (1600, 900)
    assert _png_dimensions(AVATAR_IMAGE_PATH) == (1024, 1024)

    export_assets(tmp_path)
    for name in ("welcome.svg", "welcome.png", "avatar.svg", "avatar.png"):
        assert (tmp_path / name).read_bytes() == (WELCOME_IMAGE_PATH.parent / name).read_bytes()
