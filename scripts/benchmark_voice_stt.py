"""Offline benchmark runner for the local Whisper voice-reminder pipeline.

The runner deliberately reuses the production media converter and Whisper
adapter.  It never contacts Telegram, a remote STT provider, or an LLM.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from unittest.mock import patch
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.reminder_parser import (
    ClarificationRequest,
    DeadlineRequest,
    ParsedReminder,
)
from app.services.speech_to_text import (
    SpeechToTextError,
    SpeechToTextProvider,
    WhisperCppSpeechToTextProvider,
    normalize_transcript,
)
from app.services.voice_media import (
    VoiceMediaError,
    VoiceMediaLimits,
    convert_voice_to_wav,
)
from app.services.voice_transcript import parse_voice_transcript
from app.utils.datetime_utils import localize_in_timezone, validate_timezone

ALLOWED_AUDIO_SUFFIXES = frozenset({".wav", ".ogg", ".opus"})
DEFAULT_LANGUAGE = "ru"
DEFAULT_TIMEOUT_SECONDS = 90
DEFAULT_CONVERSION_TIMEOUT_SECONDS = 30
DEFAULT_THREADS = 2
DEFAULT_MAX_FILE_SIZE_BYTES = 10_000_000
DEFAULT_MAX_DURATION_SECONDS = 120
PROCESS_RESOURCE_SAMPLE_INTERVAL_SECONDS = 0.02

ProductKind = Literal["parsed", "deadline", "clarification", "unparsed"]
ParsedValue = ParsedReminder | DeadlineRequest
ProviderFactory = Callable[[Path, "BenchmarkConfig"], SpeechToTextProvider]
Converter = Callable[[Path, Path, VoiceMediaLimits], Awaitable[int]]


class BenchmarkInputError(ValueError):
    """Safe, deterministic validation error for benchmark inputs."""


class BenchmarkOutputError(RuntimeError):
    """Safe report-output error without exposing filesystem details."""


@dataclass(frozen=True, slots=True)
class ExpectedProduct:
    kind: ProductKind
    body: str | None = None
    local_datetime: datetime | None = None
    datetime_semantics: str | None = None
    recurrence_type: str = "none"
    recurrence_interval: int = 1
    recurrence_day_of_month: int | None = None
    mode: str = "normal"
    point_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BenchmarkSample:
    file: str
    expected: str
    tags: tuple[str, ...]
    now_local: datetime
    timezone: str
    expected_product: ExpectedProduct | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    version: int
    samples: tuple[BenchmarkSample, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    command: str = "whisper-cli"
    language: str = DEFAULT_LANGUAGE
    threads: int = DEFAULT_THREADS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    conversion_timeout_seconds: int = DEFAULT_CONVERSION_TIMEOUT_SECONDS
    conversion_command: str = "ffmpeg"
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES
    max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS
    measure_rss: bool = False

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise BenchmarkInputError("пустая команда STT")
        if self.language.casefold() != DEFAULT_LANGUAGE:
            raise BenchmarkInputError("benchmark поддерживает только language=ru")
        if not 1 <= self.threads <= 8:
            raise BenchmarkInputError("threads должен быть в диапазоне 1..8")
        if not 1 <= self.timeout_seconds <= 600:
            raise BenchmarkInputError("timeout_seconds должен быть в диапазоне 1..600")
        if not 1 <= self.conversion_timeout_seconds <= 300:
            raise BenchmarkInputError("conversion_timeout_seconds должен быть в диапазоне 1..300")
        if not self.conversion_command.strip():
            raise BenchmarkInputError("пустая команда конвертации")
        if self.max_file_size_bytes <= 0 or self.max_duration_seconds <= 0:
            raise BenchmarkInputError("лимиты audio должны быть положительными")


def _parse_iso_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkInputError(f"{field} должен быть непустой ISO datetime строкой")
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BenchmarkInputError(f"{field} имеет некорректный ISO datetime") from exc


def _valid_local_offsets(local_dt: datetime, timezone: ZoneInfo) -> frozenset[timedelta]:
    offsets: set[timedelta] = set()
    for fold in (0, 1):
        candidate = local_dt.replace(tzinfo=timezone, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(timezone)
        if round_trip.replace(tzinfo=None) != local_dt:
            continue
        offset = candidate.utcoffset()
        if offset is not None:
            offsets.add(offset)
    return frozenset(offsets)


def _bind_datetime_to_timezone(
    value: datetime,
    timezone_name: str,
    field: str,
) -> datetime:
    """Bind a manifest wall-clock datetime to its declared IANA timezone.

    Naive values use the production DST policy. An explicit offset must match a
    valid occurrence in the declared zone; otherwise the manifest fails closed.
    """

    timezone = ZoneInfo(timezone_name)
    local_value = value.replace(tzinfo=None)
    if value.tzinfo is not None:
        supplied_offset = value.utcoffset()
        valid_offsets = _valid_local_offsets(local_value, timezone)
        if supplied_offset is None or not valid_offsets:
            raise BenchmarkInputError(
                f"{field} содержит недопустимое локальное время для timezone {timezone_name}"
            )
        if supplied_offset not in valid_offsets:
            raise BenchmarkInputError(
                f"{field} имеет несовместимый offset для timezone {timezone_name}"
            )
        for fold in (0, 1):
            candidate = local_value.replace(tzinfo=timezone, fold=fold)
            if candidate.utcoffset() == supplied_offset:
                return candidate

    try:
        return localize_in_timezone(local_value, timezone_name)
    except ValueError as exc:
        raise BenchmarkInputError(f"{field} нельзя привязать к timezone {timezone_name}") from exc


def _parse_expected_product(
    value: Any,
    field: str,
    *,
    timezone: str,
) -> ExpectedProduct:
    if not isinstance(value, dict):
        raise BenchmarkInputError(f"{field} должен быть объектом")

    kind = value.get("kind")
    if kind not in {"parsed", "deadline", "clarification", "unparsed"}:
        raise BenchmarkInputError(
            f"{field}.kind должен быть parsed, deadline, clarification или unparsed"
        )

    body = value.get("body")
    local_datetime = value.get("local_datetime")
    datetime_semantics = value.get("datetime_semantics")
    recurrence_type = value.get("recurrence_type", "none")
    recurrence_interval = value.get("recurrence_interval", 1)
    recurrence_day_of_month = value.get("recurrence_day_of_month")
    mode = value.get("mode", "normal")
    point_codes = value.get("point_codes", [])

    if kind in {"parsed", "deadline"}:
        if not isinstance(body, str) or not body.strip():
            raise BenchmarkInputError(f"{field}.body обязателен для parsed/deadline")
        if local_datetime is None:
            raise BenchmarkInputError(f"{field}.local_datetime обязателен для parsed/deadline")
        parsed_local_datetime = _bind_datetime_to_timezone(
            _parse_iso_datetime(local_datetime, f"{field}.local_datetime"),
            timezone,
            f"{field}.local_datetime",
        )
        if datetime_semantics not in {"wall_clock", "instant"}:
            raise BenchmarkInputError(
                f"{field}.datetime_semantics должен быть wall_clock или instant"
            )
    else:
        parsed_local_datetime = None
        if body is not None or local_datetime is not None or datetime_semantics is not None:
            raise BenchmarkInputError(f"{field} не должен содержать schedule/body для {kind}")

    if not isinstance(recurrence_type, str) or not recurrence_type.strip():
        raise BenchmarkInputError(f"{field}.recurrence_type должен быть строкой")
    if not isinstance(recurrence_interval, int) or recurrence_interval < 1:
        raise BenchmarkInputError(f"{field}.recurrence_interval должен быть >= 1")
    if recurrence_day_of_month is not None and (
        not isinstance(recurrence_day_of_month, int) or not 1 <= recurrence_day_of_month <= 31
    ):
        raise BenchmarkInputError(f"{field}.recurrence_day_of_month некорректен")
    if not isinstance(mode, str) or not mode.strip():
        raise BenchmarkInputError(f"{field}.mode должен быть строкой")
    if not isinstance(point_codes, list) or not all(
        isinstance(point_code, str) and point_code.strip() for point_code in point_codes
    ):
        raise BenchmarkInputError(f"{field}.point_codes должен быть списком строк")

    return ExpectedProduct(
        kind=kind,
        body=body.strip() if isinstance(body, str) else None,
        local_datetime=parsed_local_datetime,
        datetime_semantics=datetime_semantics,
        recurrence_type=recurrence_type.strip(),
        recurrence_interval=recurrence_interval,
        recurrence_day_of_month=recurrence_day_of_month,
        mode=mode.strip(),
        point_codes=tuple(point_code.strip() for point_code in point_codes),
    )


def load_manifest(path: Path) -> BenchmarkManifest:
    """Load and validate a benchmark manifest without touching audio files."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchmarkInputError("manifest не найден") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkInputError("manifest нельзя прочитать как UTF-8 JSON") from exc

    if not isinstance(raw, dict):
        raise BenchmarkInputError("корень manifest должен быть JSON-объектом")
    version = raw.get("version", 1)
    if not isinstance(version, int) or version != 1:
        raise BenchmarkInputError("поддерживается только manifest version=1")

    default_timezone = raw.get("timezone", "Europe/Moscow")
    if not isinstance(default_timezone, str) or not default_timezone.strip():
        raise BenchmarkInputError("timezone должен быть непустой строкой")
    try:
        default_timezone = validate_timezone(default_timezone.strip())
    except ValueError as exc:
        raise BenchmarkInputError("manifest содержит неизвестный timezone") from exc

    default_now = raw.get("now_local")
    if default_now is not None:
        parsed_default_now = _parse_iso_datetime(default_now, "now_local")
    else:
        parsed_default_now = None

    raw_samples = raw.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise BenchmarkInputError("samples должен быть непустым списком")

    samples: list[BenchmarkSample] = []
    for index, raw_sample in enumerate(raw_samples, start=1):
        field = f"samples[{index}]"
        if not isinstance(raw_sample, dict):
            raise BenchmarkInputError(f"{field} должен быть объектом")

        file_value = raw_sample.get("file")
        expected = raw_sample.get("expected")
        tags = raw_sample.get("tags", [])
        timezone = raw_sample.get("timezone", default_timezone)
        now_value = raw_sample.get("now_local", parsed_default_now)
        if not isinstance(file_value, str) or not file_value.strip():
            raise BenchmarkInputError(f"{field}.file должен быть непустой строкой")
        if not isinstance(expected, str) or not expected.strip():
            raise BenchmarkInputError(f"{field}.expected должен быть непустой строкой")
        if not isinstance(tags, list) or not all(
            isinstance(tag, str) and tag.strip() for tag in tags
        ):
            raise BenchmarkInputError(f"{field}.tags должен быть списком непустых строк")
        if not isinstance(timezone, str) or not timezone.strip():
            raise BenchmarkInputError(f"{field}.timezone должен быть непустой строкой")
        try:
            timezone = validate_timezone(timezone.strip())
        except ValueError as exc:
            raise BenchmarkInputError(f"{field}.timezone неизвестен") from exc
        if now_value is None:
            raise BenchmarkInputError(f"{field}.now_local обязателен")
        parsed_now_local = (
            now_value
            if isinstance(now_value, datetime)
            else _parse_iso_datetime(now_value, f"{field}.now_local")
        )
        now_local = _bind_datetime_to_timezone(
            parsed_now_local,
            timezone,
            f"{field}.now_local",
        )

        explicit_product = raw_sample.get("expected_product")
        expected_product = (
            None
            if explicit_product is None
            else _parse_expected_product(
                explicit_product,
                f"{field}.expected_product",
                timezone=timezone,
            )
        )
        samples.append(
            BenchmarkSample(
                file=file_value.strip(),
                expected=expected.strip(),
                tags=tuple(tag.strip() for tag in tags),
                now_local=now_local,
                timezone=timezone,
                expected_product=expected_product,
            )
        )

    return BenchmarkManifest(version=version, samples=tuple(samples))


