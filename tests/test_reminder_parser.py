from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.reminder_parser import parse_reminder_input


def test_relative_minute_does_not_truncate_elapsed_seconds() -> None:
    now_local = datetime(2026, 1, 1, 12, 0, 45, 123456, tzinfo=ZoneInfo("Europe/Moscow"))

    parsed = parse_reminder_input("напомни через 1 минуту позвонить", now_local)

    assert parsed is not None
    assert parsed.datetime_semantics == "instant"
    assert parsed.local_dt == datetime(
        2026, 1, 1, 12, 1, 45, 123456, tzinfo=ZoneInfo("Europe/Moscow")
    )


def test_relative_recurrence_preserves_elapsed_seconds() -> None:
    now_local = datetime(2026, 1, 1, 12, 0, 45, tzinfo=ZoneInfo("Europe/Moscow"))

    parsed = parse_reminder_input("напомни каждые 10 минут проверить сервер", now_local)

    assert parsed is not None
    assert parsed.datetime_semantics == "instant"
    assert parsed.local_dt == datetime(2026, 1, 1, 12, 10, 45, tzinfo=ZoneInfo("Europe/Moscow"))


def test_relative_interval_remains_elapsed_across_dst_forward() -> None:
    timezone = ZoneInfo("Europe/Helsinki")
    now_local = datetime(2026, 3, 29, 2, 30, tzinfo=timezone)

    parsed = parse_reminder_input("напомни через 1 час проверить переход часов", now_local)

    assert parsed is not None
    assert parsed.local_dt == datetime(2026, 3, 29, 4, 30, tzinfo=timezone)
    assert parsed.datetime_semantics == "instant"


def test_leap_day_accepts_valid_year_and_rejects_invalid_year() -> None:
    valid = parse_reminder_input(
        "напомни 29.02.2028 09:00 проверить календарь",
        datetime(2026, 1, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow")),
    )
    invalid = parse_reminder_input(
        "напомни 29.02.2027 09:00 проверить календарь",
        datetime(2026, 1, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow")),
    )

    assert valid is not None
    assert valid.local_dt == datetime(2028, 2, 29, 9, 0)
    assert invalid is None


def test_persistent_mode_marker_is_parsed_without_polluting_reminder_text() -> None:
    now_local = datetime(2026, 1, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    prefixed = parse_reminder_input("напомни важное завтра в 9 позвонить", now_local)
    suffixed = parse_reminder_input("напомни завтра в 9 позвонить [important]", now_local)
    ordinary = parse_reminder_input("напомни завтра в 9 позвонить", now_local)

    assert prefixed is not None
    assert prefixed.mode == "persistent"
    assert prefixed.text == "позвонить"
    assert suffixed is not None
    assert suffixed.mode == "persistent"
    assert suffixed.text == "позвонить"
    assert ordinary is not None
    assert ordinary.mode == "normal"
