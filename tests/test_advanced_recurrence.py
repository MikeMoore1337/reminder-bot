import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import (
    OccurrenceState,
    Reminder,
    ReminderClarification,
    ReminderOccurrence,
    ReminderState,
    User,
)
from app.handlers import reminders as reminders_handler
from app.services import clarification_service, reminder_service
from app.services.recurrence import (
    completion_relative_rule,
    first_occurrence_after,
    legacy_rule,
    monthly_last_rule,
    monthly_nth_rule,
    next_occurrence,
    weekly_rule,
    yearly_rule,
)
from app.services.reminder_parser import (
    ClarificationRequest,
    ParsedReminder,
    parse_clarification_answer,
    parse_reminder_input,
)
from app.utils.datetime_utils import to_utc

MOSCOW_NOW = datetime(2026, 9, 7, 10, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def test_advanced_russian_recurrence_forms_are_canonical() -> None:
    cases = {
        "напомни каждый понедельник в 09:00 планёрка": (
            "weekly_days",
            [0],
            datetime(2026, 9, 14, 9, 0),
        ),
        "напомни каждый понедельник и четверг в 09:00 тренировка": (
            "weekly_days",
            [0, 3],
            datetime(2026, 9, 10, 9, 0),
        ),
        "напомни по будням в 09:00 отчёт": (
            "weekdays",
            [0, 1, 2, 3, 4],
            datetime(2026, 9, 8, 9, 0),
        ),
        "напомни каждые 2 недели по понедельникам в 09:00 отчёт": (
            "weekly_days",
            [0],
            datetime(2026, 9, 21, 9, 0),
        ),
        "напомни каждый второй вторник месяца в 09:00 отчёт": (
            "monthly_nth",
            None,
            datetime(2026, 9, 8, 9, 0),
        ),
        "напомни в последнюю пятницу месяца в 18:00 зарплата": (
            "monthly_last",
            None,
            datetime(2026, 9, 25, 18, 0),
        ),
        "напомни каждый год 15 марта в 09:00 годовщина": (
            "yearly",
            None,
            datetime(2027, 3, 15, 9, 0),
        ),
    }

    for raw, (kind, weekdays, expected_local) in cases.items():
        parsed = parse_reminder_input(raw, MOSCOW_NOW)
        assert isinstance(parsed, ParsedReminder)
        assert parsed.recurrence_rule is not None
        assert parsed.recurrence_rule["kind"] == kind
        if weekdays is not None:
            assert parsed.recurrence_rule["weekdays"] == weekdays
        assert parsed.local_dt.replace(tzinfo=None) == expected_local


def test_weekday_variants_and_ambiguous_or_malformed_inputs_are_safe() -> None:
    selected = parse_reminder_input(
        "напомни по понедельникам и четвергам в 09:00 отчёт",
        MOSCOW_NOW,
    )
    assert isinstance(selected, ParsedReminder)
    assert selected.recurrence_rule is not None
    assert selected.recurrence_rule["weekdays"] == [0, 3]

    for raw in (
        "напомни в пятницу в 8 позвонить",
        "напомни завтра вечером позвонить",
        "напомни часов в девять позвонить",
        "напомни после обеда позвонить",
    ):
        result = parse_reminder_input(raw, MOSCOW_NOW)
        assert isinstance(result, ClarificationRequest), raw

    malformed = parse_reminder_input(
        "напомни каждый понедельник в 25:00 позвонить",
        MOSCOW_NOW,
    )
    assert not isinstance(malformed, ParsedReminder)


def test_legacy_scalar_recurrence_remains_reconstructable() -> None:
    reminder = Reminder(
        recurrence_type="monthly",
        recurrence_interval=1,
        recurrence_day_of_month=31,
    )

    assert reminder_service.get_recurrence_rule(reminder) == legacy_rule(
        "monthly",
        1,
        31,
    )


def test_calendar_rule_sequence_is_strictly_increasing() -> None:
    rule = weekly_rule(
        [0, 3],
        time(9),
        anchor_week=date(2026, 9, 7),
    )
    cursor = to_utc(datetime(2026, 9, 7, 8, 0), "Europe/Moscow")
    for _ in range(12):
        candidate = next_occurrence(cursor, rule, "Europe/Moscow")
        assert candidate is not None
        assert candidate > cursor
        cursor = candidate


def test_repeat_until_is_persisted_in_the_canonical_rule() -> None:
    parsed = parse_reminder_input(
        "напомни каждый день в 09:00 до 31.12.2026 зарядка",
        MOSCOW_NOW,
    )

    assert isinstance(parsed, ParsedReminder)
    assert parsed.recurrence_rule == {
        "version": 1,
        "kind": "legacy",
        "recurrence_type": "daily",
        "interval": 1,
        "day_of_month": None,
        "until": "2026-12-31",
    }


def test_completion_relative_parser_supports_marker_before_or_after_text() -> None:
    before = parse_reminder_input(
        "напомни завтра в 9, через 3 дня после выполнения: отчёт",
        MOSCOW_NOW,
    )
    after = parse_reminder_input(
        "напомни завтра в 9 отчёт, через 3 дня после выполнения",
        MOSCOW_NOW,
    )

    assert isinstance(before, ParsedReminder)
    assert isinstance(after, ParsedReminder)
    assert before.text == after.text == "отчёт"
    assert (
        before.recurrence_rule
        == after.recurrence_rule
        == {
            "version": 1,
            "kind": "completion_relative",
            "after_days": 3,
        }
    )


def test_ambiguous_input_never_becomes_a_reminder_and_can_be_resolved() -> None:
    request = parse_reminder_input("напомни завтра вечером позвонить", MOSCOW_NOW)

    assert isinstance(request, ClarificationRequest)
    assert "15 минут" in request.prompt

    resolved = parse_clarification_answer(
        request.raw_text,
        "18:30",
        now_local=MOSCOW_NOW,
    )
    assert isinstance(resolved, ParsedReminder)
    assert resolved.local_dt == datetime(2026, 9, 8, 18, 30, tzinfo=ZoneInfo("Europe/Moscow"))
    assert resolved.text == "позвонить"


def test_clarification_time_only_answers_are_calendar_safe() -> None:
    request = parse_reminder_input("напомни после обеда позвонить", MOSCOW_NOW)
    assert isinstance(request, ClarificationRequest)

    same_day = parse_clarification_answer(
        request.raw_text,
        "14:00",
        now_local=MOSCOW_NOW,
    )
    assert isinstance(same_day, ParsedReminder)
    assert same_day.local_dt == datetime(
        2026,
        9,
        7,
        14,
        0,
        tzinfo=ZoneInfo("Europe/Moscow"),
    )
    assert same_day.text == "позвонить"

    after_time = parse_clarification_answer(
        request.raw_text,
        "14:00",
        now_local=MOSCOW_NOW.replace(hour=15),
    )
    assert isinstance(after_time, ParsedReminder)
    assert after_time.local_dt == datetime(
        2026,
        9,
        8,
        14,
        0,
        tzinfo=ZoneInfo("Europe/Moscow"),
    )


def test_after_lunch_answer_creates_reminder_through_handler(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(clarification_service, "SessionLocal", session_factory)
            monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
            user = User(
                telegram_user_id=506,
                chat_id=606,
                timezone="Europe/Moscow",
            )
            async with session_factory() as session:
                session.add(user)
                await session.commit()
                await session.refresh(user)

            request = parse_reminder_input(
                "напомни после обеда позвонить",
                MOSCOW_NOW,
            )
            assert isinstance(request, ClarificationRequest)
            await clarification_service.create_clarification(user, request)
            message = type(
                "FakeMessage",
                (),
                {"text": "14:00", "answer": AsyncMock()},
            )()

            assert await reminders_handler._handle_clarification(message, user)
            message.answer.assert_awaited_once()
            async with session_factory() as session:
                reminders = list(
                    (
                        await session.scalars(select(Reminder).where(Reminder.user_id == user.id))
                    ).all()
                )
            assert len(reminders) == 1
            assert reminders[0].text == "позвонить"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_weekly_interval_above_bound_returns_bounded_feedback() -> None:
    result = parse_reminder_input(
        "напомни каждые 53 недели по понедельникам в 09:00 отчёт",
        MOSCOW_NOW,
    )

    assert isinstance(result, ClarificationRequest)
    assert "от 1 до 52" in result.prompt


def test_calendar_rules_handle_dst_month_end_and_leap_year() -> None:
    Helsinki = "Europe/Helsinki"
    weekly = weekly_rule([0], time(9), anchor_week=date(2026, 3, 23))
    march_occurrence = datetime(2026, 3, 23, 7, 0, tzinfo=UTC)
    assert next_occurrence(march_occurrence, weekly, Helsinki) == datetime(
        2026, 3, 30, 6, 0, tzinfo=UTC
    )

    nth = monthly_nth_rule(1, 2, time(9))
    assert first_occurrence_after(
        datetime(2026, 9, 1, 0, tzinfo=ZoneInfo(Helsinki)), nth, Helsinki
    ) == datetime(2026, 9, 8, 6, 0, tzinfo=UTC)

    last = monthly_last_rule(4, time(18))
    assert first_occurrence_after(
        datetime(2026, 9, 1, 0, tzinfo=ZoneInfo(Helsinki)), last, Helsinki
    ) == datetime(2026, 9, 25, 15, 0, tzinfo=UTC)

    leap = yearly_rule(2, 29, time(9))
    assert first_occurrence_after(
        datetime(2027, 1, 1, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        leap,
        "Europe/Moscow",
    ) == datetime(2028, 2, 29, 6, 0, tzinfo=UTC)


def test_repeat_until_has_explicit_terminal_behavior() -> None:
    rule = weekly_rule(
        [0],
        time(9),
        anchor_week=date(2026, 9, 7),
        until=date(2026, 9, 14),
    )
    final = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)

    assert next_occurrence(final, rule, "Europe/Moscow") is None


def test_completion_relative_uses_completion_timestamp_and_local_calendar() -> None:
    rule = completion_relative_rule(1)
    completion = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)

    assert next_occurrence(
        datetime(2026, 10, 24, 0, tzinfo=UTC),
        rule,
        "Europe/Helsinki",
        completion_at_utc=completion,
    ) == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)