def _resolve_sample_path(
    samples_dir: Path,
    sample: BenchmarkSample,
    *,
    max_file_size_bytes: int | None = None,
) -> Path:
    base = samples_dir.expanduser().resolve()
    raw_path = Path(sample.file)
    if raw_path.is_absolute():
        raise BenchmarkInputError(f"sample path должен быть относительным: {sample.file}")
    candidate = (base / raw_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise BenchmarkInputError(f"sample path выходит за samples-dir: {sample.file}") from exc
    if candidate.suffix.casefold() not in ALLOWED_AUDIO_SUFFIXES:
        raise BenchmarkInputError(f"неподдерживаемый audio suffix: {sample.file}")
    if not candidate.is_file():
        raise BenchmarkInputError(f"sample audio не найден: {sample.file}")
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise BenchmarkInputError(f"sample audio недоступен: {sample.file}") from exc
    if size <= 0:
        raise BenchmarkInputError(f"sample audio пустой: {sample.file}")
    if max_file_size_bytes is not None and size > max_file_size_bytes:
        raise BenchmarkInputError(f"sample audio слишком большой: {sample.file}")
    return candidate


def _resolve_model_path(model: Path) -> Path:
    candidate = model.expanduser().resolve()
    if not candidate.is_file():
        raise BenchmarkInputError(f"model не найден: {model}")
    try:
        if candidate.stat().st_size <= 0:
            raise BenchmarkInputError(f"model пустой: {model}")
    except OSError as exc:
        raise BenchmarkInputError(f"model недоступен: {model}") from exc
    return candidate


def validate_benchmark_inputs(
    manifest: BenchmarkManifest,
    *,
    samples_dir: Path,
    models: Sequence[Path],
    max_file_size_bytes: int | None = None,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    if not models:
        raise BenchmarkInputError("нужно указать хотя бы одну --model")
    resolved_models = tuple(_resolve_model_path(model) for model in models)
    resolved_samples = tuple(
        _resolve_sample_path(
            samples_dir,
            sample,
            max_file_size_bytes=max_file_size_bytes,
        )
        for sample in manifest.samples
    )
    return resolved_models, resolved_samples


def _normalize_metric_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    chars = [" " if unicodedata.category(char).startswith("P") else char for char in normalized]
    return " ".join("".join(chars).split())


def _normalize_body(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    normalized = " ".join(normalized.split())
    while normalized and unicodedata.category(normalized[-1]).startswith("P"):
        normalized = normalized[:-1].rstrip()
    return normalized


def _edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for ref_index, ref_value in enumerate(reference, start=1):
        current = [ref_index]
        for hyp_index, hyp_value in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[hyp_index] + 1,
                    previous[hyp_index - 1] + (ref_value != hyp_value),
                )
            )
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str) -> float:
    reference_words = _normalize_metric_text(reference).split()
    hypothesis_words = _normalize_metric_text(hypothesis).split()
    denominator = max(len(reference_words), 1)
    return _edit_distance(reference_words, hypothesis_words) / denominator


