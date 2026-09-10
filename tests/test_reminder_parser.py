from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.reminder_parser import ClarificationRequest, ParsedReminder, parse_reminder_input

NOW_LOCAL = datetime(2026, 1, 1, 12, 0, 45, 123456, tzinfo=ZoneInfo("Europe/Moscow"))


@pytest.mark.parametrize(
    ("number_words", "unit", "amount"),
    [
        ("одну", "минуту", timedelta(minutes=1)),
        ("две", "минуты", timedelta(minutes=2)),
        ("пять", "минут", timedelta(minutes=5)),
        ("десять", "минут", timedelta(minutes=10)),
        ("пятнадцать", "минут", timedelta(minutes=15)),
        ("двадцать пять", "минут", timedelta(minutes=25)),
        ("один", "час", timedelta(hours=1)),
        ("два", "часа", timedelta(hours=2)),
        ("двенадцать", "часов", timedelta(hours=12)),
        ("двадцать один", "час", timedelta(hours=21)),
    ],
)
def test_spoken_relative_number_words_parse_in_temporal_position(
    number_words: str,
    unit: str,
    amount: timedelta,
) -> None:
    parsed = parse_reminder_input(
        f"  Напомни через {number_words} {unit} проверить тест.  ",
        NOW_LOCAL,
    )

    assert isinstance(parsed, ParsedReminder)
    assert parsed.local_dt == NOW_LOCAL + amount
    assert parsed.text == "проверить тест."
    assert parsed.recurrence_type == "none"
    assert parsed.mode == "normal"
    assert parsed.datetime_semantics == "instant"


@pytest.mark.parametrize(
    ("number_words", "unit", "amount"),
    [
        ("один", "минуту", timedelta(minutes=1)),
        ("одна", "минуту", timedelta(minutes=1)),
        ("одну", "минуту", timedelta(minutes=1)),
        ("два", "минуты", timedelta(minutes=2)),
        ("две", "минуты", timedelta(minutes=2)),
        ("три", "минуты", timedelta(minutes=3)),
        ("четыре", "минуты", timedelta(minutes=4)),
        ("шестьдесят", "минут", timedelta(minutes=60)),
    ],
)
def test_spoken_relative_number_forms_are_deterministically_bounded(
    number_words: str,
    unit: str,
    amount: timedelta,
) -> None:
    parsed = parse_reminder_input(
        f"напомни через {number_words} {unit} bounded test",
        NOW_LOCAL,
    )

    assert isinstance(parsed, ParsedReminder)
    assert parsed.local_dt == NOW_LOCAL + amount
    assert parsed.text == "bounded test"


def test_spoken_relative_number_words_do_not_normalize_reminder_body() -> None:
    parsed = parse_reminder_input(
        "напомни через две минуты купить две бутылки воды",
        NOW_LOCAL,
    )

    assert isinstance(parsed, ParsedReminder)
    assert parsed.local_dt == NOW_LOCAL + timedelta(minutes=2)
    assert parsed.text == "купить две бутылки воды"


def test_unsupported_relative_number_words_stay_ambiguous() -> None:
    parsed = parse_reminder_input(
        "напомни через сто минут проверить тест",
        NOW_LOCAL,
    )

    assert isinstance(parsed, ClarificationRequest)


def test_missing_schedule_stays_ambiguous() -> None:
    parsed = parse_reminder_input("напомни купить молоко", NOW_LOCAL)

    assert isinstance(parsed, ClarificationRequest)


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
