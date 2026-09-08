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


def test_persistent_policy_settings_are_bounded_and_validate_quiet_hours() -> None:
    settings = _settings(
        persistent_repeat_interval_minutes=15,
        persistent_max_deliveries=8,
        persistent_max_escalations=4,
        persistent_quiet_hours_start="23:00",
        persistent_quiet_hours_end="07:00",
        persistent_user_cooldown_minutes=2,
    )

    assert settings.persistent_repeat_interval_minutes == 15
    assert settings.persistent_max_deliveries == 8
    assert settings.persistent_max_escalations == 4

    with pytest.raises(ValidationError):
        _settings(persistent_repeat_interval_minutes=4)
    with pytest.raises(ValidationError):
        _settings(persistent_quiet_hours_start="25:00")
