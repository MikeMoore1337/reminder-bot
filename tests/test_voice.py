import asyncio
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Message, Voice
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.callbacks import CallbackAction, CallbackOrigin, CallbackTarget, parse_callback
from app.db.base import Base
from app.db.models import ActionDraft, Reminder, ReminderClarification, User, VoiceReminderDraft
from app.handlers import reminders as reminders_handler
from app.keyboards.voice import voice_draft_kb
from app.services import clarification_service, reminder_service, voice_service
from app.services.reminder_parser import ClarificationRequest, ParsedReminder, parse_reminder_input
from app.services.speech_to_text import (
    SpeechToTextError,
    WhisperCppSpeechToTextProvider,
)
from app.services.voice_media import (
    VOICE_PCM_BYTES_PER_SECOND,
    VoiceMediaError,
    VoiceMediaLimits,
    cleanup_orphaned_voice_temp_dirs,
    convert_voice_to_wav,
    download_voice,
    validate_voice_metadata,
)


def _voice(
    *, duration: int = 30, mime_type: str | None = "audio/ogg", file_size: int = 100
) -> Voice:
    return Voice(
        file_id="voice-file-id",
        file_unique_id="voice-file-unique-id",
        duration=duration,
        mime_type=mime_type,
        file_size=file_size,
    )


