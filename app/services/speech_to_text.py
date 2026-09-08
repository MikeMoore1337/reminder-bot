from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Protocol

MAX_TRANSCRIPT_LENGTH = 4096
MAX_PROVIDER_OUTPUT_BYTES = 128 * 1024


class SpeechToTextError(RuntimeError):
    """Safe classification for local provider failures."""

    def __init__(self, category: str, public_message: str) -> None:
        super().__init__(public_message)
        self.category = category
        self.public_message = public_message


class SpeechToTextProvider(Protocol):
    async def transcribe(self, audio_path: Path) -> str: ...


def normalize_transcript(value: str) -> str:
    if not isinstance(value, str):
        raise SpeechToTextError(
            "malformed_output", "Локальная расшифровка вернула некорректный результат."
        )

    chunks: list[str] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and "]" in line:
            line = line.split("]", 1)[1].strip()
        if line.startswith(
            (
                "whisper_",
                "main:",
                "system_info:",
                "ggml_",
                "load_",
                "encode_",
                "decode_",
            )
        ):
            continue
        chunks.append(line)

    transcript = re.sub(r"\s+", " ", " ".join(chunks)).strip()
    if not transcript:
        raise SpeechToTextError(
            "malformed_output", "Не удалось получить текст из голосового сообщения."
        )
    if len(transcript) > MAX_TRANSCRIPT_LENGTH:
        raise SpeechToTextError("output_limit", "Расшифровка голосового сообщения слишком длинная.")
    if any(ord(char) < 32 and char not in "\t" for char in transcript):
        raise SpeechToTextError(
            "malformed_output", "Локальная расшифровка вернула некорректный результат."
        )
    return transcript


def _command_parts(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise SpeechToTextError("unavailable", "Локальная расшифровка сейчас недоступна.") from exc
    if not parts:
        raise SpeechToTextError("unavailable", "Локальная расшифровка сейчас недоступна.")
    return parts


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()
    with suppress(TimeoutError, ProcessLookupError):
        await asyncio.wait_for(process.wait(), timeout=2)


async def _read_limited(stream: asyncio.StreamReader, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(16_384)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise SpeechToTextError(
                "output_limit", "Локальная расшифровка вернула слишком большой результат."
            )
        chunks.append(chunk)


class WhisperCppSpeechToTextProvider:
    """On-demand whisper.cpp CLI adapter with explicit argv and bounded lifetime."""

    def __init__(
        self,
        *,
        command: str,
        model_path: str | None,
        language: str = "ru",
        threads: int = 2,
        timeout_seconds: int = 90,
    ) -> None:
        self.command = command
        self.model_path = model_path
        self.language = language.strip() or "ru"
        self.threads = threads
        self.timeout_seconds = timeout_seconds

    def _parts(self) -> list[str]:
        return _command_parts(self.command)

    def is_available(self) -> bool:
        if not self.model_path:
            return False
        try:
            parts = self._parts()
        except SpeechToTextError:
            return False
        executable = Path(parts[0])
        if not executable.is_file() and shutil.which(parts[0]) is None:
            return False
        return Path(self.model_path).is_file()

    async def transcribe(self, audio_path: Path) -> str:
        if not self.is_available():
            raise SpeechToTextError("unavailable", "Локальная расшифровка сейчас недоступна.")

        argv = [
            *self._parts(),
            "-m",
            self.model_path or "",
            "-f",
            str(audio_path),
            "-l",
            self.language,
            "-t",
            str(self.threads),
            "-nt",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise SpeechToTextError(
                "unavailable", "Локальная расшифровка сейчас недоступна."
            ) from exc

        if process.stdout is None:  # pragma: no cover - asyncio contract guard
            await _terminate_process(process)
            raise SpeechToTextError(
                "malformed_output", "Не удалось получить текст из голосового сообщения."
            )

        output_task = asyncio.create_task(_read_limited(process.stdout, MAX_PROVIDER_OUTPUT_BYTES))
        try:
            await asyncio.wait_for(process.wait(), timeout=self.timeout_seconds)
            output = await asyncio.wait_for(output_task, timeout=2)
        except TimeoutError as exc:
            await _terminate_process(process)
            output_task.cancel()
            await asyncio.gather(output_task, return_exceptions=True)
            raise SpeechToTextError(
                "timeout", "Локальная расшифровка заняла слишком много времени."
            ) from exc
        except SpeechToTextError:
            await _terminate_process(process)
            await asyncio.gather(output_task, return_exceptions=True)
            raise
        except asyncio.CancelledError:
            await _terminate_process(process)
            output_task.cancel()
            await asyncio.gather(output_task, return_exceptions=True)
            raise

        if process.returncode != 0:
            raise SpeechToTextError("crashed", "Локальная расшифровка завершилась с ошибкой.")
        return normalize_transcript(output.decode("utf-8", errors="replace"))
