import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(**overrides) -> Settings:
    values = {
        "bot_token": "test-token",
        "database_url": "sqlite+aiosqlite:///:memory:",
    }
    values.update(overrides)
    return Settings(**values)


def test_worker_timing_defaults_are_valid() -> None:
    settings = _settings()

    assert settings.default_timezone == "Europe/Moscow"
    assert settings.worker_lease_duration_seconds > (
        settings.worker_send_timeout_seconds + settings.worker_lease_safety_margin_seconds
    )
    assert settings.worker_retry_max_seconds >= settings.worker_retry_base_seconds


def test_worker_lease_must_cover_send_timeout_and_safety_margin() -> None:
    with pytest.raises(ValidationError, match="worker_lease_duration_seconds"):
        _settings(
            worker_lease_duration_seconds=40,
            worker_send_timeout_seconds=30,
            worker_lease_safety_margin_seconds=10,
        )


def test_worker_retry_max_cannot_be_below_retry_base() -> None:
    with pytest.raises(ValidationError, match="worker_retry_max_seconds"):
        _settings(worker_retry_base_seconds=60, worker_retry_max_seconds=30)