def _settings(temp_dir: Path, **overrides):
    values = {
        "voice_temp_dir": str(temp_dir),
        "voice_max_file_size_bytes": 10_000_000,
        "voice_max_duration_seconds": 120,
        "voice_download_timeout_seconds": 30,
        "voice_conversion_timeout_seconds": 30,
        "voice_conversion_command": "ffmpeg",
        "voice_stt_command": "whisper-cli",
        "voice_stt_model_path": None,
        "voice_stt_language": "ru",
        "voice_stt_threads": 2,
        "voice_stt_timeout_seconds": 90,
        "voice_stt_max_concurrent_jobs": 1,
        "voice_stt_queue_timeout_seconds": 5,
        "voice_draft_ttl_seconds": 900,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _open_sqlite(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    connection = await engine.connect()
    await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(voice_service, "SessionLocal", session_factory)
    monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
    monkeypatch.setattr(clarification_service, "SessionLocal", session_factory)
    return engine, connection, session_factory


async def _add_user(
    session_factory,
    *,
    telegram_user_id: int = 1001,
    chat_id: int = 2002,
    timezone: str = "Europe/Moscow",
) -> User:
    async with session_factory() as session:
        user = User(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=timezone,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


class _FakeBot:
    async def get_file(self, file_id: str, *, request_timeout: int):
        assert file_id == "voice-file-id"
        assert request_timeout == 30
        return SimpleNamespace(file_path="voice.ogg", file_size=100)

    async def download_file(self, file_path, *, destination, timeout, seek):
        assert file_path == "voice.ogg"
        assert timeout == 30
        assert seek is False
        destination.write(b"ogg-bytes")
        destination.flush()


class _FakeProvider:
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript
        self.paths: list[Path] = []

    def is_available(self) -> bool:
        return True

    async def transcribe(self, audio_path: Path) -> str:
        self.paths.append(audio_path)
        assert audio_path.name == "normalized.wav"
        return self.transcript


def test_voice_transcript_parses_spoken_relative_number_words() -> None:
    transcript = "Напомни через две минуты проверить голосовое напоминание"
    now_local = datetime(2026, 9, 8, 13, 0, 45, 123456)

    candidate, parsed = voice_service.parse_voice_transcript(
        transcript,
        now_local=now_local,
    )

    assert candidate == transcript
    assert isinstance(parsed, ParsedReminder)
    assert parsed.local_dt == now_local + timedelta(minutes=2)
    assert parsed.text == "проверить голосовое напоминание"
    assert parsed.recurrence_type == "none"
    assert parsed.mode == "normal"


def test_voice_metadata_allowlist_and_limits() -> None:
    limits = VoiceMediaLimits(
        max_file_size_bytes=100,
        max_duration_seconds=60,
        download_timeout_seconds=30,
        conversion_timeout_seconds=30,
        conversion_command="ffmpeg",
    )

    validate_voice_metadata(_voice(duration=60, file_size=100), limits)
    with pytest.raises(VoiceMediaError, match="OGG/Opus"):
        validate_voice_metadata(_voice(mime_type="audio/mpeg"), limits)
    with pytest.raises(VoiceMediaError, match="длинное"):
        validate_voice_metadata(_voice(duration=61), limits)
    with pytest.raises(VoiceMediaError, match="большое"):
        validate_voice_metadata(_voice(file_size=101), limits)


def test_download_is_bounded_by_actual_stream_size(tmp_path: Path) -> None:
    async def scenario() -> None:
        limits = VoiceMediaLimits(
            max_file_size_bytes=4,
            max_duration_seconds=120,
            download_timeout_seconds=30,
            conversion_timeout_seconds=30,
            conversion_command="ffmpeg",
        )

        class OversizedBot(_FakeBot):
            async def get_file(self, file_id: str, *, request_timeout: int):
                assert request_timeout == 30
                return SimpleNamespace(file_path="voice.ogg", file_size=4)

            async def download_file(self, file_path, *, destination, timeout, seek):
                destination.write(b"12345")

        with pytest.raises(VoiceMediaError) as caught:
            await download_voice(
                OversizedBot(), _voice(file_size=4), tmp_path / "input.ogg", limits
            )
        assert caught.value.category == "size"

    asyncio.run(scenario())


def test_voice_process_creates_persistent_preview_draft_and_cleans_media(monkeypatch, tmp_path):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            monkeypatch.setattr(voice_service, "settings", _settings(tmp_path))
            voice_service.voice_metrics = voice_service.VoiceMetrics()

            async def fake_convert(
                source: Path, destination: Path, limits: VoiceMediaLimits
            ) -> int:
                assert source.name == "input.ogg"
                destination.write_bytes(b"RIFF" + b"0" * 100)
                return destination.stat().st_size

            monkeypatch.setattr(voice_service, "convert_voice_to_wav", fake_convert)
            user = await _add_user(session_factory)
            provider = _FakeProvider("завтра в 9 позвонить врачу")

            result = await voice_service.process_voice_message(
                _FakeBot(),
                user,
                _voice(),
                source_message_id=7001,
                provider=provider,
                now_utc=now,
            )

            assert result.draft is not None
            assert result.draft.transcript == "завтра в 9 позвонить врачу"
            assert result.draft.reminder_text == "позвонить врачу"
            assert result.draft.source_message_id == 7001
            assert provider.paths and not provider.paths[0].exists()
            assert list(tmp_path.iterdir()) == []
            assert voice_service.get_voice_metrics()["parse_success"] == 1
            async with session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(Reminder)) == 0
                saved = await session.get(VoiceReminderDraft, result.draft.id)
                assert saved is not None
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_spoken_relative_voice_handler_keeps_confirmation_gate(monkeypatch, tmp_path):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            transcript = "Напомни через две минуты проверить голосовое напоминание"
            monkeypatch.setattr(voice_service, "settings", _settings(tmp_path))
            monkeypatch.setattr(voice_service, "utc_now", lambda: now)
            voice_service.voice_metrics = voice_service.VoiceMetrics()
            voice_service._stt_semaphore = None
            voice_service._stt_semaphore_limit = None

            async def fake_convert(
                source: Path, destination: Path, limits: VoiceMediaLimits
            ) -> int:
                assert source.name == "input.ogg"
                destination.write_bytes(b"RIFF" + b"0" * 100)
                return destination.stat().st_size

            monkeypatch.setattr(voice_service, "convert_voice_to_wav", fake_convert)
            provider = _FakeProvider(transcript)
            monkeypatch.setattr(voice_service, "_default_provider", lambda: provider)

            user = await _add_user(session_factory)
            monkeypatch.setattr(
                reminders_handler,
                "get_or_create_user",
                AsyncMock(return_value=user),
            )
            preview_message = Message.model_construct(
                message_id=9100,
                chat=SimpleNamespace(id=user.chat_id, type="private"),
            )
            message_answers = AsyncMock(return_value=preview_message)
            message_edits = AsyncMock()
            monkeypatch.setattr(Message, "answer", message_answers)
            monkeypatch.setattr(Message, "edit_reply_markup", message_edits)

            voice_message = Message.model_construct(
                message_id=7001,
                from_user=SimpleNamespace(id=user.telegram_user_id),
                chat=SimpleNamespace(id=user.chat_id, type="private"),
                voice=_voice(),
            )
            await reminders_handler.voice_reminder_handler(voice_message, _FakeBot())

            assert provider.paths
            metrics = voice_service.get_voice_metrics()
            assert metrics["stt_success"] == 1
            assert metrics["parse_success"] == 1
            message_answers.assert_awaited_once()
            preview_text = message_answers.await_args.args[0]
            assert "проверить голосовое напоминание" in preview_text
            markup = message_answers.await_args.kwargs["reply_markup"]
            create_data = markup.inline_keyboard[0][0].callback_data
            parsed_callback = parse_callback(create_data)
            assert parsed_callback is not None
            assert parsed_callback.action == CallbackAction.CREATE

            async with session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(Reminder)) == 0
                draft = await session.scalar(select(VoiceReminderDraft))
                assert draft is not None
                assert draft.reminder_text == "проверить голосовое напоминание"
                assert draft.datetime_semantics == "instant"

            callback_answers = AsyncMock()
            callback = SimpleNamespace(
                data=create_data,
                message=preview_message,
                from_user=SimpleNamespace(id=user.telegram_user_id),
                answer=callback_answers,
            )
            await reminders_handler.reminder_callback(callback)

            callback_answers.assert_awaited_once_with("Напоминание сохранено", show_alert=False)
            async with session_factory() as session:
                reminders = list((await session.scalars(select(Reminder))).all())
                assert len(reminders) == 1
                assert reminders[0].text == "проверить голосовое напоминание"
                assert (
                    await session.scalar(select(func.count()).select_from(VoiceReminderDraft)) == 0
                )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_voice_process_does_not_download_when_provider_is_unconfigured(monkeypatch, tmp_path):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            monkeypatch.setattr(voice_service, "settings", _settings(tmp_path))

            class FailingDownloadBot:
                async def get_file(self, file_id: str, *, request_timeout: int):
                    raise AssertionError("audio must not be downloaded without a model")

            user = await _add_user(session_factory)
            with pytest.raises(voice_service.VoiceProcessingError, match="недоступна") as caught:
                await voice_service.process_voice_message(
                    FailingDownloadBot(),
                    user,
                    _voice(),
                    provider=None,
                )
            assert caught.value.category == "unavailable"
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_voice_process_failure_cleans_temp_directory(monkeypatch, tmp_path):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            monkeypatch.setattr(voice_service, "settings", _settings(tmp_path))
            voice_service.voice_metrics = voice_service.VoiceMetrics()

            async def fake_convert(
                source: Path, destination: Path, limits: VoiceMediaLimits
            ) -> int:
                destination.write_bytes(b"RIFF" + b"0" * 100)
                return destination.stat().st_size

            monkeypatch.setattr(voice_service, "convert_voice_to_wav", fake_convert)

            class FailingProvider:
                def is_available(self) -> bool:
                    return True

                async def transcribe(self, audio_path: Path) -> str:
                    raise SpeechToTextError("timeout", "safe timeout")

            user = await _add_user(session_factory)
            with pytest.raises(voice_service.VoiceProcessingError, match="много времени"):
                await voice_service.process_voice_message(
                    _FakeBot(),
                    user,
                    _voice(),
                    provider=FailingProvider(),
                    now_utc=now,
                )
            assert list(tmp_path.iterdir()) == []
            metrics = voice_service.get_voice_metrics()
            assert metrics["failure_stt_timeout"] == 1
            assert metrics["cleanup_success"] == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure_kind", "expected_category", "expected_fragment"),
    [
        ("malformed_output", "malformed_output", "Не удалось получить текст"),
        ("crashed", "crashed", "завершилась с ошибкой"),
    ],
)
def test_voice_process_provider_failures_are_safe_and_clean(
    monkeypatch,
    tmp_path,
    failure_kind: str,
    expected_category: str,
    expected_fragment: str,
):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            monkeypatch.setattr(voice_service, "settings", _settings(tmp_path))
            voice_service.voice_metrics = voice_service.VoiceMetrics()

            async def fake_convert(
                source: Path, destination: Path, limits: VoiceMediaLimits
            ) -> int:
                destination.write_bytes(b"RIFF" + b"0" * 100)
                return destination.stat().st_size

            monkeypatch.setattr(voice_service, "convert_voice_to_wav", fake_convert)

            class FailingProvider:
                def is_available(self) -> bool:
                    return True

                async def transcribe(self, audio_path: Path) -> str:
                    if failure_kind == "malformed_output":
                        return "\x00"
                    raise RuntimeError("private provider path")

            user = await _add_user(session_factory)
            with pytest.raises(
                voice_service.VoiceProcessingError, match=expected_fragment
            ) as caught:
                await voice_service.process_voice_message(
                    _FakeBot(),
                    user,
                    _voice(),
                    provider=FailingProvider(),
                    now_utc=now,
                )
            assert caught.value.category == expected_category
            assert "private provider path" not in str(caught.value)
            assert list(tmp_path.iterdir()) == []
            metrics = voice_service.get_voice_metrics()
            assert metrics[f"failure_stt_{expected_category}"] == 1
            assert metrics["cleanup_success"] == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_voice_process_cancellation_cleans_temp_directory(monkeypatch, tmp_path):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            monkeypatch.setattr(voice_service, "settings", _settings(tmp_path))
            voice_service.voice_metrics = voice_service.VoiceMetrics()

            async def fake_convert(
                source: Path, destination: Path, limits: VoiceMediaLimits
            ) -> int:
                destination.write_bytes(b"RIFF" + b"0" * 100)
                return destination.stat().st_size

            monkeypatch.setattr(voice_service, "convert_voice_to_wav", fake_convert)

            class CancellingProvider:
                def is_available(self) -> bool:
                    return True

                async def transcribe(self, audio_path: Path) -> str:
                    raise asyncio.CancelledError

            user = await _add_user(session_factory)
            with pytest.raises(asyncio.CancelledError):
                await voice_service.process_voice_message(
                    _FakeBot(),
                    user,
                    _voice(),
                    provider=CancellingProvider(),
                    now_utc=now,
                )
            assert list(tmp_path.iterdir()) == []
            metrics = voice_service.get_voice_metrics()
            assert metrics["failure_stt_cancelled"] == 1
            assert metrics["cleanup_success"] == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_voice_ambiguous_transcript_uses_persisted_clarification(monkeypatch, tmp_path):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            monkeypatch.setattr(voice_service, "settings", _settings(tmp_path))
            voice_service.voice_metrics = voice_service.VoiceMetrics()

            async def fake_convert(
                source: Path, destination: Path, limits: VoiceMediaLimits
            ) -> int:
                destination.write_bytes(b"RIFF" + b"0" * 100)
                return destination.stat().st_size

            monkeypatch.setattr(voice_service, "convert_voice_to_wav", fake_convert)
            user = await _add_user(session_factory)
            result = await voice_service.process_voice_message(
                _FakeBot(),
                user,
                _voice(),
                source_message_id=7100,
                provider=_FakeProvider("после обеда позвонить врачу"),
                now_utc=now,
            )

            assert result.draft is None
            assert result.message
            assert "время" in result.message.lower()
            assert list(tmp_path.iterdir()) == []
            message = SimpleNamespace(
                text="14:00",
                answer=AsyncMock(return_value=SimpleNamespace(message_id=9100)),
            )
            assert await reminders_handler._handle_clarification(
                message,
                user,
                now_utc=now,
            )
            async with session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(Reminder)) == 0
                draft = await session.scalar(select(VoiceReminderDraft))
                assert draft is not None
                assert draft.transcript == "после обеда позвонить врачу"
                assert draft.source_message_id == 7100
                clarification = await session.scalar(select(ReminderClarification))
                assert clarification is None
            message.answer.assert_awaited_once()
            metrics = voice_service.get_voice_metrics()
            assert metrics["parse_clarification"] == 1
            assert metrics["parse_correction"] == 1
            assert metrics["stt_success"] == 1

            created = await voice_service.confirm_voice_draft(
                user,
                draft.id,
                expected_revision=draft.action_revision,
                expected_message_id=9100,
                now_utc=now,
            )
            assert created is not None
            assert (
                await voice_service.confirm_voice_draft(
                    user,
                    draft.id,
                    expected_revision=draft.action_revision,
                    expected_message_id=9100,
                    now_utc=now,
                )
                is None
            )
            async with session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(Reminder)) == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_voice_conversion_rejects_decoded_output_over_duration_bound(monkeypatch, tmp_path):
    async def scenario() -> None:
        source = tmp_path / "input.ogg"
        destination = tmp_path / "normalized.wav"
        source.write_bytes(b"ogg")
        limits = VoiceMediaLimits(
            max_file_size_bytes=10_000,
            max_duration_seconds=1,
            download_timeout_seconds=30,
            conversion_timeout_seconds=30,
            conversion_command=sys.executable,
        )
        validate_voice_metadata(_voice(duration=1, file_size=100), limits)
        captured: dict[str, object] = {}

        class Stream:
            def __init__(self) -> None:
                self.chunks = [
                    b"R" * 44,
                    b"P" * (VOICE_PCM_BYTES_PER_SECOND + 1),
                ]

            async def read(self, _: int) -> bytes:
                return self.chunks.pop(0) if self.chunks else b""

        class Process:
            returncode = None
            stdout = Stream()
            killed = False

            async def wait(self) -> int:
                if self.killed:
                    self.returncode = -9
                    return self.returncode
                await asyncio.sleep(60)
                return 0

            def kill(self) -> None:
                self.killed = True

        process = Process()

        async def fake_create(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return process

        monkeypatch.setattr("app.services.voice_media.asyncio.create_subprocess_exec", fake_create)
        with pytest.raises(VoiceMediaError, match="превышает допустимую длительность") as caught:
            await convert_voice_to_wav(source, destination, limits)
        assert caught.value.category == "duration"
        assert process.killed
        assert not destination.exists()
        assert list(captured["args"])[-1] == "pipe:1"
        assert captured["kwargs"]["stdout"] == asyncio.subprocess.PIPE

    asyncio.run(scenario())


def test_voice_conversion_logs_safe_nonzero_diagnostic(monkeypatch, tmp_path, caplog):
    async def scenario() -> None:
        source = tmp_path / "input.ogg"
        destination = tmp_path / "normalized.wav"
        source.write_bytes(b"ogg")

        class Stream:
            async def read(self, _: int) -> bytes:
                return b""

        class Process:
            returncode = 187
            stdout = Stream()

            async def wait(self) -> int:
                return self.returncode

            def kill(self) -> None:
                raise AssertionError("failed process already exited")

        async def fake_create(*args, **kwargs):
            return Process()

        monkeypatch.setattr("app.services.voice_media.asyncio.create_subprocess_exec", fake_create)
        limits = VoiceMediaLimits(
            max_file_size_bytes=10_000,
            max_duration_seconds=1,
            download_timeout_seconds=30,
            conversion_timeout_seconds=30,
            conversion_command=sys.executable,
        )

        with pytest.raises(VoiceMediaError, match="Не удалось подготовить"):
            await convert_voice_to_wav(source, destination, limits)

    caplog.set_level(logging.WARNING, logger="app.services.voice_media")
    asyncio.run(scenario())

    records = [record for record in caplog.records if record.name == "app.services.voice_media"]
    assert len(records) == 1
    assert records[0].message == "Voice media conversion failed"
    assert records[0].extra_data == "stage=media category=conversion return_code=187"
    assert str(tmp_path) not in records[0].extra_data


def test_voice_flow_arbitration_clears_stale_scenarios(monkeypatch, tmp_path):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            monkeypatch.setattr(voice_service, "settings", _settings(tmp_path))
            user = await _add_user(session_factory)
            parsed = ParsedReminder(
                local_dt=datetime(2026, 9, 8, 15, 0),
                text="проверить сценарий",
            )
            clarification_request = parse_reminder_input(
                "напомни после обеда проверить сценарий",
                datetime(2026, 9, 8, 13, 0),
            )
            assert isinstance(clarification_request, ClarificationRequest)

            await clarification_service.create_clarification(
                user,
                clarification_request,
                now_utc=now,
            )
            first_voice = await voice_service.create_voice_draft(
                user,
                "напомни завтра в 9 проверить сценарий",
                parsed,
                source_message_id=7001,
                now_utc=now,
            )
            assert first_voice.id
            async with session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(ActionDraft)) == 0
                assert (
                    await session.scalar(select(func.count()).select_from(ReminderClarification))
                    == 0
                )

            await clarification_service.create_clarification(
                user,
                clarification_request,
                now_utc=now,
            )
            async with session_factory() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(VoiceReminderDraft)) == 0
                )

            # The action draft is created against a persisted future reminder.
            async with session_factory() as session, session.begin():
                reminder = await reminder_service.create_reminder_in_session(
                    session,
                    user,
                    parsed.local_dt,
                    parsed.text,
                    now_utc=now,
                )
            action = await reminder_service.create_action_draft(
                user,
                reminder.id,
                action_type="snooze",
                expected_action_revision=reminder.action_revision,
                current_step="awaiting_value",
                now_utc=now,
            )
            assert action is not None
            voice_after_action = await voice_service.create_voice_draft(
                user,
                "напомни завтра в 9 новый сценарий",
                parsed,
                source_message_id=7002,
                now_utc=now,
            )
            assert voice_after_action.source_message_id == 7002
            async with session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(ActionDraft)) == 0
                assert (
                    await session.scalar(select(func.count()).select_from(ReminderClarification))
                    == 0
                )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_voice_stt_concurrency_queue_is_bounded(monkeypatch, tmp_path):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            monkeypatch.setattr(
                voice_service,
                "settings",
                _settings(tmp_path, voice_stt_queue_timeout_seconds=1),
            )
            voice_service.voice_metrics = voice_service.VoiceMetrics()
            voice_service._stt_semaphore = None
            voice_service._stt_semaphore_limit = None

            async def fake_convert(
                source: Path, destination: Path, limits: VoiceMediaLimits
            ) -> int:
                destination.write_bytes(b"RIFF" + b"0" * 100)
                return destination.stat().st_size

            monkeypatch.setattr(voice_service, "convert_voice_to_wav", fake_convert)
            started = asyncio.Event()
            release = asyncio.Event()

            class BlockingProvider:
                def is_available(self) -> bool:
                    return True

                async def transcribe(self, audio_path: Path) -> str:
                    started.set()
                    await release.wait()
                    return "завтра в 9 первое голосовое"

            user = await _add_user(session_factory)
            first_task = asyncio.create_task(
                voice_service.process_voice_message(
                    _FakeBot(),
                    user,
                    _voice(),
                    source_message_id=101,
                    provider=BlockingProvider(),
                    now_utc=now,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=2)

            with pytest.raises(
                voice_service.VoiceProcessingError, match="обрабатывается"
            ) as caught:
                await voice_service.process_voice_message(
                    _FakeBot(),
                    user,
                    _voice(),
                    source_message_id=102,
                    provider=_FakeProvider("завтра в 9 второе голосовое"),
                    now_utc=now,
                )
            assert caught.value.category == "busy"

            release.set()
            first_result = await first_task
            assert first_result.draft is not None
            assert list(tmp_path.iterdir()) == []
            assert voice_service.get_voice_metrics()["failure_concurrency_busy"] == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_voice_draft_confirmation_is_atomic_and_stale_safe(monkeypatch, tmp_path):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            monkeypatch.setattr(voice_service, "settings", _settings(tmp_path))
            user = await _add_user(session_factory)
            parsed = parse_reminder_input(
                "напомни 2026-09-08 15:00 проверить отчёт",
                datetime(2026, 9, 8, 10, 0),
            )
            assert isinstance(parsed, ParsedReminder)
            draft = await voice_service.create_voice_draft(
                user,
                "напомни в 12 проверить отчёт",
                parsed,
                source_message_id=8001,
                now_utc=now,
            )
            assert await voice_service.bind_voice_preview_message(
                user,
                draft.id,
                revision=draft.action_revision,
                message_id=9001,
                now_utc=now,
            )

            other_user = await _add_user(
                session_factory,
                telegram_user_id=1002,
                chat_id=2003,
            )
            assert (
                await voice_service.confirm_voice_draft(
                    other_user,
                    draft.id,
                    expected_revision=draft.action_revision,
                    expected_message_id=9001,
                    now_utc=now,
                )
                is None
            )

            assert (
                await voice_service.confirm_voice_draft(
                    user,
                    draft.id,
                    expected_revision=draft.action_revision,
                    expected_message_id=9002,
                    now_utc=now,
                )
                is None
            )
            async with session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(Reminder)) == 0

            created = await voice_service.confirm_voice_draft(
                user,
                draft.id,
                expected_revision=draft.action_revision,
                expected_message_id=9001,
                now_utc=now,
            )
            assert created is not None
            assert created.text == "проверить отчёт"
            assert await voice_service.get_active_voice_draft(user, now_utc=now) is None
            assert (
                await voice_service.confirm_voice_draft(
                    user,
                    draft.id,
                    expected_revision=draft.action_revision,
                    expected_message_id=9001,
                    now_utc=now,
                )
                is None
            )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_voice_draft_retries_are_idempotent_and_expired_drafts_cleanup(monkeypatch, tmp_path):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            monkeypatch.setattr(
                voice_service, "settings", _settings(tmp_path, voice_draft_ttl_seconds=60)
            )
            user = await _add_user(session_factory)
            parsed = ParsedReminder(local_dt=datetime(2026, 9, 8, 15, 0), text="тест")
            first = await voice_service.create_voice_draft(
                user, "напомни в 15 тест", parsed, source_message_id=42, now_utc=now
            )
            retry = await voice_service.create_voice_draft(
                user, "напомни в 15 тест", parsed, source_message_id=42, now_utc=now
            )
            assert retry.id == first.id

            replaced = await voice_service.create_voice_draft(
                user, "напомни в 15 новый тест", parsed, source_message_id=43, now_utc=now
            )
            assert replaced.source_message_id == 43
            assert replaced.transcript == "напомни в 15 новый тест"
            assert await voice_service.bind_voice_preview_message(
                user,
                replaced.id,
                revision=replaced.action_revision,
                message_id=9002,
                now_utc=now,
            )
            assert await voice_service.discard_voice_draft(
                user,
                replaced.id,
                expected_revision=replaced.action_revision,
                expected_message_id=9002,
                now_utc=now,
            )
            expired = await voice_service.create_voice_draft(
                user, "напомни в 15 истёкший тест", parsed, source_message_id=44, now_utc=now
            )
            assert (
                await voice_service.cleanup_expired_voice_drafts(
                    now_utc=now + timedelta(seconds=61)
                )
                == 1
            )
            assert await voice_service.get_active_voice_draft(user, now_utc=now) is None
            assert expired.source_message_id == 44
            assert voice_service.get_voice_metrics()["cancellation_success"] == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_orphaned_voice_temp_cleanup_is_prefix_and_ttl_bounded(tmp_path: Path) -> None:
    stale = tmp_path / "reminder-bot-voice-stale"
    stale.mkdir()
    (stale / "input.ogg").write_bytes(b"audio")
    fresh = tmp_path / "reminder-bot-voice-fresh"
    fresh.mkdir()
    unrelated = tmp_path / "not-a-voice-temp"
    unrelated.mkdir()
    now_epoch = time.time()
    os.utime(stale, (now_epoch - 61, now_epoch - 61))

    assert (
        cleanup_orphaned_voice_temp_dirs(
            str(tmp_path),
            max_age_seconds=60,
            now_epoch=now_epoch,
        )
        == 1
    )
    assert not stale.exists()
    assert fresh.exists()
    assert unrelated.exists()