def character_error_rate(reference: str, hypothesis: str) -> float:
    reference_chars = list(_normalize_metric_text(reference))
    hypothesis_chars = list(_normalize_metric_text(hypothesis))
    denominator = max(len(reference_chars), 1)
    return _edit_distance(reference_chars, hypothesis_chars) / denominator


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Write a JSON report, creating only the requested output parents."""

    try:
        output_path = path.expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise BenchmarkOutputError from exc


def _datetime_key(value: datetime) -> str:
    if value.tzinfo is None:
        return value.isoformat()
    return value.astimezone(UTC).isoformat()


def _parsed_kind(parsed: Any) -> str:
    if isinstance(parsed, DeadlineRequest):
        return "deadline"
    if isinstance(parsed, ParsedReminder):
        return "parsed"
    if isinstance(parsed, ClarificationRequest):
        return "clarification"
    if parsed is None:
        return "unparsed"
    return "unparsed"


def _schedule_signature(parsed: ParsedValue) -> dict[str, Any]:
    return {
        "local_datetime": _datetime_key(parsed.local_dt),
        "datetime_semantics": parsed.datetime_semantics,
        "recurrence_type": getattr(parsed, "recurrence_type", "none"),
        "recurrence_interval": getattr(parsed, "recurrence_interval", 1),
        "recurrence_day_of_month": getattr(parsed, "recurrence_day_of_month", None),
        "mode": parsed.mode,
        "point_codes": list(getattr(parsed, "point_codes", ())),
    }


def _expected_schedule_signature(expected: ExpectedProduct) -> dict[str, Any]:
    if expected.local_datetime is None or expected.datetime_semantics is None:
        raise BenchmarkInputError("parsed expected_product has no schedule")
    return {
        "local_datetime": _datetime_key(expected.local_datetime),
        "datetime_semantics": expected.datetime_semantics,
        "recurrence_type": expected.recurrence_type,
        "recurrence_interval": expected.recurrence_interval,
        "recurrence_day_of_month": expected.recurrence_day_of_month,
        "mode": expected.mode,
        "point_codes": list(expected.point_codes),
    }


def product_expectation_from_transcript(
    transcript: str,
    *,
    now_local: datetime,
) -> ExpectedProduct:
    """Derive a fallback expectation from the current parser.

    Real-user manifests should provide explicit ``expected_product`` values so
    parser regressions are not hidden by deriving ground truth from the parser.
    """

    _, parsed = parse_voice_transcript(transcript, now_local=now_local)
    kind = _parsed_kind(parsed)
    if not isinstance(parsed, (ParsedReminder, DeadlineRequest)):
        return ExpectedProduct(kind=kind)  # type: ignore[arg-type]
    return ExpectedProduct(
        kind=kind,  # type: ignore[arg-type]
        body=parsed.text,
        local_datetime=parsed.local_dt,
        datetime_semantics=parsed.datetime_semantics,
        recurrence_type=getattr(parsed, "recurrence_type", "none"),
        recurrence_interval=getattr(parsed, "recurrence_interval", 1),
        recurrence_day_of_month=getattr(parsed, "recurrence_day_of_month", None),
        mode=parsed.mode,
        point_codes=tuple(getattr(parsed, "point_codes", ())),
    )


def score_product(
    expected: ExpectedProduct,
    actual: ParsedReminder | DeadlineRequest | ClarificationRequest | None,
    *,
    actual_error: bool = False,
) -> dict[str, Any]:
    actual_kind = "stt_error" if actual_error else _parsed_kind(actual)
    parser_success = isinstance(actual, (ParsedReminder, DeadlineRequest)) and not actual_error
    schedule_correct: bool | None = None
    body_correct: bool | None = None
    fully_correct: bool | None = None

    if expected.kind in {"parsed", "deadline"}:
        if isinstance(actual, (ParsedReminder, DeadlineRequest)) and not actual_error:
            schedule_correct = _schedule_signature(actual) == _expected_schedule_signature(expected)
            body_correct = _normalize_body(actual.text) == _normalize_body(expected.body or "")
            fully_correct = schedule_correct and body_correct
        else:
            schedule_correct = False
            body_correct = False
            fully_correct = False

    if actual_error:
        interpretation_correct = False
    elif expected.kind in {"parsed", "deadline"}:
        interpretation_correct = bool(fully_correct)
    else:
        interpretation_correct = actual_kind == expected.kind

    safety_fail_closed = expected.kind in {"clarification", "unparsed"} and not parser_success
    return {
        "expected_kind": expected.kind,
        "actual_kind": actual_kind,
        "parser_success": parser_success,
        "schedule_correct": schedule_correct,
        "body_correct": body_correct,
        "fully_correct": fully_correct,
        "interpretation_correct": interpretation_correct,
        "safety_fail_closed": safety_fail_closed,
    }


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


def _latency_stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean_ms": None, "median_ms": None, "p95_ms": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean_ms": sum(ordered) / len(ordered),
        "median_ms": ordered[len(ordered) // 2]
        if len(ordered) % 2
        else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2,
        "p95_ms": _percentile(ordered, 0.95),
    }


def _seconds_stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "mean_seconds": None,
            "median_seconds": None,
            "p95_seconds": None,
        }
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean_seconds": sum(ordered) / len(ordered),
        "median_seconds": ordered[len(ordered) // 2]
        if len(ordered) % 2
        else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2,
        "p95_seconds": _percentile(ordered, 0.95),
    }


def _ratio_stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean_ratio": None, "median_ratio": None, "p95_ratio": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean_ratio": sum(ordered) / len(ordered),
        "median_ratio": ordered[len(ordered) // 2]
        if len(ordered) % 2
        else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2,
        "p95_ratio": _percentile(ordered, 0.95),
    }


def _percent_stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "mean_percent": None,
            "median_percent": None,
            "p95_percent": None,
        }
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean_percent": sum(ordered) / len(ordered),
        "median_percent": ordered[len(ordered) // 2]
        if len(ordered) % 2
        else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2,
        "p95_percent": _percentile(ordered, 0.95),
    }


def _mean_stats(values: Sequence[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "mean": sum(values) / len(values) if values else None,
    }


def aggregate_results(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate quality, product, error, latency, RSS, and CPU metrics by model."""

    by_model: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_model.setdefault(str(record["model_path"]), []).append(record)

    def summarize(model_records: Sequence[dict[str, Any]]) -> dict[str, Any]:
        total = len(model_records)
        parser_successes = sum(
            bool(record["product"]["parser_success"]) for record in model_records
        )
        expected_parsed = [
            record
            for record in model_records
            if record["product"]["expected_kind"] in {"parsed", "deadline"}
        ]
        expected_nonparsed = [
            record
            for record in model_records
            if record["product"]["expected_kind"] in {"clarification", "unparsed"}
        ]
        fully_correct = sum(
            record["product"]["fully_correct"] is True for record in expected_parsed
        )
        schedule_correct = sum(
            record["product"]["schedule_correct"] is True for record in expected_parsed
        )
        body_correct = sum(record["product"]["body_correct"] is True for record in expected_parsed)
        outcome_matches = sum(
            bool(record["product"]["interpretation_correct"]) for record in model_records
        )
        safety_closed = sum(
            bool(record["product"]["safety_fail_closed"]) for record in expected_nonparsed
        )
        errors = [record for record in model_records if record["status"] == "error"]
        error_counts: dict[str, int] = {}
        for record in errors:
            category = str(record["error"]["category"])
            error_counts[category] = error_counts.get(category, 0) + 1
        latencies = [
            float(record["stt_latency_ms"])
            for record in model_records
            if record["stt_latency_ms"] is not None
        ]
        wers = [float(record["wer"]) for record in model_records if record.get("wer") is not None]
        cers = [float(record["cer"]) for record in model_records if record.get("cer") is not None]
        rss_values = [
            int(record["peak_rss_bytes"])
            for record in model_records
            if record["peak_rss_bytes"] is not None
        ]
        process_wall_values = [
            float(record["stt_process_wall_seconds"])
            for record in model_records
            if record.get("stt_process_wall_seconds") is not None
        ]
        cpu_user_values = [
            float(record["stt_cpu_user_seconds"])
            for record in model_records
            if record.get("stt_cpu_user_seconds") is not None
        ]
        cpu_system_values = [
            float(record["stt_cpu_system_seconds"])
            for record in model_records
            if record.get("stt_cpu_system_seconds") is not None
        ]
        cpu_total_values = [
            float(record["stt_cpu_total_seconds"])
            for record in model_records
            if record.get("stt_cpu_total_seconds") is not None
        ]
        cpu_ratio_values = [
            float(record["cpu_time_wall_ratio"])
            for record in model_records
            if record.get("cpu_time_wall_ratio") is not None
        ]
        cpu_mean_utilization_values = [
            float(record["cpu_utilization_mean_percent"])
            for record in model_records
            if record.get("cpu_utilization_mean_percent") is not None
        ]
        cpu_peak_utilization_values = [
            float(record["cpu_utilization_peak_percent"])
            for record in model_records
            if record.get("cpu_utilization_peak_percent") is not None
        ]

        def rate(numerator: int, denominator: int) -> float | None:
            return numerator / denominator if denominator else None

        return {
            "samples": total,
            "parser_success_rate": rate(parser_successes, total),
            "expected_outcome_match_rate": rate(outcome_matches, total),
            "expected_parsed_samples": len(expected_parsed),
            "schedule_correct_rate": rate(schedule_correct, len(expected_parsed)),
            "body_correct_rate": rate(body_correct, len(expected_parsed)),
            "fully_correct_reminder_interpretation_rate": rate(fully_correct, len(expected_parsed)),
            "expected_nonparsed_samples": len(expected_nonparsed),
            "safety_fail_closed_rate": rate(safety_closed, len(expected_nonparsed)),
            "timeout_count": error_counts.get("timeout", 0),
            "crash_count": error_counts.get("crashed", 0),
            "error_count": len(errors),
            "error_counts": error_counts,
            "wer": _mean_stats(wers),
            "cer": _mean_stats(cers),
            "stt_latency_ms": _latency_stats(latencies),
            "stt_process_wall_seconds": _seconds_stats(process_wall_values),
            "stt_cpu_user_seconds": _seconds_stats(cpu_user_values),
            "stt_cpu_system_seconds": _seconds_stats(cpu_system_values),
            "stt_cpu_total_seconds": _seconds_stats(cpu_total_values),
            "cpu_time_wall_ratio": _ratio_stats(cpu_ratio_values),
            "cpu_utilization_mean_percent": _percent_stats(cpu_mean_utilization_values),
            "cpu_utilization_peak_percent": _percent_stats(cpu_peak_utilization_values),
            "peak_rss_bytes_max": max(rss_values) if rss_values else None,
            "peak_rss_measured_count": len(rss_values),
            "cpu_measured_count": len(cpu_total_values),
        }

    return {
        "by_model": {model: summarize(model_records) for model, model_records in by_model.items()}
    }


