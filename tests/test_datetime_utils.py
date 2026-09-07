from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.utils.datetime_utils import (
    from_utc_to_user,
    localize_in_timezone,
    resolve_schedule_datetime,
    to_utc,
)


def test_daily_local_time_keeps_wall_clock_across_dst_forward() -> None:
    before = to_utc(datetime(2026, 3, 28, 9, 0), "Europe/Helsinki")
    after = to_utc(datetime(2026, 3, 29, 9, 0), "Europe/Helsinki")

    assert before == datetime(2026, 3, 28, 7, 0, tzinfo=UTC)
    assert after == datetime(2026, 3, 29, 6, 0, tzinfo=UTC)
    assert from_utc_to_user(after, "Europe/Helsinki").hour == 9


def test_daily_local_time_keeps_wall_clock_across_dst_backward() -> None:
    before = to_utc(datetime(2026, 10, 24, 9, 0), "Europe/Helsinki")
    after = to_utc(datetime(2026, 10, 25, 9, 0), "Europe/Helsinki")

    assert before == datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
    assert after == datetime(2026, 10, 25, 7, 0, tzinfo=UTC)
    assert from_utc_to_user(after, "Europe/Helsinki").hour == 9


def test_nonexistent_local_time_is_shifted_forward_by_dst_gap() -> None:
    localized = localize_in_timezone(datetime(2026, 3, 29, 3, 30), "Europe/Helsinki")

    assert localized.hour == 4
    assert localized.minute == 30


def test_ambiguous_local_time_uses_earlier_occurrence() -> None:
    localized = localize_in_timezone(datetime(2026, 10, 25, 3, 30), "Europe/Helsinki")

    assert localized.fold == 0
    assert localized.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_calendar_ambiguous_input_still_uses_fold_zero_policy() -> None:
    assert to_utc(datetime(2026, 10, 25, 3, 30), "Europe/Helsinki") == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )


def test_instant_input_preserves_fold_one_and_exact_utc_instant() -> None:
    instant = datetime(
        2026,
        10,
        25,
        3,
        30,
        tzinfo=ZoneInfo("Europe/Helsinki"),
        fold=1,
    )

    assert instant.astimezone(UTC) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert resolve_schedule_datetime(instant, "Europe/Helsinki", "instant") == datetime(
        2026, 10, 25, 1, 30, tzinfo=UTC
    )


def test_naive_utc_database_value_is_interpreted_as_utc() -> None:
    local = from_utc_to_user(datetime(2026, 1, 1, 9, 0), "Europe/Moscow")

    assert local == datetime(2026, 1, 1, 12, 0, tzinfo=local.tzinfo)
