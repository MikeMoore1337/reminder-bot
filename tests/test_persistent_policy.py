from datetime import UTC, datetime

import pytest

from app.services.persistent_policy import (
    PersistentPolicy,
    is_quiet_hours,
    next_allowed_delivery,
    next_persistent_delivery,
    normalize_reminder_mode,
    parse_clock,
)


def test_mode_aliases_are_canonical_and_unknown_values_fail_closed() -> None:
    assert normalize_reminder_mode("важное") == "persistent"
    assert normalize_reminder_mode("important") == "persistent"
    assert normalize_reminder_mode("обычное") == "normal"

    with pytest.raises(ValueError):
        normalize_reminder_mode("forever")


def test_quiet_hours_use_the_persisted_timezone() -> None:
    candidate = datetime(2026, 1, 11, 3, 0, tzinfo=UTC)

    assert is_quiet_hours(
        candidate,
        "America/New_York",
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
    )
    assert next_allowed_delivery(
        candidate,
        "America/New_York",
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
    ) == datetime(2026, 1, 11, 13, 0, tzinfo=UTC)


def test_quiet_hour_end_uses_nonexistent_time_dst_policy() -> None:
    # 01:30 EST is inside the overnight window.  03:30 local is the first
    # valid wall-clock target after the spring-forward gap.
    candidate = datetime(2026, 3, 8, 6, 30, tzinfo=UTC)

    assert next_allowed_delivery(
        candidate,
        "America/New_York",
        quiet_hours_start="22:00",
        quiet_hours_end="03:30",
    ) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)


def test_persistent_delivery_is_bounded_by_both_policy_limits() -> None:
    policy = PersistentPolicy(interval_minutes=15, max_deliveries=10, max_escalations=2)

    assert policy.max_effective_deliveries == 3
    assert next_persistent_delivery(
        datetime(2026, 9, 8, 10, 0, tzinfo=UTC), "Europe/Moscow", policy
    ) == datetime(2026, 9, 8, 10, 15, tzinfo=UTC)


def test_policy_rejects_unbounded_or_malformed_configuration() -> None:
    with pytest.raises(ValueError):
        PersistentPolicy(interval_minutes=4)
    with pytest.raises(ValueError):
        PersistentPolicy(max_deliveries=0)
    with pytest.raises(ValueError):
        PersistentPolicy(max_escalations=100)
    with pytest.raises(ValueError):
        parse_clock("25:00")