def _parse_proc_stat_cpu_ticks(value: str) -> tuple[int, int]:
    """Parse Linux /proc/<pid>/stat utime and stime fields.

    The command name is wrapped in parentheses and may itself contain spaces
    or parentheses, so splitting the complete line is not safe.
    """

    try:
        fields_after_comm = value.rsplit(") ", 1)[1].split()
        user_ticks = int(fields_after_comm[11])
        system_ticks = int(fields_after_comm[12])
    except (IndexError, ValueError) as exc:
        raise ValueError("invalid /proc stat CPU fields") from exc
    return user_ticks, system_ticks


def _read_proc_cpu_ticks(pid: int) -> tuple[int, int] | None:
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        return None
    try:
        value = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
        return _parse_proc_stat_cpu_ticks(value)
    except (OSError, UnicodeError, ValueError):
        return None


def _proc_clock_ticks_per_second() -> int | None:
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        return None
    try:
        value = int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, OSError, ValueError):
        return None
    return value if value > 0 else None


@dataclass(frozen=True, slots=True)
class _ProcessMeasurement:
    process_wall_seconds: float | None
    cpu_user_seconds: float | None
    cpu_system_seconds: float | None
    cpu_total_seconds: float | None
    cpu_time_wall_ratio: float | None
    cpu_utilization_mean_percent: float | None
    cpu_utilization_peak_percent: float | None
    peak_rss_bytes: int | None
    cpu_method: str
    rss_method: str

    def as_record(self) -> dict[str, Any]:
        return {
            "stt_process_wall_seconds": self.process_wall_seconds,
            "stt_process_wall_ms": (
                self.process_wall_seconds * 1000 if self.process_wall_seconds is not None else None
            ),
            "stt_cpu_user_seconds": self.cpu_user_seconds,
            "stt_cpu_system_seconds": self.cpu_system_seconds,
            "stt_cpu_total_seconds": self.cpu_total_seconds,
            "cpu_time_wall_ratio": self.cpu_time_wall_ratio,
            "cpu_utilization_mean_percent": self.cpu_utilization_mean_percent,
            "cpu_utilization_peak_percent": self.cpu_utilization_peak_percent,
            "peak_rss_bytes": self.peak_rss_bytes,
            "cpu_method": self.cpu_method,
            "rss_method": self.rss_method,
        }


