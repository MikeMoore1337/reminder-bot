import asyncio
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import RecurrenceType, Reminder, ReminderKind, ReminderMode, User
from app.handlers import reminders as reminders_handler
from app.services import reminder_service
from app.services.recurrence import encode_rule, legacy_rule, yearly_rule
from app.services.reminder_parser import parse_reminder_input
from app.services.reminder_service import (
    advance_occurrence_until_future,
    build_snooze_state,
    calculate_next_occurrence,
    delivery_at_utc,
    format_reminder_for_user,
)
from app.utils.datetime_utils import to_utc

FALLBACK_START_UTC = datetime(2026, 10, 24, 23, 30, tzinfo=UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _create_reminder_with_sqlite(monkeypatch, parsed, *, user_timezone: str):
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            user = User(
                telegram_user_id=1001,
                chat_id=2002,
                timezone=user_timezone,
            )
            async with session_factory() as session:
                session.add(user)
                await session.commit()
                await session.refresh(user)

            monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
            monkeypatch.setattr(reminder_service, "utc_now", lambda: FALLBACK_START_UTC)
            return await reminder_service.create_reminder(
                user=user,
                local_dt=parsed.local_dt,
                text=parsed.text,
                recurrence_type=parsed.recurrence_type,
                recurrence_interval=parsed.recurrence_interval,
                datetime_semantics=parsed.datetime_semantics,
            )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def test_daily_recurrence_is_calculated_in_schedule_timezone() -> None:
    canonical = to_utc(datetime(2026, 3, 28, 9, 0), "Europe/Helsinki")

    next_occurrence = calculate_next_occurrence(
        canonical,
        RecurrenceType.DAILY.value,
        1,
        timezone_name="Europe/Helsinki",
    )

    assert next_occurrence == datetime(2026, 3, 29, 6, 0, tzinfo=UTC)


def test_create_relative_one_off_preserves_fallback_fold_one_end_to_end(monkeypatch) -> None:
    timezone = "Europe/Helsinki"
    now_local = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo(timezone))
    parsed = parse_reminder_input("напомни через 2 часа проверить переход часов", now_local)

    assert parsed is not None
    assert parsed.datetime_semantics == "instant"
    assert parsed.local_dt.fold == 1
    assert parsed.local_dt.astimezone(UTC) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)

    reminder = _create_reminder_with_sqlite(monkeypatch, parsed, user_timezone=timezone)

    assert _as_utc(reminder.remind_at_utc) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert _as_utc(reminder.delivery_at_utc) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_create_calendar_ambiguous_input_keeps_fold_zero_policy(monkeypatch) -> None:
    timezone = "Europe/Helsinki"
    parsed = parse_reminder_input(
        "напомни 25.10.2026 03:30 проверить календарное время",
        datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo(timezone)),
    )

    assert parsed is not None
    assert parsed.datetime_semantics == "wall_clock"

    reminder = _create_reminder_with_sqlite(monkeypatch, parsed, user_timezone=timezone)

    assert _as_utc(reminder.remind_at_utc) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_create_elapsed_hourly_recurrence_preserves_fallback_fold_one(monkeypatch) -> None:
    timezone = "Europe/Helsinki"
    now_local = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo(timezone))
    parsed = parse_reminder_input("напомни каждые 2 часа проверить переход часов", now_local)

    assert parsed is not None
    assert parsed.recurrence_type == RecurrenceType.HOURLY.value
    assert parsed.recurrence_interval == 2
    assert parsed.datetime_semantics == "instant"
    assert parsed.local_dt.fold == 1

    reminder = _create_reminder_with_sqlite(monkeypatch, parsed, user_timezone=timezone)

    assert _as_utc(reminder.remind_at_utc) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert calculate_next_occurrence(
        _as_utc(reminder.remind_at_utc),
        RecurrenceType.HOURLY.value,
        2,
        timezone_name=timezone,
    ) == datetime(2026, 10, 25, 3, 30, tzinfo=UTC)


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