def test_voice_confirmation_preserves_instant_dst_fold_and_expired_cancel_is_stale(
    monkeypatch, tmp_path
):
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        try:
            now = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
            monkeypatch.setattr(
                voice_service, "settings", _settings(tmp_path, voice_draft_ttl_seconds=60)
            )
            user = await _add_user(session_factory, timezone="Europe/Berlin")
            instant = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
            parsed = ParsedReminder(
                local_dt=instant,
                text="проверить инстант",
                datetime_semantics="instant",
            )
            draft = await voice_service.create_voice_draft(
                user,
                "напомни через час проверить инстант",
                parsed,
                source_message_id=77,
                now_utc=now,
            )
            created = await voice_service.confirm_voice_draft(
                user,
                draft.id,
                expected_revision=draft.action_revision,
                now_utc=now,
            )
            assert created is not None
            assert created.remind_at_utc.replace(tzinfo=UTC) == instant

            expired = await voice_service.create_voice_draft(
                user,
                "напомни через час отменяемый инстант",
                ParsedReminder(
                    local_dt=instant,
                    text="отменяемый инстант",
                    datetime_semantics="instant",
                ),
                source_message_id=78,
                now_utc=now,
            )
            assert not await voice_service.discard_voice_draft(
                user,
                expired.id,
                expected_revision=expired.action_revision,
                now_utc=now + timedelta(seconds=61),
            )
            assert await voice_service.get_active_voice_draft(user, now_utc=now) is None
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_voice_callback_keyboard_is_compact_and_scoped() -> None:
    markup = voice_draft_kb(42, 1)
    callbacks = [
        parse_callback(button.callback_data) for row in markup.inline_keyboard for button in row
    ]
    assert all(callback is not None for callback in callbacks)
    assert {callback.action for callback in callbacks if callback is not None} == {
        CallbackAction.CREATE,
        CallbackAction.CANCEL,
    }
    assert all(
        callback is not None
        and callback.target == CallbackTarget.VOICE_DRAFT
        and callback.origin == CallbackOrigin.VOICE
        and callback.target_id == 42
        and callback.revision == 1
        for callback in callbacks
    )


