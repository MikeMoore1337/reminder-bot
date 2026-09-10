from __future__ import annotations

import argparse
import io
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

PREFLIGHT_COMMAND_TIMEOUT_SECONDS = 20
PREFLIGHT_VERSION_TIMEOUT_SECONDS = 5
PREFLIGHT_MAX_WAV_BYTES = 1_048_576
SYNTHETIC_DURATION_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class VoiceRuntimePreflightError(RuntimeError):
    category: str
    return_code: int | None = None

    def __str__(self) -> str:
        if self.return_code is None:
            return self.category
        return f"{self.category} return_code={self.return_code}"


def _command_parts(command: str, *, category: str) -> list[str]:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise VoiceRuntimePreflightError(category) from exc
    if not parts:
        raise VoiceRuntimePreflightError(category)
    return parts


def _resolve_executable(parts: Sequence[str], *, category: str) -> None:
    executable = parts[0]
    if Path(executable).parent != Path("."):
        path = Path(executable)
        if not path.is_file():
            raise VoiceRuntimePreflightError(category)
        if not os.access(path, os.X_OK):
            raise VoiceRuntimePreflightError(f"{category}_not_executable")
        return
    if shutil.which(executable) is None:
        raise VoiceRuntimePreflightError(category)


def _run(
    argv: Sequence[str],
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise VoiceRuntimePreflightError("executable_missing") from exc
    except PermissionError as exc:
        raise VoiceRuntimePreflightError("executable_not_executable") from exc
    except subprocess.TimeoutExpired as exc:
        raise VoiceRuntimePreflightError("command_timeout") from exc
    except OSError as exc:
        raise VoiceRuntimePreflightError(f"runtime_{type(exc).__name__}") from exc


def _combined_output(result: subprocess.CompletedProcess[bytes]) -> str:
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


def _run_checked(
    argv: Sequence[str],
    *,
    category: str,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[bytes]:
    result = _run(argv, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        raise VoiceRuntimePreflightError(category, result.returncode)
    return result


def _require_capability(
    command: Sequence[str],
    *,
    option: str,
    pattern: str,
    category: str,
) -> None:
    result = _run_checked(
        [*command, "-hide_banner", option],
        category=category,
        timeout_seconds=PREFLIGHT_VERSION_TIMEOUT_SECONDS,
    )
    if re.search(pattern, _combined_output(result), flags=re.IGNORECASE | re.MULTILINE) is None:
        raise VoiceRuntimePreflightError(category)


def _validate_wav_output(data: bytes) -> None:
    if len(data) > PREFLIGHT_MAX_WAV_BYTES:
        raise VoiceRuntimePreflightError("wav_output_too_large")
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
            compression = handle.getcomptype()
    except (EOFError, wave.Error) as exc:
        raise VoiceRuntimePreflightError("wav_output_invalid") from exc

    if (
        channels != 1
        or sample_width != 2
        or sample_rate != 16_000
        or frame_count <= 0
        or compression != "NONE"
    ):
        raise VoiceRuntimePreflightError("wav_output_shape")


def _synthetic_conversion(command: Sequence[str]) -> None:
    try:
        with tempfile.TemporaryDirectory(prefix="reminder-bot-voice-preflight-") as directory:
            source = Path(directory) / "synthetic.ogg"
            _run_checked(
                [
                    *command,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency=1000:duration={SYNTHETIC_DURATION_SECONDS}",
                    "-c:a",
                    "libopus",
                    "-f",
                    "ogg",
                    "-y",
                    str(source),
                ],
                category="synthetic_fixture_failed",
                timeout_seconds=PREFLIGHT_COMMAND_TIMEOUT_SECONDS,
            )
            if not source.is_file() or source.stat().st_size <= 0:
                raise VoiceRuntimePreflightError("synthetic_fixture_empty")

            result = _run_checked(
                [
                    *command,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-sample_fmt",
                    "s16",
                    "-f",
                    "wav",
                    "-y",
                    "pipe:1",
                ],
                category="converter_nonzero",
                timeout_seconds=PREFLIGHT_COMMAND_TIMEOUT_SECONDS,
            )
            _validate_wav_output(result.stdout)
    except VoiceRuntimePreflightError:
        raise
    except OSError as exc:
        raise VoiceRuntimePreflightError("synthetic_fixture_runtime") from exc


def _check_converter(environment: Mapping[str, str]) -> None:
    command = _command_parts(
        environment.get("VOICE_CONVERSION_COMMAND", "ffmpeg"),
        category="converter_command_invalid",
    )
    _resolve_executable(command, category="converter_missing")
    _run_checked(
        [*command, "-version"],
        category="converter_version_failed",
        timeout_seconds=PREFLIGHT_VERSION_TIMEOUT_SECONDS,
    )
    _require_capability(
        command,
        option="-protocols",
        pattern=r"^\s*pipe\s*$",
        category="converter_pipe_missing",
    )
    _require_capability(
        command,
        option="-demuxers",
        pattern=r"\bogg\b",
        category="converter_ogg_demuxer_missing",
    )
    _require_capability(
        command,
        option="-decoders",
        pattern=r"\bopus\b",
        category="converter_opus_decoder_missing",
    )
    _require_capability(
        command,
        option="-muxers",
        pattern=r"\bwav\b",
        category="converter_wav_muxer_missing",
    )
    _require_capability(
        command,
        option="-codecs",
        pattern=r"\bpcm_s16le\b",
        category="converter_pcm_s16_missing",
    )
    _synthetic_conversion(command)


def _check_stt(environment: Mapping[str, str]) -> None:
    model_path = environment.get("VOICE_STT_MODEL_PATH", "").strip()
    if not model_path:
        raise VoiceRuntimePreflightError("stt_model_missing")
    model = Path(model_path)
    try:
        if not model.is_file() or model.stat().st_size <= 0:
            raise VoiceRuntimePreflightError("stt_model_missing")
    except OSError as exc:
        raise VoiceRuntimePreflightError("stt_model_unreadable") from exc

    command = _command_parts(
        environment.get("VOICE_STT_COMMAND", "whisper-cli"),
        category="stt_command_invalid",
    )
    _resolve_executable(command, category="stt_executable_missing")
    _run_checked(
        [*command, "--help"],
        category="stt_help_failed",
        timeout_seconds=PREFLIGHT_VERSION_TIMEOUT_SECONDS,
    )


def run_voice_runtime_preflight(
    *,
    require_media: bool = False,
    environment: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if environment is None else environment
    model_path = env.get("VOICE_STT_MODEL_PATH", "").strip()
    if not require_media and not model_path:
        return "VOICE_RUNTIME_PREFLIGHT_SKIPPED_DISABLED"

    _check_converter(env)
    if model_path:
        _check_stt(env)
    return "VOICE_RUNTIME_PREFLIGHT_OK"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the configured voice runtime safely.")
    parser.add_argument(
        "--require-media",
        action="store_true",
        help="run the media preflight even when local STT is disabled",
    )
    args = parser.parse_args(argv)
    try:
        result = run_voice_runtime_preflight(require_media=args.require_media)
    except VoiceRuntimePreflightError as exc:
        print(f"VOICE_RUNTIME_PREFLIGHT_FAILED {exc}")
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
