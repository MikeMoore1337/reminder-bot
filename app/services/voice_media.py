from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

from aiogram import Bot
from aiogram.types import Voice

logger = logging.getLogger(__name__)

VOICE_ALLOWED_MIME_TYPES = frozenset({"audio/ogg", "audio/opus", "application/ogg"})
VOICE_PCM_SAMPLE_RATE = 16_000
VOICE_PCM_BYTES_PER_SECOND = VOICE_PCM_SAMPLE_RATE * 2
VOICE_WAV_HEADER_BYTES = 44
VOICE_TEMP_DIR_PREFIX = "reminder-bot-voice-"
VOICE_CONVERSION_READ_CHUNK_BYTES = 64 * 1024


def _size_limit_message(max_bytes: int) -> str:
    max_megabytes = max(1, (max_bytes + 999_999) // 1_000_000)
    return f"Голосовое сообщение слишком большое. Лимит — {max_megabytes} МБ."


class VoiceMediaError(RuntimeError):
    """Safe, user-facing classification for a media-boundary failure."""

    def __init__(self, category: str, public_message: str) -> None:
        super().__init__(public_message)
        self.category = category
        self.public_message = public_message


@dataclass(frozen=True, slots=True)
class VoiceMediaLimits:
    max_file_size_bytes: int
    max_duration_seconds: int
    download_timeout_seconds: int
    conversion_timeout_seconds: int
    conversion_command: str


class _BoundedWriter:
    """BinaryIO facade that fails before a download can exceed its byte limit."""

    def __init__(self, raw: BinaryIO, max_bytes: int) -> None:
        self._raw = raw
        self._max_bytes = max_bytes
        self.size = 0

    def write(self, data: bytes) -> int:
        if self.size + len(data) > self._max_bytes:
            raise VoiceMediaError(
                "size",
                _size_limit_message(self._max_bytes),
            )
        written = self._raw.write(data)
        self.size += written
        return written

    def flush(self) -> None:
        self._raw.flush()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._raw.seek(offset, whence)


def validate_voice_metadata(voice: Voice, limits: VoiceMediaLimits) -> None:
    if not isinstance(voice.file_id, str) or not voice.file_id.strip():
        raise VoiceMediaError("validation", "Не удалось определить голосовое сообщение.")

    mime_type = voice.mime_type.strip().lower() if voice.mime_type else None
    # Telegram's Voice object already identifies an OGG/Opus voice message;
    # older updates may omit mime_type, so absence is accepted while an
    # explicitly unsupported type is rejected.
    if mime_type is not None and mime_type not in VOICE_ALLOWED_MIME_TYPES:
        raise VoiceMediaError(
            "mime_type",
            "Поддерживаются только голосовые сообщения Telegram в формате OGG/Opus.",
        )

    if not isinstance(voice.duration, int) or voice.duration <= 0:
        raise VoiceMediaError(
            "duration", "Не удалось определить длительность голосового сообщения."
        )
    if voice.duration > limits.max_duration_seconds:
        raise VoiceMediaError(
            "duration",
            f"Голосовое сообщение слишком длинное. Лимит — {limits.max_duration_seconds} секунд.",
        )

    if voice.file_size is not None and (
        not isinstance(voice.file_size, int)
        or voice.file_size <= 0
        or voice.file_size > limits.max_file_size_bytes
    ):
        raise VoiceMediaError(
            "size",
            _size_limit_message(limits.max_file_size_bytes),
        )


def _command_parts(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise VoiceMediaError(
            "conversion", "Локальный конвертер голосовых сообщений настроен неверно."
        ) from exc
    if not parts:
        raise VoiceMediaError("conversion", "Локальный конвертер голосовых сообщений недоступен.")
    return parts


async def download_voice(
    bot: Bot,
    voice: Voice,
    destination: Path,
    limits: VoiceMediaLimits,
) -> int:
    """Download a Telegram voice message into a bounded private temp file."""

    try:
        file_info = await bot.get_file(
            voice.file_id,
            request_timeout=limits.download_timeout_seconds,
        )
        if file_info.file_size is not None and file_info.file_size > limits.max_file_size_bytes:
            raise VoiceMediaError(
                "size",
                _size_limit_message(limits.max_file_size_bytes),
            )
        if not file_info.file_path:
            raise VoiceMediaError(
                "download", "Не удалось получить голосовое сообщение. Попробуй ещё раз."
            )

        with destination.open("wb") as raw:
            bounded = _BoundedWriter(raw, limits.max_file_size_bytes)
            await bot.download_file(
                file_info.file_path,
                destination=cast(BinaryIO, bounded),
                timeout=limits.download_timeout_seconds,
                seek=False,
            )
            bounded.flush()
            if bounded.size <= 0:
                raise VoiceMediaError("download", "Голосовое сообщение оказалось пустым.")
            return bounded.size
    except VoiceMediaError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise VoiceMediaError(
            "download", "Не удалось получить голосовое сообщение. Попробуй ещё раз."
        ) from exc


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError, OSError):
            process.kill()
    with suppress(TimeoutError, ProcessLookupError, OSError):
        await asyncio.wait_for(process.wait(), timeout=2)


async def _write_bounded_wav(
    stdout: Any,
    destination: Path,
    max_bytes: int,
) -> int:
    total = 0
    try:
        with destination.open("wb") as output:
            while True:
                chunk = await stdout.read(VOICE_CONVERSION_READ_CHUNK_BYTES)
                if not chunk:
                    break
                next_total = total + len(chunk)
                if next_total > max_bytes:
                    raise VoiceMediaError(
                        "duration",
                        "Голосовое сообщение превышает допустимую длительность.",
                    )
                output.write(chunk)
                total = next_total
            output.flush()
    except VoiceMediaError:
        raise
    except asyncio.CancelledError:
        raise
    except OSError as exc:
        raise VoiceMediaError("conversion", "Не удалось подготовить голосовое сообщение.") from exc
    return total


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task


async def _abort_conversion(
    process: asyncio.subprocess.Process,
    output_task: asyncio.Task[Any],
    wait_task: asyncio.Task[Any],
) -> None:
    await _terminate_process(process)
    await _cancel_task(output_task)
    await _cancel_task(wait_task)


def _log_conversion_failure(
    category: str,
    *,
    return_code: int | None = None,
    error_type: str | None = None,
) -> None:
    fields = ["stage=media", f"category={category}"]
    if return_code is not None:
        fields.append(f"return_code={return_code}")
    if error_type is not None:
        fields.append(f"error_type={error_type[:80]}")
    logger.warning("Voice media conversion failed", extra={"extra_data": " ".join(fields)})


def _remove_partial_output(destination: Path) -> None:
    with suppress(FileNotFoundError):
        destination.unlink()


async def convert_voice_to_wav(
    source: Path,
    destination: Path,
    limits: VoiceMediaLimits,
) -> int:
    """Convert OGG/Opus to bounded mono 16 kHz PCM WAV using an external CLI."""

    try:
        command = _command_parts(limits.conversion_command)
    except VoiceMediaError as exc:
        _log_conversion_failure(exc.category)
        raise
    argv = [
        *command,
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        str(VOICE_PCM_SAMPLE_RATE),
        "-sample_fmt",
        "s16",
        "-f",
        "wav",
        "-y",
        "pipe:1",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        _log_conversion_failure("conversion_unavailable", error_type=type(exc).__name__)
        raise VoiceMediaError(
            "conversion_unavailable", "Локальная обработка голосовых сообщений сейчас недоступна."
        ) from exc

    stdout = process.stdout
    if stdout is None:
        await _terminate_process(process)
        _log_conversion_failure("conversion", error_type="missing_stdout")
        raise VoiceMediaError("conversion", "Не удалось подготовить голосовое сообщение.")

    max_wav_size = VOICE_WAV_HEADER_BYTES + limits.max_duration_seconds * VOICE_PCM_BYTES_PER_SECOND
    output_task = asyncio.create_task(_write_bounded_wav(stdout, destination, max_wav_size))
    wait_task = asyncio.create_task(process.wait())
    try:
        return_code, size = await asyncio.wait_for(
            asyncio.gather(wait_task, output_task),
            timeout=limits.conversion_timeout_seconds,
        )
    except TimeoutError as exc:
        await _abort_conversion(process, output_task, wait_task)
        _remove_partial_output(destination)
        _log_conversion_failure("conversion_timeout")
        raise VoiceMediaError(
            "conversion_timeout", "Обработка голосового сообщения заняла слишком много времени."
        ) from exc
    except VoiceMediaError as exc:
        await _abort_conversion(process, output_task, wait_task)
        _remove_partial_output(destination)
        _log_conversion_failure(exc.category)
        raise
    except asyncio.CancelledError:
        await _abort_conversion(process, output_task, wait_task)
        _remove_partial_output(destination)
        raise

    if return_code != 0 or process.returncode != 0:
        _remove_partial_output(destination)
        _log_conversion_failure("conversion", return_code=return_code)
        raise VoiceMediaError("conversion", "Не удалось подготовить голосовое сообщение.")
    if size <= VOICE_WAV_HEADER_BYTES:
        _remove_partial_output(destination)
        _log_conversion_failure("duration")
        raise VoiceMediaError("duration", "Голосовое сообщение превышает допустимую длительность.")
    return size


def new_voice_temp_dir(root: str | None) -> Path:
    try:
        return Path(tempfile.mkdtemp(prefix=VOICE_TEMP_DIR_PREFIX, dir=root or None))
    except OSError as exc:
        raise VoiceMediaError(
            "temp", "Не удалось подготовить временное хранилище для голосового сообщения."
        ) from exc


def cleanup_orphaned_voice_temp_dirs(
    root: str | None,
    *,
    max_age_seconds: int,
    now_epoch: float | None = None,
) -> int:
    """Remove only stale temp directories created by this voice flow."""

    base = Path(root) if root else Path(tempfile.gettempdir())
    if not base.is_dir():
        return 0

    cutoff = (time.time() if now_epoch is None else now_epoch) - max_age_seconds
    removed = 0
    for candidate in base.iterdir():
        if not candidate.name.startswith(VOICE_TEMP_DIR_PREFIX):
            continue
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            if candidate.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(candidate)
            removed += 1
        except FileNotFoundError:
            continue
    return removed