class _ProcessResourceMonitor:
    """Best-effort Linux /proc measurement for one whisper-cli process.

    CPU ticks are sampled while the process runs. The wait wrapper samples
    before reaping the child, and every metric retains the last valid sample if
    /proc disappears immediately after process termination.
    """

    def __init__(
        self,
        *,
        measure_rss: bool,
        clock_ticks_per_second: int | None = None,
        procfs_available: bool | None = None,
    ) -> None:
        self._measure_rss = measure_rss
        if procfs_available is None:
            procfs_available = sys.platform.startswith("linux") and Path("/proc").is_dir()
        self._clock_ticks_per_second = (
            clock_ticks_per_second if procfs_available else None
        ) or _proc_clock_ticks_per_second()
        if procfs_available and self._clock_ticks_per_second:
            self.cpu_method = "linux_proc_stat_poll"
        else:
            self.cpu_method = "unavailable"
        self.rss_method = (
            "linux_proc_vmhwm_poll"
            if measure_rss and procfs_available
            else "disabled"
            if not measure_rss
            else "unavailable"
        )
        self.peak_rss_bytes: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._last_observation_at: float | None = None
        self._first_cpu_ticks: tuple[int, int] | None = None
        self._last_cpu_ticks: tuple[int, int] | None = None
        self._last_cpu_sample_at: float | None = None
        self._utilization_cpu_seconds = 0.0
        self._utilization_wall_seconds = 0.0
        self._utilization_percent: list[float] = []

    @property
    def method(self) -> str:
        """Compatibility alias for the pre-CPU RSS-only monitor."""

        return self.rss_method

    def attach(
        self,
        process: asyncio.subprocess.Process,
        *,
        started_at: float | None = None,
        start_sampling: bool = True,
    ) -> None:
        self._process = process
        self._started_at = time.monotonic() if started_at is None else started_at
        self.sample(now=self._started_at)
        self._wrap_wait(process)
        if start_sampling and (
            self.cpu_method != "unavailable" or self.rss_method != "unavailable"
        ):
            self._task = asyncio.create_task(self._sample())

    @staticmethod
    def _read_rss_bytes(pid: int) -> int | None:
        try:
            status = (Path("/proc") / str(pid) / "status").read_text(encoding="ascii")
        except (OSError, UnicodeError):
            return None
        fallback: int | None = None
        for line in status.splitlines():
            if not (line.startswith("VmHWM:") or line.startswith("VmRSS:")):
                continue
            parts = line.split()
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            value = int(parts[1]) * 1024
            if line.startswith("VmHWM:"):
                return value
            fallback = value
        return fallback

    def _wrap_wait(self, process: asyncio.subprocess.Process) -> None:
        original_wait = process.wait

        async def monitored_wait(*args: Any, **kwargs: Any) -> int:
            self.sample()
            try:
                return await original_wait(*args, **kwargs)
            finally:
                finished_at = time.monotonic()
                self.sample(now=finished_at)
                self._finished_at = finished_at

        try:
            process.wait = monitored_wait  # type: ignore[method-assign]
        except (AttributeError, TypeError):
            # The polling task remains useful for process implementations that
            # do not allow instance method replacement.
            return

    def sample(self, *, now: float | None = None) -> None:
        if self._process is None:
            return
        observation_at = time.monotonic() if now is None else now
        self._last_observation_at = observation_at
        if self.cpu_method != "unavailable":
            cpu_ticks = _read_proc_cpu_ticks(self._process.pid)
            if cpu_ticks is not None:
                self._record_cpu_sample(observation_at, cpu_ticks)
        if self.rss_method != "unavailable":
            rss = self._read_rss_bytes(self._process.pid)
            if rss is not None:
                self.peak_rss_bytes = max(self.peak_rss_bytes or 0, rss)

    def _record_cpu_sample(self, observed_at: float, cpu_ticks: tuple[int, int]) -> None:
        if self._first_cpu_ticks is None:
            self._first_cpu_ticks = cpu_ticks
            self._last_cpu_ticks = cpu_ticks
            self._last_cpu_sample_at = observed_at
            return
        if self._last_cpu_ticks is None or self._last_cpu_sample_at is None:
            return
        if any(
            current < previous
            for current, previous in zip(cpu_ticks, self._last_cpu_ticks, strict=True)
        ):
            return
        elapsed = observed_at - self._last_cpu_sample_at
        if elapsed >= PROCESS_RESOURCE_SAMPLE_INTERVAL_SECONDS and self._clock_ticks_per_second:
            delta_ticks = sum(cpu_ticks) - sum(self._last_cpu_ticks)
            delta_cpu_seconds = delta_ticks / self._clock_ticks_per_second
            self._utilization_cpu_seconds += delta_cpu_seconds
            self._utilization_wall_seconds += elapsed
            self._utilization_percent.append(delta_cpu_seconds / elapsed * 100)
        self._last_cpu_ticks = cpu_ticks
        self._last_cpu_sample_at = observed_at

    async def _sample(self) -> None:
        if self._process is None:
            return
        try:
            while True:
                self.sample()
                if self._process.returncode is not None:
                    return
                await asyncio.sleep(PROCESS_RESOURCE_SAMPLE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    async def stop(self) -> None:
        if self._process is not None:
            self.sample()
            if self._finished_at is None and self._process.returncode is not None:
                self._finished_at = time.monotonic()
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def snapshot(self) -> _ProcessMeasurement:
        process_wall_seconds: float | None = None
        if self._started_at is not None:
            end = self._finished_at or self._last_observation_at
            if end is not None:
                process_wall_seconds = max(0.0, end - self._started_at)

        cpu_user_seconds: float | None = None
        cpu_system_seconds: float | None = None
        cpu_total_seconds: float | None = None
        if (
            self._first_cpu_ticks is not None
            and self._last_cpu_ticks is not None
            and self._clock_ticks_per_second
        ):
            user_ticks = self._last_cpu_ticks[0] - self._first_cpu_ticks[0]
            system_ticks = self._last_cpu_ticks[1] - self._first_cpu_ticks[1]
            if user_ticks >= 0 and system_ticks >= 0:
                cpu_user_seconds = user_ticks / self._clock_ticks_per_second
                cpu_system_seconds = system_ticks / self._clock_ticks_per_second
                cpu_total_seconds = cpu_user_seconds + cpu_system_seconds

        cpu_time_wall_ratio = None
        if cpu_total_seconds is not None and process_wall_seconds and process_wall_seconds > 0:
            cpu_time_wall_ratio = cpu_total_seconds / process_wall_seconds
        mean_utilization = None
        peak_utilization = None
        if self._utilization_wall_seconds > 0:
            mean_utilization = self._utilization_cpu_seconds / self._utilization_wall_seconds * 100
            peak_utilization = max(self._utilization_percent)

        return _ProcessMeasurement(
            process_wall_seconds=process_wall_seconds,
            cpu_user_seconds=cpu_user_seconds,
            cpu_system_seconds=cpu_system_seconds,
            cpu_total_seconds=cpu_total_seconds,
            cpu_time_wall_ratio=cpu_time_wall_ratio,
            cpu_utilization_mean_percent=mean_utilization,
            cpu_utilization_peak_percent=peak_utilization,
            peak_rss_bytes=self.peak_rss_bytes,
            cpu_method=self.cpu_method,
            rss_method=self.rss_method,
        )


class _MeasuredTranscriptionError(RuntimeError):
    def __init__(self, cause: Exception, measurement: _ProcessMeasurement) -> None:
        super().__init__("transcription failed after process measurement")
        self.cause = cause
        self.measurement = measurement


async def _transcribe_with_measurement(
    provider: SpeechToTextProvider,
    audio_path: Path,
    *,
    measure_rss: bool,
) -> tuple[str, _ProcessMeasurement]:
    monitor = _ProcessResourceMonitor(measure_rss=measure_rss)
    original_create = asyncio.create_subprocess_exec

    async def monitored_create(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original_create(*args, **kwargs)
        monitor.attach(process)
        return process

    with patch.object(asyncio, "create_subprocess_exec", new=monitored_create):
        transcript: str | None = None
        captured_error: Exception | None = None
        try:
            transcript = await provider.transcribe(audio_path)
        except Exception as exc:
            captured_error = exc
        finally:
            await monitor.stop()
    measurement = monitor.snapshot()
    if captured_error is not None:
        raise _MeasuredTranscriptionError(captured_error, measurement) from captured_error
    if transcript is None:  # pragma: no cover - provider protocol violation
        raise _MeasuredTranscriptionError(
            RuntimeError("provider returned no transcript"),
            measurement,
        )
    return transcript, measurement


def _default_provider(model_path: Path, config: BenchmarkConfig) -> WhisperCppSpeechToTextProvider:
    return WhisperCppSpeechToTextProvider(
        command=config.command,
        model_path=str(model_path),
        language=config.language,
        threads=config.threads,
        timeout_seconds=config.timeout_seconds,
    )


def _record_base(
    sample: BenchmarkSample,
    model_path: Path,
    *,
    normalization_latency_ms: float,
) -> dict[str, Any]:
    explicit_product = sample.expected_product is not None
    expected_product = sample.expected_product or product_expectation_from_transcript(
        sample.expected,
        now_local=sample.now_local,
    )
    return {
        "sample": sample.file,
        "tags": list(sample.tags),
        "expected": sample.expected,
        "expected_product_source": "manifest" if explicit_product else "parser-derived",
        "model": model_path.name,
        "model_path": str(model_path),
        "model_size_bytes": model_path.stat().st_size,
        "normalization_latency_ms": normalization_latency_ms,
        "stt_latency_ms": None,
        "stt_process_wall_seconds": None,
        "stt_process_wall_ms": None,
        "stt_cpu_user_seconds": None,
        "stt_cpu_system_seconds": None,
        "stt_cpu_total_seconds": None,
        "cpu_time_wall_ratio": None,
        "cpu_utilization_mean_percent": None,
        "cpu_utilization_peak_percent": None,
        "peak_rss_bytes": None,
        "cpu_method": "unavailable",
        "rss_method": "disabled",
        "status": "error",
        "error": None,
        "transcript": None,
        "normalized_expected": _normalize_metric_text(sample.expected),
        "normalized_transcript": None,
        "normalized_exact_match": False,
        "wer": None,
        "cer": None,
        "product": score_product(expected_product, None, actual_error=True),
    }


async def run_benchmark(
    manifest: BenchmarkManifest,
    *,
    samples_dir: Path,
    models: Sequence[Path],
    config: BenchmarkConfig,
    provider_factory: ProviderFactory = _default_provider,
    converter: Converter = convert_voice_to_wav,
) -> dict[str, Any]:
    resolved_models, resolved_samples = validate_benchmark_inputs(
        manifest,
        samples_dir=samples_dir,
        models=models,
        max_file_size_bytes=config.max_file_size_bytes,
    )
    records: list[dict[str, Any]] = []
    limits = VoiceMediaLimits(
        max_file_size_bytes=config.max_file_size_bytes,
        max_duration_seconds=config.max_duration_seconds,
        download_timeout_seconds=config.conversion_timeout_seconds,
        conversion_timeout_seconds=config.conversion_timeout_seconds,
        conversion_command=config.conversion_command,
    )

    for sample, source_path in zip(manifest.samples, resolved_samples, strict=True):
        with TemporaryDirectory(prefix="reminder-bot-voice-benchmark-") as temp_dir:
            normalized_path = Path(temp_dir) / "normalized.wav"
            started = time.perf_counter()
            conversion_error: dict[str, str] | None = None
            try:
                await converter(source_path, normalized_path, limits)
            except VoiceMediaError as exc:
                conversion_error = {"stage": "conversion", "category": exc.category}
            except Exception as exc:  # pragma: no cover - defensive runtime shield
                conversion_error = {
                    "stage": "conversion",
                    "category": "conversion_error",
                    "type": type(exc).__name__,
                }
            normalization_latency_ms = (time.perf_counter() - started) * 1000

            for model_path in resolved_models:
                record = _record_base(
                    sample,
                    model_path,
                    normalization_latency_ms=normalization_latency_ms,
                )
                if conversion_error is not None:
                    record["error"] = conversion_error
                    records.append(record)
                    continue

                provider = provider_factory(model_path, config)
                stt_started = time.perf_counter()
                try:
                    transcript, measurement = await _transcribe_with_measurement(
                        provider,
                        normalized_path,
                        measure_rss=config.measure_rss,
                    )
                    stt_latency_ms = (time.perf_counter() - stt_started) * 1000
                    normalized_transcript = normalize_transcript(transcript)
                    _, parsed = parse_voice_transcript(
                        normalized_transcript,
                        now_local=sample.now_local,
                    )
                    expected_product = (
                        sample.expected_product
                        or product_expectation_from_transcript(
                            sample.expected,
                            now_local=sample.now_local,
                        )
                    )
                    record.update(
                        {
                            "status": "ok",
                            "stt_latency_ms": stt_latency_ms,
                            "transcript": normalized_transcript,
                            "normalized_transcript": _normalize_metric_text(normalized_transcript),
                            "normalized_exact_match": _normalize_metric_text(sample.expected)
                            == _normalize_metric_text(normalized_transcript),
                            "wer": word_error_rate(sample.expected, normalized_transcript),
                            "cer": character_error_rate(sample.expected, normalized_transcript),
                            "product": score_product(expected_product, parsed),
                        }
                    )
                    record.update(measurement.as_record())
                except _MeasuredTranscriptionError as measured_exc:
                    record.update(measured_exc.measurement.as_record())
                    cause = measured_exc.cause
                    if isinstance(cause, SpeechToTextError):
                        record["error"] = {"stage": "stt", "category": cause.category}
                    else:
                        record["error"] = {
                            "stage": "stt",
                            "category": "provider_error",
                            "type": type(cause).__name__,
                        }
                except SpeechToTextError as exc:
                    record["error"] = {"stage": "stt", "category": exc.category}
                except Exception as exc:  # pragma: no cover - defensive runtime shield
                    record["error"] = {
                        "stage": "stt",
                        "category": "provider_error",
                        "type": type(exc).__name__,
                    }
                finally:
                    if record["stt_latency_ms"] is None and stt_started:
                        record["stt_latency_ms"] = (time.perf_counter() - stt_started) * 1000
                records.append(record)

    summary = aggregate_results(records)
    return {
        "manifest_version": manifest.version,
        "settings": {
            "language": config.language,
            "threads": config.threads,
            "timeout_seconds": config.timeout_seconds,
            "conversion_timeout_seconds": config.conversion_timeout_seconds,
            "conversion_command": config.conversion_command,
            "measure_rss": config.measure_rss,
            "measure_cpu": True,
        },
        "records": records,
        "summary": summary,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline benchmark for the local Russian voice STT pipeline."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--samples-dir",
        type=Path,
        help="root for relative sample file paths; defaults to manifest directory",
    )
    parser.add_argument("--model", dest="models", type=Path, action="append", required=True)
    parser.add_argument("--command", default="whisper-cli", help="whisper-cli command/argv")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--conversion-timeout-seconds",
        type=int,
        default=DEFAULT_CONVERSION_TIMEOUT_SECONDS,
    )
    parser.add_argument("--conversion-command", default="ffmpeg")
    parser.add_argument("--max-file-size-bytes", type=int, default=DEFAULT_MAX_FILE_SIZE_BYTES)
    parser.add_argument("--max-duration-seconds", type=int, default=DEFAULT_MAX_DURATION_SECONDS)
    parser.add_argument(
        "--measure-rss",
        action="store_true",
        help="best-effort Linux /proc high-water RSS sampling for whisper-cli",
    )
    parser.add_argument("--output", type=Path, help="write the JSON report to this path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    manifest_path = args.manifest.expanduser().resolve()
    samples_dir = (args.samples_dir or manifest_path.parent).expanduser().resolve()
    try:
        manifest = load_manifest(manifest_path)
        config = BenchmarkConfig(
            command=args.command,
            language=args.language,
            threads=args.threads,
            timeout_seconds=args.timeout_seconds,
            conversion_timeout_seconds=args.conversion_timeout_seconds,
            conversion_command=args.conversion_command,
            max_file_size_bytes=args.max_file_size_bytes,
            max_duration_seconds=args.max_duration_seconds,
            measure_rss=args.measure_rss,
        )
        report = asyncio.run(
            run_benchmark(
                manifest,
                samples_dir=samples_dir,
                models=args.models,
                config=config,
            )
        )
    except BenchmarkInputError as exc:
        print(f"benchmark input error: {exc}", file=sys.stderr)
        return 2

    if args.output is None:
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        print(rendered, end="")
    else:
        try:
            write_report(args.output, report)
        except BenchmarkOutputError:
            print("benchmark output cannot be written", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