def test_recurring_reminder_keeps_persisted_timezone_after_profile_change() -> None:
    profile_timezone = User(timezone="Europe/Moscow").timezone
    reminder = Reminder(schedule_timezone="Europe/Helsinki")
    canonical = to_utc(datetime(2026, 3, 28, 9, 0), reminder.schedule_timezone)

    next_occurrence = calculate_next_occurrence(
        canonical,
        RecurrenceType.DAILY.value,
        1,
        timezone_name=reminder.schedule_timezone,
    )

    assert profile_timezone == "Europe/Moscow"
    assert reminder.schedule_timezone == "Europe/Helsinki"
    assert next_occurrence == datetime(2026, 3, 29, 6, 0, tzinfo=UTC)


def test_deadline_display_uses_persisted_schedule_timezone() -> None:
    reminder = Reminder(
        id=42,
        text="проверить оплату",
        remind_at_utc=to_utc(datetime(2026, 9, 10, 17, 0), "Europe/Moscow"),
        delivery_at_utc=to_utc(datetime(2026, 9, 10, 17, 0), "Europe/Moscow"),
        schedule_timezone="Europe/Moscow",
        kind=ReminderKind.DEADLINE.value,
        deadline_at_utc=to_utc(datetime(2026, 9, 10, 18, 0), "Europe/Moscow"),
        recurrence_type=RecurrenceType.NONE.value,
        recurrence_interval=1,
    )

    rendered = format_reminder_for_user(reminder, "Asia/Tokyo")

    assert "Дедлайн: 10.09.2026 18:00 (Europe/Moscow)" in rendered


def test_format_recurrence_localizes_reconstructed_legacy_none() -> None:
    reminder = Reminder(
        recurrence_type=RecurrenceType.NONE.value,
        recurrence_interval=1,
        recurrence_rule=None,
    )

    assert reminder_service.format_recurrence(reminder) == "нет"


def test_format_recurrence_localizes_explicit_legacy_none() -> None:
    reminder = Reminder(
        recurrence_type=RecurrenceType.NONE.value,
        recurrence_interval=1,
        recurrence_rule=encode_rule(legacy_rule(RecurrenceType.NONE.value, 1)),
    )

    assert reminder_service.format_recurrence(reminder) == "нет"


def test_saved_reminder_response_does_not_expose_internal_none() -> None:
    reminder = Reminder(
        id=42,
        text="проверить отчёт",
        remind_at_utc=datetime(2026, 9, 11, 9, 0, tzinfo=UTC),
        schedule_timezone="Europe/Moscow",
        state="scheduled",
        kind=ReminderKind.ORDINARY.value,
        mode=ReminderMode.NORMAL.value,
        recurrence_type=RecurrenceType.NONE.value,
        recurrence_interval=1,
        recurrence_rule=None,
    )
    user = User(timezone="Europe/Moscow")

    response = reminders_handler._saved_reminder_response(
        reminder,
        user,
        prefix="Напоминание сохранено.",
    )

    assert "Повтор: нет" in response
    assert "Повтор: none" not in response


@pytest.mark.parametrize(
    ("recurrence_type", "interval", "expected"),
    [
        (RecurrenceType.MINUTES.value, 5, "каждые 5 минут"),
        (RecurrenceType.HOURLY.value, 1, "каждый час"),
        (RecurrenceType.DAILY.value, 1, "каждый день"),
        (RecurrenceType.WEEKLY.value, 1, "каждую неделю"),
        (RecurrenceType.MONTHLY.value, 1, "каждый месяц"),
    ],
)
def test_format_recurrence_keeps_legacy_labels(
    recurrence_type: str,
    interval: int,
    expected: str,
) -> None:
    reminder = Reminder(
        recurrence_type=recurrence_type,
        recurrence_interval=interval,
        recurrence_rule=None,
    )

    assert reminder_service.format_recurrence(reminder) == expected


def test_format_recurrence_keeps_advanced_until_label() -> None:
    reminder = Reminder(
        recurrence_type=RecurrenceType.ADVANCED.value,
        recurrence_interval=1,
        recurrence_rule=encode_rule(yearly_rule(3, 15, time(9), until=date(2026, 12, 31))),
    )

    assert reminder_service.format_recurrence(reminder) == "ежегодно 15.03 в 09:00 до 2026-12-31"
