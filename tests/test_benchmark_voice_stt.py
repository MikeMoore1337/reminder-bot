import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import scripts.benchmark_voice_stt as benchmark_voice_stt
from app.services.reminder_parser import ClarificationRequest, ParsedReminder
from app.services.speech_to_text import SpeechToTextError
from app.services.voice_transcript import parse_voice_transcript
from scripts.benchmark_voice_stt import (
    BenchmarkConfig,
    BenchmarkInputError,
    BenchmarkManifest,
    BenchmarkOutputError,
    BenchmarkSample,
    ExpectedProduct,
    aggregate_results,
    character_error_rate,
    load_manifest,
    main,
    run_benchmark,
    score_product,
    validate_benchmark_inputs,
    word_error_rate,
    write_report,
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


def test_load_manifest_binds_helsinki_dst_datetimes_and_scores_parser_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "helsinki.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "timezone": "Europe/Helsinki",
                "now_local": "2026-10-24T12:00:00+03:00",
                "samples": [
                    {
                        "file": "dst.ogg",
                        "expected": "Напомни завтра в 9 утра проверить тест",
                        "tags": ["dst"],
                        "expected_product": {
                            "kind": "parsed",
                            "local_datetime": "2026-10-25T09:00:00+02:00",
                            "datetime_semantics": "wall_clock",
                            "body": "утра проверить тест",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manifest = load_manifest(path)
    sample = manifest.samples[0]
    expected_product = sample.expected_product

    assert isinstance(sample.now_local.tzinfo, ZoneInfo)
    assert sample.now_local.tzinfo.key == "Europe/Helsinki"
    assert sample.now_local.utcoffset() == timedelta(hours=3)
    assert expected_product is not None
    assert expected_product.local_datetime is not None
    assert isinstance(expected_product.local_datetime.tzinfo, ZoneInfo)
    assert expected_product.local_datetime.tzinfo.key == "Europe/Helsinki"
    assert expected_product.local_datetime.utcoffset() == timedelta(hours=2)

    _, parsed = parse_voice_transcript(
        sample.expected,
        now_local=sample.now_local,
    )
    assert isinstance(parsed, ParsedReminder)
    assert parsed.local_dt == expected_product.local_datetime
    assert score_product(expected_product, parsed)["schedule_correct"] is True
    assert score_product(expected_product, parsed)["fully_correct"] is True


def test_load_manifest_binds_normal_moscow_datetime_to_zoneinfo(tmp_path: Path) -> None:
    path = tmp_path / "moscow.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "timezone": "Europe/Moscow",
                "now_local": "2026-09-10T12:00:00+03:00",
                "samples": [
                    {
                        "file": "01.ogg",
                        "expected": "Напомни завтра в 9 проверить тест",
                        "expected_product": {
                            "kind": "parsed",
                            "local_datetime": "2026-09-11T09:00:00+03:00",
                            "datetime_semantics": "wall_clock",
                            "body": "проверить тест",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    sample = load_manifest(path).samples[0]

    assert isinstance(sample.now_local.tzinfo, ZoneInfo)
    assert sample.now_local.tzinfo.key == "Europe/Moscow"
    assert sample.now_local.utcoffset() == timedelta(hours=3)
    assert sample.expected_product is not None
    _, parsed = parse_voice_transcript(sample.expected, now_local=sample.now_local)
    assert isinstance(parsed, ParsedReminder)
    assert parsed.local_dt.utcoffset() == timedelta(hours=3)
    assert score_product(sample.expected_product, parsed)["fully_correct"] is True


def test_load_manifest_rejects_incompatible_explicit_timezone_offset(tmp_path: Path) -> None:
    path = tmp_path / "incompatible.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "timezone": "Europe/Helsinki",
                "now_local": "2026-10-24T12:00:00+02:00",
                "samples": [{"file": "01.ogg", "expected": "x"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkInputError, match="offset"):
        load_manifest(path)


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


def test_proc_stat_parser_handles_spaces_in_command_name() -> None:
    fields_after_state = ["0"] * 13
    fields_after_state[10] = "123"
    fields_after_state[11] = "45"

    assert benchmark_voice_stt._parse_proc_stat_cpu_ticks(
        "42 (whisper cli worker) S " + " ".join(fields_after_state)
    ) == (123, 45)


def test_process_resource_monitor_keeps_last_cpu_sample_after_proc_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 42
        returncode = None

        async def wait(self) -> int:
            return 0

    monitor = benchmark_voice_stt._ProcessResourceMonitor(
        measure_rss=False,
        clock_ticks_per_second=100,
        procfs_available=True,
    )
    readings = iter([(100, 50), (140, 70), None])
    monkeypatch.setattr(
        benchmark_voice_stt,
        "_read_proc_cpu_ticks",
        lambda _pid: next(readings),
    )
    monitor.attach(FakeProcess(), started_at=10.0, start_sampling=False)  # type: ignore[arg-type]
    monitor.sample(now=11.0)
    monitor.sample(now=12.0)
    measurement = monitor.snapshot()

    assert measurement.process_wall_seconds == pytest.approx(2.0)
    assert measurement.cpu_user_seconds == pytest.approx(0.4)
    assert measurement.cpu_system_seconds == pytest.approx(0.2)
    assert measurement.cpu_total_seconds == pytest.approx(0.6)
    assert measurement.cpu_time_wall_ratio == pytest.approx(0.3)
    assert measurement.cpu_utilization_mean_percent == pytest.approx(60.0)
    assert measurement.cpu_utilization_peak_percent == pytest.approx(60.0)


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
            "wer": 0.1,
            "cer": 0.05,
            "peak_rss_bytes": 1000,
            "error": None,
        },
        {
            "model_path": "/models/base.bin",
            "status": "error",
            "product": error_product,
            "stt_latency_ms": 200.0,
            "wer": 0.3,
            "cer": 0.15,
            "peak_rss_bytes": None,
            "error": {"category": "timeout"},
        },
    ]

    summary = aggregate_results(records)["by_model"]["/models/base.bin"]

    assert summary["fully_correct_reminder_interpretation_rate"] == pytest.approx(0.5)
    assert summary["parser_success_rate"] == pytest.approx(0.5)
    assert summary["timeout_count"] == 1
    assert summary["wer"]["mean"] == pytest.approx(0.2)
    assert summary["cer"]["mean"] == pytest.approx(0.1)
    assert summary["stt_latency_ms"]["p95_ms"] == 200.0
    assert summary["peak_rss_bytes_max"] == 1000


def test_aggregate_results_reports_process_cpu_statistics() -> None:
    product = {
        "expected_kind": "clarification",
        "parser_success": False,
        "schedule_correct": None,
        "body_correct": None,
        "fully_correct": None,
        "interpretation_correct": True,
        "safety_fail_closed": True,
    }
    records = [
        {
            "model_path": "/models/small-q5_1.bin",
            "status": "ok",
            "product": product,
            "stt_latency_ms": 100.0,
            "stt_process_wall_seconds": 2.0,
            "stt_cpu_user_seconds": 1.0,
            "stt_cpu_system_seconds": 0.2,
            "stt_cpu_total_seconds": 1.2,
            "cpu_time_wall_ratio": 0.6,
            "cpu_utilization_mean_percent": 60.0,
            "cpu_utilization_peak_percent": 80.0,
            "peak_rss_bytes": 1000,
            "error": None,
        },
        {
            "model_path": "/models/small-q5_1.bin",
            "status": "ok",
            "product": product,
            "stt_latency_ms": 200.0,
            "stt_process_wall_seconds": 3.0,
            "stt_cpu_user_seconds": 2.0,
            "stt_cpu_system_seconds": 0.4,
            "stt_cpu_total_seconds": 2.4,
            "cpu_time_wall_ratio": 0.8,
            "cpu_utilization_mean_percent": 80.0,
            "cpu_utilization_peak_percent": 120.0,
            "peak_rss_bytes": 2000,
            "error": None,
        },
    ]

    summary = aggregate_results(records)["by_model"]["/models/small-q5_1.bin"]

    assert summary["stt_cpu_total_seconds"]["mean_seconds"] == pytest.approx(1.8)
    assert summary["stt_cpu_total_seconds"]["p95_seconds"] == pytest.approx(2.4)
    assert summary["cpu_time_wall_ratio"]["mean_ratio"] == pytest.approx(0.7)
    assert summary["cpu_utilization_peak_percent"]["p95_percent"] == pytest.approx(120.0)
    assert summary["cpu_measured_count"] == 2


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


def test_write_report_creates_nested_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "results" / "report.json"

    write_report(output, {"status": "ok", "records": []})

    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "ok", "records": []}


def test_write_report_fails_safely_for_directory_target(tmp_path: Path) -> None:
    output = tmp_path / "existing-directory"
    output.mkdir()

    with pytest.raises(BenchmarkOutputError):
        write_report(output, {"status": "ok"})


def test_main_returns_two_for_unwritable_output_without_leaking_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "timezone": "UTC",
                "now_local": "2026-09-10T12:00:00+00:00",
                "samples": [{"file": "01.ogg", "expected": "x"}],
            }
        ),
        encoding="utf-8",
    )
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")
    output = tmp_path / "existing-directory"
    output.mkdir()

    async def fake_run_benchmark(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "ok"}

    monkeypatch.setattr(benchmark_voice_stt, "run_benchmark", fake_run_benchmark)

    result = main(
        [
            "--manifest",
            str(manifest),
            "--model",
            str(model),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert capsys.readouterr().err.strip() == "benchmark output cannot be written"
