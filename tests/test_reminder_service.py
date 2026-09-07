from datetime import UTC, datetime

from app.db.models import RecurrenceType, Reminder
from app.services.reminder_service import (
    advance_occurrence_until_future,
    build_snooze_state,
    calculate_next_occurrence,
    delivery_at_utc,
)
from app.utils.datetime_utils import to_utc


def test_daily_recurrence_is_calculated_in_schedule_timezone() -> None:
    canonical = to_utc(datetime(2026, 3, 28, 9, 0), "Europe/Helsinki")

    next_occurrence = calculate_next_occurrence(
        canonical,
        RecurrenceType.DAILY.value,
        1,
        timezone_name="Europe/Helsinki",
    )

    assert next_occurrence == datetime(2026, 3, 29, 6, 0, tzinfo=UTC)


def test_weekly_recurrence_is_calculated_in_schedule_timezone() -> None:
    canonical = to_utc(datetime(2026, 10, 18, 9, 0), "Europe/Helsinki")

    next_occurrence = calculate_next_occurrence(
        canonical,
        RecurrenceType.WEEKLY.value,
        1,
        timezone_name="Europe/Helsinki",
    )

    assert next_occurrence == datetime(2026, 10, 25, 7, 0, tzinfo=UTC)


def test_monthly_anchor_returns_to_day_31_after_short_february() -> None:
    january = to_utc(datetime(2026, 1, 31, 9, 0), "Europe/Moscow")
    february = calculate_next_occurrence(
        january,
        RecurrenceType.MONTHLY.value,
        1,
        timezone_name="Europe/Moscow",
        recurrence_day_of_month=31,
    )

    assert february == to_utc(datetime(2026, 2, 28, 9, 0), "Europe/Moscow")

    march = calculate_next_occurrence(
        february,
        RecurrenceType.MONTHLY.value,
        1,
        timezone_name="Europe/Moscow",
        recurrence_day_of_month=31,
    )

    assert march == to_utc(datetime(2026, 3, 31, 9, 0), "Europe/Moscow")


def test_missed_occurrences_advance_without_burst_replay() -> None:
    canonical = to_utc(datetime(2026, 1, 1, 9, 0), "Europe/Moscow")
    now_utc = to_utc(datetime(2026, 1, 4, 12, 0), "Europe/Moscow")

    next_occurrence = advance_occurrence_until_future(
        canonical,
        RecurrenceType.DAILY.value,
        1,
        timezone_name="Europe/Moscow",
        recurrence_day_of_month=None,
        now_utc=now_utc,
    )

    assert next_occurrence == to_utc(datetime(2026, 1, 5, 9, 0), "Europe/Moscow")


def test_snooze_keeps_canonical_occurrence_and_changes_delivery_only() -> None:
    canonical = datetime(2026, 3, 29, 6, 0, tzinfo=UTC)
    now_utc = datetime(2026, 3, 29, 6, 1, tzinfo=UTC)

    canonical_after, delivery_after = build_snooze_state(canonical, now_utc, 10)

    assert canonical_after == canonical
    assert delivery_after == datetime(2026, 3, 29, 6, 11, tzinfo=UTC)


def test_schedule_state_is_restart_reconstructable_from_persisted_columns() -> None:
    columns = set(Reminder.__table__.columns.keys())

    assert {
        "remind_at_utc",
        "schedule_timezone",
        "delivery_at_utc",
        "snoozed_until_utc",
        "recurrence_day_of_month",
    } <= columns

    reminder = Reminder(
        remind_at_utc=datetime(2026, 3, 29, 6, 0, tzinfo=UTC),
        schedule_timezone="Europe/Helsinki",
        delivery_at_utc=datetime(2026, 3, 29, 6, 11, tzinfo=UTC),
        snoozed_until_utc=datetime(2026, 3, 29, 6, 11, tzinfo=UTC),
        recurrence_type=RecurrenceType.DAILY.value,
        recurrence_interval=1,
        recurrence_day_of_month=None,
    )

    assert delivery_at_utc(reminder) == datetime(2026, 3, 29, 6, 11, tzinfo=UTC)