def test_whisper_cpp_provider_uses_explicit_bounded_argv(monkeypatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        model = tmp_path / "ggml-base.bin"
        model.write_bytes(b"model")
        audio = tmp_path / "normalized.wav"
        audio.write_bytes(b"wav")
        captured: dict[str, object] = {}

        class Stream:
            def __init__(self, chunks: list[bytes]) -> None:
                self.chunks = chunks

            async def read(self, _: int) -> bytes:
                return self.chunks.pop(0) if self.chunks else b""

        class Process:
            returncode = 0
            stdout = Stream(
                [
                    b"system_info: hidden\n[00:00:00.000 --> 00:00:01.000] ",
                    "напомни завтра в 9 тест\n".encode(),
                ]
            )

            async def wait(self) -> int:
                return 0

            def kill(self) -> None:
                raise AssertionError("ready process must not be killed")

        async def fake_create(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return Process()

        monkeypatch.setattr(
            "app.services.speech_to_text.asyncio.create_subprocess_exec", fake_create
        )
        provider = WhisperCppSpeechToTextProvider(
            command=sys.executable,
            model_path=str(model),
            language="ru",
            threads=2,
            timeout_seconds=5,
        )
        assert provider.is_available()
        assert await provider.transcribe(audio) == "напомни завтра в 9 тест"
        assert model.exists()
        args = list(captured["args"])
        assert args[0] == sys.executable
        assert args[1:] == [
            "-m",
            str(model),
            "-f",
            str(audio),
            "-l",
            "ru",
            "-t",
            "2",
            "-nt",
        ]
        assert "shell" not in captured["kwargs"]

    asyncio.run(scenario())


def test_whisper_cpp_provider_kills_timed_out_process(monkeypatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        model = tmp_path / "ggml-base.bin"
        model.write_bytes(b"model")
        audio = tmp_path / "normalized.wav"
        audio.write_bytes(b"wav")

        class Stream:
            async def read(self, _: int) -> bytes:
                return b""

        class Process:
            returncode = None
            stdout = Stream()
            killed = False

            async def wait(self) -> int:
                if self.killed:
                    self.returncode = -9
                    return self.returncode
                await asyncio.sleep(60)
                return 0

            def kill(self) -> None:
                self.killed = True

        process = Process()

        async def fake_create(*args, **kwargs):
            return process

        monkeypatch.setattr(
            "app.services.speech_to_text.asyncio.create_subprocess_exec", fake_create
        )
        provider = WhisperCppSpeechToTextProvider(
            command=sys.executable,
            model_path=str(model),
            timeout_seconds=0.01,
        )
        with pytest.raises(SpeechToTextError, match="слишком много времени") as caught:
            await provider.transcribe(audio)
        assert caught.value.category == "timeout"
        assert process.killed

    asyncio.run(scenario())
