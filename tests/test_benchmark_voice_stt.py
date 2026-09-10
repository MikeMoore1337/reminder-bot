import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.reminder_parser import ClarificationRequest, ParsedReminder
from app.services.speech_to_text import SpeechToTextError
from scripts.benchmark_voice_stt import (
    BenchmarkConfig,
    BenchmarkInputError,
    BenchmarkManifest,
    BenchmarkSample,
    ExpectedProduct,
    aggregate_results,
    character_error_rate,
    load_manifest,
    run_benchmark,
    score_product,
    validate_benchmark_inputs,
    word_error_rate,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _product(*, minutes: int = 2, body: str = "проверить голосовое напоминание") -> ExpectedProduct:
    return ExpectedProduct(
        kind="parsed",
        body=body,
        local_datetime=NOW + timedelta(minutes=minutes),
        datetime_semantics="instant",
    )


def _manifest(sample_file: str = "01.ogg") -> BenchmarkManifest:
    return BenchmarkManifest(
        version=1,
        samples=(
            BenchmarkSample(
                file=sample_file,
                expected="Напомни через две минуты проверить голосовое напоминание",
                tags=("relative", "minutes"),
                now_local=NOW,
                timezone="UTC",
                expected_product=_product(),
            ),
        ),
    )


def test_load_manifest_parses_explicit_product_ground_truth(tmp_path: Path) -> None:
    path = tmp_path / "voice-benchmark.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "timezone": "UTC",
                "now_local": "2026-09-10T12:00:00+00:00",
                "samples": [
                    {
                        "file": "01.ogg",
                        "expected": "Напомни через две минуты проверить голосовое напоминание",
                        "tags": ["relative"],
                        "expected_product": {
                            "kind": "parsed",
                            "local_datetime": "2026-09-10T12:02:00+00:00",
                            "datetime_semantics": "instant",
                            "body": "проверить голосовое напоминание",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manifest = load_manifest(path)

    assert manifest.version == 1
    assert manifest.samples[0].expected_product is not None
    assert manifest.samples[0].expected_product.body == "проверить голосовое напоминание"
    assert manifest.samples[0].now_local == NOW


@pytest.mark.parametrize(
    "payload",
    [
        {"samples": []},
        {"now_local": "not-a-date", "samples": [{"file": "01.ogg", "expected": "x"}]},
        {
            "now_local": "2026-09-10T12:00:00+00:00",
            "samples": [{"file": "../01.ogg", "expected": "x", "tags": [1]}],
        },
    ],
)
def test_load_manifest_rejects_malformed_manifest(tmp_path: Path, payload: dict) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkInputError):
        load_manifest(path)


def test_wer_and_cer_use_deterministic_normalization() -> None:
    assert word_error_rate("Напомни купить молоко.", "напомни купить молоко") == 0
    assert word_error_rate("a b c", "a x c") == pytest.approx(1 / 3)
    assert character_error_rate("Кот.", "кит") == pytest.approx(1 / 3)


def test_product_score_compares_schedule_and_body_against_ground_truth() -> None:
    expected = _product()
    correct = ParsedReminder(
        local_dt=NOW + timedelta(minutes=2),
        text="проверить голосовое напоминание.",
        datetime_semantics="instant",
    )
    wrong = ParsedReminder(
        local_dt=NOW + timedelta(minutes=2),
        text="проверить глазовой напоминания",
        datetime_semantics="instant",
    )

    correct_score = score_product(expected, correct)
    wrong_score = score_product(expected, wrong)

    assert correct_score["schedule_correct"] is True
    assert correct_score["body_correct"] is True
    assert correct_score["fully_correct"] is True
    assert wrong_score["schedule_correct"] is True
    assert wrong_score["body_correct"] is False
    assert wrong_score["fully_correct"] is False


def test_clarification_is_scored_as_fail_closed_product_behavior() -> None:
    expected = ExpectedProduct(kind="clarification")
    actual = ClarificationRequest(kind="missing_schedule", prompt="уточни время", raw_text="x")
    unsafe = ParsedReminder(local_dt=NOW, text="купить молоко")

    safe_score = score_product(expected, actual)
    unsafe_score = score_product(expected, unsafe)

    assert safe_score["interpretation_correct"] is True
    assert safe_score["safety_fail_closed"] is True
    assert unsafe_score["interpretation_correct"] is False
    assert unsafe_score["safety_fail_closed"] is False


def test_aggregate_results_reports_quality_errors_latency_and_rss() -> None:
    product = {
        "expected_kind": "parsed",
        "actual_kind": "parsed",
        "parser_success": True,
        "schedule_correct": True,
        "body_correct": True,
        "fully_correct": True,
        "interpretation_correct": True,
        "safety_fail_closed": False,
    }
    error_product = {
        "expected_kind": "parsed",
        "actual_kind": "stt_error",
        "parser_success": False,
        "schedule_correct": False,
        "body_correct": False,
        "fully_correct": False,
        "interpretation_correct": False,
        "safety_fail_closed": False,
    }
    records = [
        {
            "model_path": "/models/base.bin",
            "status": "ok",
            "product": product,
            "stt_latency_ms": 100.0,
            "peak_rss_bytes": 1000,
            "error": None,
        },
        {
            "model_path": "/models/base.bin",
            "status": "error",
            "product": error_product,
            "stt_latency_ms": 200.0,
            "peak_rss_bytes": None,
            "error": {"category": "timeout"},
        },
    ]

    summary = aggregate_results(records)["by_model"]["/models/base.bin"]

    assert summary["fully_correct_reminder_interpretation_rate"] == pytest.approx(0.5)
    assert summary["parser_success_rate"] == pytest.approx(0.5)
    assert summary["timeout_count"] == 1
    assert summary["stt_latency_ms"]["p95_ms"] == 200.0
    assert summary["peak_rss_bytes_max"] == 1000


def test_validate_inputs_rejects_missing_model_audio_and_path_escape(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "01.ogg").write_bytes(b"ogg")
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")

    resolved_models, resolved_samples = validate_benchmark_inputs(
        _manifest(), samples_dir=audio_dir, models=[model]
    )
    assert resolved_models == (model.resolve(),)
    assert resolved_samples == ((audio_dir / "01.ogg").resolve(),)

    with pytest.raises(BenchmarkInputError, match="model не найден"):
        validate_benchmark_inputs(
            _manifest(), samples_dir=audio_dir, models=[tmp_path / "missing.bin"]
        )
    with pytest.raises(BenchmarkInputError, match="sample audio не найден"):
        validate_benchmark_inputs(_manifest("missing.ogg"), samples_dir=audio_dir, models=[model])
    with pytest.raises(BenchmarkInputError, match="выходит за samples-dir"):
        validate_benchmark_inputs(
            _manifest("../outside.ogg"), samples_dir=audio_dir, models=[model]
        )


def test_run_benchmark_uses_injected_runtime_without_real_model_or_ffmpeg(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "01.ogg").write_bytes(b"ogg")
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")

    async def fake_converter(source: Path, destination: Path, _limits) -> int:
        assert source.name == "01.ogg"
        destination.write_bytes(b"RIFF")
        return destination.stat().st_size

    class FakeProvider:
        async def transcribe(self, audio_path: Path) -> str:
            assert audio_path.name == "normalized.wav"
            return "Напомни через две минуты проверить голосовое напоминание"

    report = asyncio.run(
        run_benchmark(
            _manifest(),
            samples_dir=audio_dir,
            models=[model],
            config=BenchmarkConfig(),
            converter=fake_converter,
            provider_factory=lambda _model, _config: FakeProvider(),
        )
    )

    record = report["records"][0]
    assert record["status"] == "ok"
    assert record["normalized_exact_match"] is True
    assert record["wer"] == 0
    assert record["cer"] == 0
    assert record["product"]["fully_correct"] is True
    assert (
        report["summary"]["by_model"][str(model.resolve())][
            "fully_correct_reminder_interpretation_rate"
        ]
        == 1
    )


def test_run_benchmark_counts_timeout_without_real_runtime(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "01.ogg").write_bytes(b"ogg")
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")

    async def fake_converter(_source: Path, destination: Path, _limits) -> int:
        destination.write_bytes(b"RIFF")
        return destination.stat().st_size

    class TimeoutProvider:
        async def transcribe(self, _audio_path: Path) -> str:
            raise SpeechToTextError("timeout", "safe")

    report = asyncio.run(
        run_benchmark(
            _manifest(),
            samples_dir=audio_dir,
            models=[model],
            config=BenchmarkConfig(),
            converter=fake_converter,
            provider_factory=lambda _model, _config: TimeoutProvider(),
        )
    )

    record = report["records"][0]
    summary = report["summary"]["by_model"][str(model.resolve())]
    assert record["status"] == "error"
    assert record["error"] == {"stage": "stt", "category": "timeout"}
    assert summary["timeout_count"] == 1
    assert summary["fully_correct_reminder_interpretation_rate"] == 0
