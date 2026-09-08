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


def test_digest_lease_default_follows_existing_worker_lease() -> None:
    settings = _settings(
        worker_lease_duration_seconds=70,
        worker_send_timeout_seconds=55,
        worker_lease_safety_margin_seconds=10,
    )

    assert settings.digest_lease_duration_seconds == 70


def test_worker_lease_must_cover_send_timeout_and_safety_margin() -> None:
    with pytest.raises(ValidationError, match="worker_lease_duration_seconds"):
        _settings(
            worker_lease_duration_seconds=40,
            worker_send_timeout_seconds=30,
            worker_lease_safety_margin_seconds=10,
        )


def test_digest_lease_must_cover_send_timeout_and_safety_margin() -> None:
    with pytest.raises(ValidationError, match="digest_lease_duration_seconds"):
        _settings(
            digest_lease_duration_seconds=40,
            worker_send_timeout_seconds=30,
            worker_lease_safety_margin_seconds=10,
        )


def test_worker_retry_max_cannot_be_below_retry_base() -> None:
    with pytest.raises(ValidationError, match="worker_retry_max_seconds"):
        _settings(worker_retry_base_seconds=60, worker_retry_max_seconds=30)


def test_condition_bounds_are_opt_in_and_lease_covers_request_timeout() -> None:
    settings = _settings()

    assert settings.condition_worker_enabled is False
    assert settings.condition_cleanup_interval_seconds == 3600
    assert settings.condition_authorization_env_allowlist == frozenset()
    assert settings.condition_lease_duration_seconds > settings.condition_request_timeout_seconds
    assert settings.condition_retry_max_seconds >= settings.condition_retry_base_seconds

    with pytest.raises(ValidationError, match="condition_lease_duration_seconds"):
        _settings(condition_request_timeout_seconds=30, condition_lease_duration_seconds=30)
    with pytest.raises(ValidationError, match="condition_retry_max_seconds"):
        _settings(condition_retry_base_seconds=60, condition_retry_max_seconds=30)


def test_condition_authorization_allowlist_is_deployment_owned_and_bounded() -> None:
    settings = _settings(
        CONDITION_AUTHORIZATION_ENV_ALLOWLIST="provider_token,SECOND_PROVIDER_TOKEN"
    )

    assert settings.condition_authorization_env_allowlist == frozenset(
        {"PROVIDER_TOKEN", "SECOND_PROVIDER_TOKEN"}
    )
    with pytest.raises(ValidationError, match="condition_authorization_env_allowlist"):
        _settings(CONDITION_AUTHORIZATION_ENV_ALLOWLIST="provider-token")


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
