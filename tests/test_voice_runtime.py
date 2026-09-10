import io
import wave

import pytest

from app.services import voice_runtime
from app.services.voice_runtime import VoiceRuntimePreflightError


def _wav_bytes(*, channels: int = 1, sample_rate: int = 16_000, sample_width: int = 2) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00" * sample_width * channels * 16)
    return output.getvalue()


def test_voice_runtime_preflight_skips_when_stt_is_disabled() -> None:
    assert (
        voice_runtime.run_voice_runtime_preflight(
            environment={"VOICE_STT_MODEL_PATH": "", "VOICE_CONVERSION_COMMAND": "missing"}
        )
        == "VOICE_RUNTIME_PREFLIGHT_SKIPPED_DISABLED"
    )


def test_voice_runtime_preflight_media_mode_does_not_require_stt_model(monkeypatch) -> None:
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(voice_runtime, "_check_converter", calls.append)

    result = voice_runtime.run_voice_runtime_preflight(
        require_media=True,
        environment={"VOICE_STT_MODEL_PATH": ""},
    )

    assert result == "VOICE_RUNTIME_PREFLIGHT_OK"
    assert calls == [{"VOICE_STT_MODEL_PATH": ""}]


def test_voice_runtime_preflight_fails_closed_when_converter_is_missing() -> None:
    with pytest.raises(VoiceRuntimePreflightError, match="converter_missing") as error:
        voice_runtime.run_voice_runtime_preflight(
            require_media=True,
            environment={
                "VOICE_STT_MODEL_PATH": "",
                "VOICE_CONVERSION_COMMAND": "missing-converter",
            },
        )

    assert error.value.category == "converter_missing"


def test_voice_runtime_preflight_rejects_non_pcm_wav_shape() -> None:
    try:
        voice_runtime._validate_wav_output(_wav_bytes(channels=2))
    except VoiceRuntimePreflightError as exc:
        assert exc.category == "wav_output_shape"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("stereo WAV must fail the mono preflight")


def test_voice_runtime_preflight_accepts_mono_16khz_s16_wav() -> None:
    voice_runtime._validate_wav_output(_wav_bytes())


def test_voice_runtime_main_reports_only_safe_category(monkeypatch, capsys) -> None:
    def fail(**_: object) -> str:
        raise VoiceRuntimePreflightError("converter_nonzero", return_code=187)

    monkeypatch.setattr(voice_runtime, "run_voice_runtime_preflight", fail)

    assert voice_runtime.main(["--require-media"]) == 1
    output = capsys.readouterr().out
    assert output.strip() == "VOICE_RUNTIME_PREFLIGHT_FAILED converter_nonzero return_code=187"
    assert "stderr" not in output
    assert "VOICE_CONVERSION_COMMAND" not in output