def test_clarification_is_restart_safe_and_expires(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(clarification_service, "SessionLocal", session_factory)
            user = User(telegram_user_id=501, chat_id=601, timezone="Europe/Moscow")
            async with session_factory() as session:
                session.add(user)
                await session.commit()
                await session.refresh(user)

            request = parse_reminder_input("напомни после обеда позвонить", MOSCOW_NOW)
            assert isinstance(request, ClarificationRequest)
            created_at = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
            created = await clarification_service.create_clarification(
                user,
                request,
                now_utc=created_at,
            )
            restored = await clarification_service.get_active_clarification(
                user,
                now_utc=created_at + timedelta(minutes=1),
            )
            assert restored is not None
            assert restored.id == created.id
            assert restored.raw_text == request.raw_text

            assert (
                await clarification_service.get_active_clarification(
                    user,
                    now_utc=created_at + timedelta(minutes=16),
                )
                is None
            )
            async with session_factory() as session:
                assert (
                    await session.scalar(
                        select(ReminderClarification).where(
                            ReminderClarification.user_id == user.id,
                        )
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_advanced_rule_is_persisted_and_reconstructed_after_restart(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
            now_utc = datetime(2026, 9, 7, 7, 0, tzinfo=UTC)
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now_utc)
            user = User(
                telegram_user_id=502,
                chat_id=602,
                timezone="Europe/Moscow",
            )
            async with session_factory() as session:
                session.add(user)
                await session.commit()
                await session.refresh(user)

            parsed = parse_reminder_input(
                "напомни каждый понедельник и четверг в 09:00 отчёт",
                datetime(2026, 9, 7, 10, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            )
            assert isinstance(parsed, ParsedReminder)
            reminder = await reminder_service.create_reminder(
                user,
                parsed.local_dt,
                parsed.text,
                recurrence_type=parsed.recurrence_type,
                recurrence_interval=parsed.recurrence_interval,
                recurrence_rule=parsed.recurrence_rule,
                recurrence_day_of_month=parsed.recurrence_day_of_month,
            )
            assert reminder.recurrence_type == "advanced"
            assert json.loads(reminder.recurrence_rule or "") == parsed.recurrence_rule

            # A fresh ORM load uses only the persisted canonical rule, not parser state.
            async with session_factory() as session:
                restored = await session.get(Reminder, reminder.id)
            assert restored is not None
            assert reminder_service.get_recurrence_rule(restored) == parsed.recurrence_rule
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_completion_relative_done_and_pause_resume_keep_action_anchor(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
            user = User(
                telegram_user_id=503,
                chat_id=603,
                timezone="Europe/Helsinki",
            )
            initial_at = to_utc(datetime(2026, 10, 24, 9, 0), user.timezone)
            done_at = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
            rule = completion_relative_rule(1)
            async with session_factory() as session:
                session.add(user)
                await session.flush()
                reminder = Reminder(
                    user_id=user.id,
                    chat_id=user.chat_id,
                    text="отчёт",
                    remind_at_utc=initial_at,
                    delivery_at_utc=None,
                    schedule_timezone=user.timezone,
                    status="sent",
                    state=ReminderState.DELIVERED.value,
                    action_revision=0,
                    recurrence_type="advanced",
                    recurrence_interval=1,
                    recurrence_rule=json.dumps(rule),
                    last_message_id=77,
                    last_delivery_occurrence_utc=initial_at,
                )
                session.add(reminder)
                await session.flush()
                occurrence = ReminderOccurrence(
                    reminder_id=reminder.id,
                    occurrence_at_utc=initial_at,
                    delivery_at_utc=initial_at,
                    status=OccurrenceState.DELIVERED.value,
                    action_revision=0,
                    message_id=77,
                    delivered_at=initial_at,
                )
                session.add(occurrence)
                await session.commit()
                reminder_id = reminder.id
                occurrence_id = occurrence.id

            monkeypatch.setattr(reminder_service, "utc_now", lambda: done_at)
            assert await reminder_service.pause_reminder(
                user,
                reminder_id,
                expected_revision=0,
                expected_occurrence_id=occurrence_id,
                expected_occurrence_at_utc=initial_at,
                expected_message_id=77,
            )
            async with session_factory() as session:
                paused = await session.get(Reminder, reminder_id)
                assert paused is not None
                assert paused.state == ReminderState.PAUSED.value
                paused_revision = paused.action_revision

            assert await reminder_service.resume_reminder(
                user,
                reminder_id,
                expected_revision=paused_revision,
            )
            async with session_factory() as session:
                resumed = await session.get(Reminder, reminder_id)
                assert resumed is not None
                assert resumed.state == ReminderState.DELIVERED.value
                assert resumed.delivery_at_utc is None
                resumed_revision = resumed.action_revision

            assert await reminder_service.complete_reminder(
                user,
                reminder_id,
                expected_revision=resumed_revision,
                expected_occurrence_id=occurrence_id,
                expected_message_id=77,
            )
            expected_next = next_occurrence(
                initial_at,
                rule,
                user.timezone,
                completion_at_utc=done_at,
            )
            async with session_factory() as session:
                completed = await session.get(Reminder, reminder_id)
                assert completed is not None
                assert completed.state == ReminderState.SCHEDULED.value
                assert completed.status == "pending"
                assert completed.remind_at_utc.replace(tzinfo=UTC) == expected_next
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_completion_relative_snooze_child_advances_parent_from_done(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
            user = User(
                telegram_user_id=504,
                chat_id=604,
                timezone="Europe/Moscow",
            )
            initial_at = to_utc(datetime(2026, 9, 10, 9, 0), user.timezone)
            delivered_at = datetime(2026, 9, 10, 7, 0, tzinfo=UTC)
            snoozed_until = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
            rule = completion_relative_rule(2)
            async with session_factory() as session:
                session.add(user)
                await session.flush()
                parent = Reminder(
                    user_id=user.id,
                    chat_id=user.chat_id,
                    text="отчёт",
                    remind_at_utc=initial_at,
                    delivery_at_utc=None,
                    schedule_timezone=user.timezone,
                    status="sent",
                    state=ReminderState.DELIVERED.value,
                    action_revision=0,
                    recurrence_type="advanced",
                    recurrence_interval=1,
                    recurrence_rule=json.dumps(rule),
                    last_message_id=78,
                    last_delivery_occurrence_utc=initial_at,
                )
                session.add(parent)
                await session.flush()
                source = ReminderOccurrence(
                    reminder_id=parent.id,
                    occurrence_at_utc=initial_at,
                    delivery_at_utc=initial_at,
                    status=OccurrenceState.DELIVERED.value,
                    action_revision=0,
                    message_id=78,
                    delivered_at=delivered_at,
                )
                session.add(source)
                await session.commit()
                parent_id = parent.id
                source_id = source.id

            monkeypatch.setattr(reminder_service, "utc_now", lambda: delivered_at)
            child = await reminder_service.snooze_reminder(
                user,
                parent_id,
                expected_revision=0,
                expected_occurrence_id=source_id,
                expected_occurrence_at_utc=initial_at,
                expected_message_id=78,
                target_at_utc=snoozed_until,
            )
            assert child is not None
            child_id = child.id

            async with session_factory() as session:
                persisted_child = await session.get(Reminder, child_id)
                assert persisted_child is not None
                persisted_child.state = ReminderState.DELIVERED.value
                persisted_child.status = "sent"
                persisted_child.delivery_at_utc = None
                persisted_child.last_message_id = 79
                persisted_child.last_delivery_occurrence_utc = snoozed_until
                child_occurrence = ReminderOccurrence(
                    reminder_id=child_id,
                    occurrence_at_utc=snoozed_until,
                    delivery_at_utc=snoozed_until,
                    status=OccurrenceState.DELIVERED.value,
                    action_revision=persisted_child.action_revision,
                    message_id=79,
                    delivered_at=snoozed_until,
                )
                session.add(child_occurrence)
                await session.commit()
                child_revision = persisted_child.action_revision
                child_occurrence_id = child_occurrence.id

            done_at = datetime(2026, 9, 10, 8, 30, tzinfo=UTC)
            monkeypatch.setattr(reminder_service, "utc_now", lambda: done_at)
            assert await reminder_service.complete_reminder(
                user,
                child_id,
                expected_revision=child_revision,
                expected_occurrence_id=child_occurrence_id,
                expected_message_id=79,
            )
            expected_next = next_occurrence(
                initial_at,
                rule,
                user.timezone,
                completion_at_utc=done_at,
            )
            async with session_factory() as session:
                restored_parent = await session.get(Reminder, parent_id)
                restored_child = await session.get(Reminder, child_id)
                assert restored_parent is not None
                assert restored_child is not None
                assert restored_parent.state == ReminderState.SCHEDULED.value
                assert restored_parent.status == "pending"
                assert restored_parent.remind_at_utc.replace(tzinfo=UTC) == expected_next
                assert restored_child.state == ReminderState.COMPLETED.value
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_bounded_recurrence_done_schedules_or_terminates_and_persists(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
            user = User(
                telegram_user_id=505,
                chat_id=605,
                timezone="Europe/Moscow",
            )
            async with session_factory() as session:
                session.add(user)
                await session.commit()
                await session.refresh(user)

            async def seed(occurrence_at: datetime, message_id: int, rule: dict) -> tuple[int, int]:
                async with session_factory() as session:
                    reminder = Reminder(
                        user_id=user.id,
                        chat_id=user.chat_id,
                        text="bounded report",
                        remind_at_utc=occurrence_at,
                        delivery_at_utc=None,
                        schedule_timezone=user.timezone,
                        status="sent",
                        state=ReminderState.DELIVERED.value,
                        action_revision=0,
                        recurrence_type="advanced",
                        recurrence_interval=1,
                        recurrence_rule=json.dumps(rule),
                        last_message_id=message_id,
                        last_delivery_occurrence_utc=occurrence_at,
                    )
                    session.add(reminder)
                    await session.flush()
                    occurrence = ReminderOccurrence(
                        reminder_id=reminder.id,
                        occurrence_at_utc=occurrence_at,
                        delivery_at_utc=occurrence_at,
                        status=OccurrenceState.DELIVERED.value,
                        action_revision=0,
                        message_id=message_id,
                        delivered_at=occurrence_at,
                    )
                    session.add(occurrence)
                    await session.commit()
                    return reminder.id, occurrence.id

            rule = weekly_rule(
                [0],
                time(9),
                anchor_week=date(2026, 9, 7),
                until=date(2026, 9, 21),
            )
            non_final_at = to_utc(datetime(2026, 9, 14, 9, 0), user.timezone)
            non_final_id, non_final_occurrence_id = await seed(non_final_at, 80, rule)
            non_final_done_at = to_utc(datetime(2026, 9, 14, 10, 0), user.timezone)
            monkeypatch.setattr(reminder_service, "utc_now", lambda: non_final_done_at)
            assert await reminder_service.complete_reminder(
                user,
                non_final_id,
                expected_revision=0,
                expected_occurrence_id=non_final_occurrence_id,
                expected_message_id=80,
            )
            expected_next = to_utc(datetime(2026, 9, 21, 9, 0), user.timezone)
            async with session_factory() as session:
                scheduled = await session.get(Reminder, non_final_id)
                assert scheduled is not None
                assert scheduled.state == ReminderState.SCHEDULED.value
                assert scheduled.status == "pending"
                assert scheduled.remind_at_utc.replace(tzinfo=UTC) == expected_next

            final_at = expected_next
            final_id, final_occurrence_id = await seed(final_at, 81, rule)
            final_done_at = to_utc(datetime(2026, 9, 21, 10, 0), user.timezone)
            monkeypatch.setattr(reminder_service, "utc_now", lambda: final_done_at)
            assert await reminder_service.complete_reminder(
                user,
                final_id,
                expected_revision=0,
                expected_occurrence_id=final_occurrence_id,
                expected_message_id=81,
            )

            # A fresh ORM load proves the terminal state survives a restart.
            async with session_factory() as session:
                completed = await session.get(Reminder, final_id)
                assert completed is not None
                assert completed.state == ReminderState.COMPLETED.value
                assert completed.status == "sent"
                assert completed.delivery_at_utc is None
                assert completed.last_message_id is None
                assert completed.completed_at is not None
                assert completed.completed_at.replace(tzinfo=UTC) == final_done_at
        finally:
            await engine.dispose()

    asyncio.run(scenario())
