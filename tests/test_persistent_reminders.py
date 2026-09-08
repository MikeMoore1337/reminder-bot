import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.callbacks import CallbackAction, parse_callback
from app.db.base import Base
from app.db.models import (
    OccurrenceState,
    Reminder,
    ReminderOccurrence,
    ReminderState,
    User,
)
from app.services import reminder_service
from app.workers import reminder_worker as worker


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _worker_settings(**overrides):
    values = {
        "worker_lease_duration_seconds": 60,
        "worker_max_attempts": 3,
        "persistent_user_cooldown_minutes": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _setup(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    connection = await engine.connect()
    await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "worker_metrics", worker.WorkerMetrics())
    return engine, connection, session_factory


async def _add_user(
    session_factory,
    *,
    timezone: str = "Europe/Moscow",
    telegram_user_id: int = 1001,
    chat_id: int = 2002,
) -> User:
    async with session_factory() as session:
        user = User(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone=timezone,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _add_persistent_reminder(
    session_factory,
    user: User,
    now_utc: datetime,
    *,
    delivery_at_utc: datetime | None = None,
    max_deliveries: int = 6,
    max_escalations: int = 5,
    quiet_hours_start: str = "22:00",
    quiet_hours_end: str = "08:00",
    state: str = ReminderState.SCHEDULED.value,
    action_revision: int = 0,
    message_id: int | None = None,
    delivered_occurrence: bool = False,
) -> tuple[Reminder, ReminderOccurrence | None]:
    delivered = delivered_occurrence or state == ReminderState.DELIVERED.value
    canonical_at = now_utc
    occurrence_at = canonical_at if delivered else None
    async with session_factory() as session:
        reminder = Reminder(
            user_id=user.id,
            chat_id=user.chat_id,
            text="important test reminder",
            remind_at_utc=canonical_at,
            delivery_at_utc=delivery_at_utc if delivery_at_utc is not None else canonical_at,
            schedule_timezone=user.timezone,
            status="sent" if delivered else "pending",
            state=state,
            action_revision=action_revision,
            mode="persistent",
            persistent_interval_minutes=15,
            persistent_max_deliveries=max_deliveries,
            persistent_max_escalations=max_escalations,
            persistent_quiet_hours_start=quiet_hours_start,
            persistent_quiet_hours_end=quiet_hours_end,
            last_message_id=message_id if delivered else None,
            last_delivery_occurrence_utc=occurrence_at,
            sent_at=now_utc if delivered else None,
        )
        session.add(reminder)
        await session.flush()
        occurrence = None
        if delivered:
            occurrence = ReminderOccurrence(
                reminder_id=reminder.id,
                occurrence_at_utc=occurrence_at,
                delivery_at_utc=occurrence_at,
                status=OccurrenceState.DELIVERED.value,
                action_revision=action_revision,
                message_id=message_id,
                delivered_at=now_utc,
            )
            session.add(occurrence)
        await session.commit()
        await session.refresh(reminder)
        if occurrence is not None:
            await session.refresh(occurrence)
        return reminder, occurrence


async def _get_reminder(session_factory, reminder_id: int) -> Reminder:
    async with session_factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        assert reminder is not None
        return reminder


def test_create_persists_canonical_mode_and_bounded_policy(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _setup(monkeypatch)
        now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            monkeypatch.setattr(
                reminder_service,
                "get_settings",
                lambda: SimpleNamespace(
                    persistent_repeat_interval_minutes=20,
                    persistent_max_deliveries=4,
                    persistent_max_escalations=3,
                    persistent_quiet_hours_start="21:00",
                    persistent_quiet_hours_end="07:00",
                ),
            )
            user = await _add_user(session_factory)
            created = await reminder_service.create_reminder(
                user=user,
                local_dt=datetime(2026, 9, 8, 14, 0),
                text="persistent task",
                mode="important",
            )

            assert created.mode == "persistent"
            assert created.persistent_interval_minutes == 20
            assert created.persistent_max_deliveries == 4
            assert created.persistent_max_escalations == 3
            assert created.persistent_quiet_hours_start == "21:00"
            assert created.persistent_quiet_hours_end == "07:00"
            assert created.persistent_delivery_count == 0
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_persistent_delivery_repeats_then_exhausts_after_restart_safe_reload(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _setup(monkeypatch)
        now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(worker, "settings", _worker_settings())
            user = await _add_user(session_factory)
            reminder, _ = await _add_persistent_reminder(
                session_factory,
                user,
                now,
                max_deliveries=2,
                max_escalations=5,
            )

            first = (await worker.claim_due_reminders(1, now_utc=now))[0]
            assert await reminder_service.set_last_message_id(
                reminder.id,
                7001,
                lease_token=first.lease_token,
                occurrence_at_utc=now,
                now_utc=now,
            )
            assert await worker.finalize_delivery_success(
                reminder.id, first.lease_token, now_utc=now
            )

            scheduled = await _get_reminder(session_factory, reminder.id)
            assert scheduled.mode == "persistent"
            assert scheduled.state == ReminderState.SCHEDULED.value
            assert scheduled.persistent_delivery_count == 1
            assert scheduled.persistent_escalation_count == 0
            assert _utc(scheduled.delivery_at_utc) == now + timedelta(minutes=15)

            second_time = now + timedelta(minutes=15)
            second = (await worker.claim_due_reminders(1, now_utc=second_time))[0]
            assert await reminder_service.set_last_message_id(
                reminder.id,
                7002,
                lease_token=second.lease_token,
                occurrence_at_utc=now,
                now_utc=second_time,
            )
            assert await worker.finalize_delivery_success(
                reminder.id, second.lease_token, now_utc=second_time
            )

            exhausted = await _get_reminder(session_factory, reminder.id)
            assert exhausted.state == ReminderState.DELIVERED.value
            assert exhausted.status == "sent"
            assert exhausted.delivery_at_utc is None
            assert exhausted.persistent_delivery_count == 2
            assert exhausted.persistent_escalation_count == 1
            assert exhausted.persistent_stop_reason == "delivery_limit"
            assert exhausted.persistent_exhausted_at is not None
            assert await worker.claim_due_reminders(1, now_utc=second_time) == []
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_persistent_claim_defers_for_quiet_hours_and_user_cooldown(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _setup(monkeypatch)
        now = datetime(2026, 9, 8, 20, 30, tzinfo=UTC)
        try:
            monkeypatch.setattr(worker, "settings", _worker_settings())
            user = await _add_user(session_factory)
            quiet, _ = await _add_persistent_reminder(session_factory, user, now)

            assert await worker.claim_due_reminders(1, now_utc=now) == []
            deferred = await _get_reminder(session_factory, quiet.id)
            assert deferred.persistent_deferred_count == 1
            assert _utc(deferred.delivery_at_utc) == datetime(2026, 9, 9, 5, 0, tzinfo=UTC)
            assert worker.worker_metrics.quiet_hours_deferred == 1

            first_time = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
            cooldown_user = await _add_user(
                session_factory,
                telegram_user_id=1002,
                chat_id=2003,
            )
            first, _ = await _add_persistent_reminder(
                session_factory,
                cooldown_user,
                first_time,
                delivery_at_utc=first_time,
                quiet_hours_start="00:00",
                quiet_hours_end="00:00",
            )
            second, _ = await _add_persistent_reminder(
                session_factory,
                cooldown_user,
                first_time,
                delivery_at_utc=first_time,
                quiet_hours_start="00:00",
                quiet_hours_end="00:00",
            )
            monkeypatch.setattr(
                worker,
                "settings",
                _worker_settings(persistent_user_cooldown_minutes=1),
            )
            claimed = await worker.claim_due_reminders(2, now_utc=first_time)
            assert [item.id for item in claimed] == [first.id]
            cooldown_deferred = await _get_reminder(session_factory, second.id)
            assert _utc(cooldown_deferred.delivery_at_utc) == first_time + timedelta(minutes=1)
            assert cooldown_deferred.persistent_deferred_count == 1
            assert worker.worker_metrics.user_cooldown_deferred == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_snooze_stops_one_off_persistent_loop_and_disable_is_owner_revision_guarded(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _setup(monkeypatch)
        now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            monkeypatch.setattr(worker, "settings", _worker_settings())
            user = await _add_user(session_factory)
            snoozed, occurrence = await _add_persistent_reminder(
                session_factory,
                user,
                now,
                message_id=8001,
                delivered_occurrence=True,
            )
            assert occurrence is not None

            assert await reminder_service.disable_persistent_reminder(
                user,
                snoozed.id,
                expected_revision=0,
                expected_occurrence_id=occurrence.id,
                expected_occurrence_at_utc=now,
                expected_message_id=8001,
            )
            disabled = await _get_reminder(session_factory, snoozed.id)
            assert disabled.mode == "normal"
            assert disabled.state == ReminderState.DELIVERED.value
            assert disabled.persistent_stop_reason == "user_disabled"
            assert disabled.action_revision == 1
            assert not await reminder_service.disable_persistent_reminder(
                user,
                snoozed.id,
                expected_revision=0,
                expected_occurrence_id=occurrence.id,
                expected_occurrence_at_utc=now,
                expected_message_id=8001,
            )

            snoozed_again, occurrence_again = await _add_persistent_reminder(
                session_factory,
                user,
                now + timedelta(hours=1),
                message_id=8002,
                delivered_occurrence=True,
            )
            assert occurrence_again is not None
            target = now + timedelta(minutes=10)
            assert (
                await reminder_service.snooze_reminder(
                    user,
                    snoozed_again.id,
                    expected_revision=0,
                    expected_occurrence_id=occurrence_again.id,
                    expected_occurrence_at_utc=now + timedelta(hours=1),
                    expected_message_id=8002,
                    target_at_utc=target,
                )
                is not None
            )
            ordinary = await _get_reminder(session_factory, snoozed_again.id)
            assert ordinary.mode == "normal"
            assert ordinary.persistent_stop_reason == "user_snoozed"
            assert _utc(ordinary.delivery_at_utc) == target

            claim = (await worker.claim_due_reminders(1, now_utc=target))[0]
            assert await reminder_service.set_last_message_id(
                ordinary.id,
                8003,
                lease_token=claim.lease_token,
                occurrence_at_utc=ordinary.remind_at_utc,
                now_utc=target,
            )
            assert await worker.finalize_delivery_success(
                ordinary.id, claim.lease_token, now_utc=target
            )
            delivered_once = await _get_reminder(session_factory, ordinary.id)
            assert delivered_once.mode == "normal"
            assert delivered_once.state == ReminderState.DELIVERED.value
            assert delivered_once.delivery_at_utc is None
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_persistent_controls_and_rendering_explain_how_to_stop() -> None:
    reminder = Reminder(
        id=42,
        text="important test reminder",
        remind_at_utc=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
        schedule_timezone="Europe/Moscow",
        mode="persistent",
        persistent_interval_minutes=15,
        persistent_max_deliveries=3,
        persistent_max_escalations=5,
        persistent_delivery_count=1,
    )

    markup = worker.reminder_actions_kb(
        reminder.id,
        occurrence_id=7,
        revision=2,
        state=ReminderState.DELIVERED.value,
        mode=reminder.mode,
    )
    actions = {
        parse_callback(button.callback_data).action
        for row in markup.inline_keyboard
        for button in row
    }
    assert CallbackAction.DISABLE_PERSISTENT in actions
    delivery = worker._delivery_text(reminder, None)
    assert "Важное напоминание" in delivery
    assert "Выключить повторы" in delivery
