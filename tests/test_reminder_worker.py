import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import RecurrenceType, Reminder, User
from app.services import reminder_service
from app.workers import reminder_worker as worker


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _worker_settings(**overrides):
    values = {
        "worker_batch_size": 100,
        "worker_poll_interval_seconds": 0.01,
        "worker_lease_duration_seconds": 60,
        "worker_send_timeout_seconds": 1,
        "worker_retry_base_seconds": 10,
        "worker_retry_max_seconds": 300,
        "worker_max_attempts": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _sqlite_setup(monkeypatch):
    async def setup():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        connection = await engine.connect()
        await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(worker, "SessionLocal", session_factory)
        monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
        monkeypatch.setattr(worker, "worker_metrics", worker.WorkerMetrics())
        return engine, connection, session_factory

    return setup


async def _insert_reminder(
    session_factory,
    now_utc: datetime,
    *,
    status: str = "pending",
    recurrence_type: str = RecurrenceType.NONE.value,
    lease_until: datetime | None = None,
    lease_token: str | None = None,
    attempt_count: int = 0,
) -> int:
    async with session_factory() as session:
        user = User(telegram_user_id=1001, chat_id=2002, timezone="Europe/Moscow")
        session.add(user)
        await session.flush()
        reminder = Reminder(
            user_id=user.id,
            chat_id=user.chat_id,
            text="test reminder",
            remind_at_utc=now_utc,
            delivery_at_utc=now_utc,
            schedule_timezone="Europe/Moscow",
            status=status,
            recurrence_type=recurrence_type,
            recurrence_interval=1,
            lease_until=lease_until,
            lease_token=lease_token,
            processing_started_at=now_utc - timedelta(minutes=5)
            if status == "processing"
            else None,
            attempt_count=attempt_count,
        )
        session.add(reminder)
        await session.commit()
        return reminder.id


async def _get_reminder(session_factory, reminder_id: int) -> Reminder:
    async with session_factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        assert reminder is not None
        return reminder


def test_claim_recovery_and_stale_token_finalization(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(worker, "settings", _worker_settings())
            reminder_id = await _insert_reminder(session_factory, now)

            first_claim = await worker.claim_due_reminders(1, now_utc=now)
            assert len(first_claim) == 1
            first_token = first_claim[0].lease_token
            assert first_token
            assert first_claim[0].attempt_count == 1
            assert (
                await worker.finalize_delivery_success(
                    reminder_id,
                    first_token,
                    now_utc=now + timedelta(seconds=61),
                )
                is False
            )

            async with session_factory() as session:
                await session.execute(
                    update(Reminder)
                    .where(Reminder.id == reminder_id)
                    .values(lease_until=now - timedelta(seconds=1))
                )
                await session.commit()

            second_claim = await worker.claim_due_reminders(1, now_utc=now)
            assert len(second_claim) == 1
            second_token = second_claim[0].lease_token
            assert second_token and second_token != first_token
            assert second_claim[0].attempt_count == 2

            assert (
                await worker.finalize_delivery_success(reminder_id, first_token, now_utc=now)
                is False
            )
            assert (
                await worker.finalize_delivery_success(reminder_id, second_token, now_utc=now)
                is True
            )

            saved = await _get_reminder(session_factory, reminder_id)
            assert saved.status == "sent"
            assert saved.lease_token is None
            assert saved.lease_until is None
            assert saved.processing_started_at is None
            assert saved.attempt_count == 0
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_prelease_processing_row_is_reclaimable(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(worker, "settings", _worker_settings())
            reminder_id = await _insert_reminder(
                session_factory,
                now,
                status="processing",
                lease_until=None,
                lease_token=None,
                attempt_count=0,
            )

            claimed = await worker.claim_due_reminders(1, now_utc=now)

            assert [reminder.id for reminder in claimed] == [reminder_id]
            assert claimed[0].lease_token
            assert worker.worker_metrics.recovered == 1
            assert worker.worker_metrics.expired_leases == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_retry_backoff_retry_after_and_max_attempts(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(
                worker,
                "settings",
                _worker_settings(worker_max_attempts=2, worker_retry_base_seconds=10),
            )
            reminder_id = await _insert_reminder(session_factory, now)
            first_claim = (await worker.claim_due_reminders(1, now_utc=now))[0]

            retry_failure = worker.DeliveryFailure(
                kind=worker.DeliveryErrorKind.TRANSIENT,
                error_type="TelegramRetryAfter",
                retry_after_seconds=42,
            )
            assert await worker.finalize_delivery_failure(
                reminder_id,
                first_claim.lease_token,
                retry_failure,
                now_utc=now,
            )
            saved = await _get_reminder(session_factory, reminder_id)
            assert saved.status == "pending"
            assert saved.retry_count == 1
            assert _utc(saved.next_retry_at) == now + timedelta(seconds=42)
            assert await worker.claim_due_reminders(1, now_utc=now + timedelta(seconds=41)) == []

            second_claim = (
                await worker.claim_due_reminders(1, now_utc=now + timedelta(seconds=42))
            )[0]
            assert second_claim.attempt_count == 2
            assert await worker.finalize_delivery_failure(
                reminder_id,
                second_claim.lease_token,
                worker.DeliveryFailure(
                    kind=worker.DeliveryErrorKind.TRANSIENT,
                    error_type="TimeoutError",
                ),
                now_utc=now + timedelta(seconds=42),
            )

            saved = await _get_reminder(session_factory, reminder_id)
            assert saved.status == "failed"
            assert saved.next_retry_at is None
            assert saved.lease_token is None
            assert worker.worker_metrics.retried == 1
            assert worker.worker_metrics.failed == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_terminal_failure_is_not_retried(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(worker, "settings", _worker_settings())
            reminder_id = await _insert_reminder(session_factory, now)
            claimed = (await worker.claim_due_reminders(1, now_utc=now))[0]

            assert await worker.finalize_delivery_failure(
                reminder_id,
                claimed.lease_token,
                worker.DeliveryFailure(
                    kind=worker.DeliveryErrorKind.TERMINAL,
                    error_type="TelegramBadRequest",
                ),
                now_utc=now,
            )

            saved = await _get_reminder(session_factory, reminder_id)
            assert saved.status == "failed"
            assert saved.retry_count == 1
            assert saved.next_retry_at is None
            assert saved.lease_token is None
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_delivery_error_classification_is_bounded_and_secret_safe() -> None:
    bad_request_type = type("TelegramBadRequest", (Exception,), {})
    retry_after_type = type("TelegramRetryAfter", (Exception,), {})
    retry_after = retry_after_type()
    retry_after.retry_after = 37

    terminal = worker.classify_delivery_error(bad_request_type("bot123:secret"))
    retry = worker.classify_delivery_error(retry_after)
    timeout = worker.classify_delivery_error(TimeoutError("private provider details"))

    assert terminal.kind == worker.DeliveryErrorKind.TERMINAL
    assert retry.kind == worker.DeliveryErrorKind.TRANSIENT
    assert retry.retry_after_seconds == 37
    assert timeout.kind == worker.DeliveryErrorKind.TRANSIENT
    assert "secret" not in worker._safe_failure_text(terminal)
    assert (
        worker.retry_delay_seconds(1, base_seconds=10, max_seconds=30, retry_after_seconds=90) == 30
    )


def test_process_success_is_lease_guarded_and_finalizes_one_off(monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.calls = 0

        async def send_message(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(message_id=9001)

    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(worker, "settings", _worker_settings())
            monkeypatch.setattr(worker, "utc_now", lambda: now)
            reminder_id = await _insert_reminder(session_factory, now)
            bot = FakeBot()

            assert await worker.process_due_reminders(bot) == 1
            assert bot.calls == 1
            saved = await _get_reminder(session_factory, reminder_id)
            assert saved.status == "sent"
            assert saved.last_message_id == 9001
            assert _utc(saved.last_delivery_occurrence_utc) == now
            assert saved.lease_token is None
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_stale_and_duplicate_callback_actions_are_noops(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            reminder_id = await _insert_reminder(
                session_factory,
                now + timedelta(hours=1),
                recurrence_type=RecurrenceType.HOURLY.value,
            )
            async with session_factory() as session:
                reminder = await session.get(Reminder, reminder_id)
                assert reminder is not None
                reminder.last_message_id = 7002
                reminder.last_delivery_occurrence_utc = now
                await session.commit()
                user = await session.get(User, reminder.user_id)
                assert user is not None

            assert (
                await reminder_service.cancel_reminder(
                    user=user,
                    reminder_id=reminder_id,
                    expected_message_id=7001,
                )
                is False
            )
            assert (
                await reminder_service.snooze_reminder(
                    user=user,
                    reminder_id=reminder_id,
                    expected_message_id=7001,
                )
                is None
            )

            saved = await _get_reminder(session_factory, reminder_id)
            assert saved.status == "pending"
            assert _utc(saved.remind_at_utc) == now + timedelta(hours=1)
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_one_off_latest_delivery_actions_are_atomic_and_consumed(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            reminder_id = await _insert_reminder(session_factory, now)
            async with session_factory() as session:
                reminder = await session.get(Reminder, reminder_id)
                assert reminder is not None
                reminder.status = "sent"
                reminder.last_message_id = 7001
                reminder.last_delivery_occurrence_utc = now
                user = await session.get(User, reminder.user_id)
                assert user is not None
                await session.commit()

            snoozed = await reminder_service.snooze_reminder(
                user=user,
                reminder_id=reminder_id,
                expected_message_id=7001,
            )
            assert snoozed is not None
            assert snoozed.status == "pending"
            assert _utc(snoozed.delivery_at_utc) == now + timedelta(minutes=10)
            assert snoozed.last_message_id is None
            assert snoozed.last_delivery_occurrence_utc is None

            assert (
                await reminder_service.snooze_reminder(
                    user=user,
                    reminder_id=reminder_id,
                    expected_message_id=7001,
                )
                is None
            )

            async with session_factory() as session:
                reminder = await session.get(Reminder, reminder_id)
                assert reminder is not None
                reminder.status = "sent"
                reminder.last_message_id = 7002
                reminder.last_delivery_occurrence_utc = reminder.remind_at_utc
                await session.commit()

            assert (
                await reminder_service.cancel_reminder(
                    user=user,
                    reminder_id=reminder_id,
                    expected_message_id=7002,
                )
                is True
            )
            assert (
                await reminder_service.cancel_reminder(
                    user=user,
                    reminder_id=reminder_id,
                    expected_message_id=7002,
                )
                is False
            )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_recurring_delivery_keyboard_exposes_delete_only() -> None:
    markup = worker.reminder_actions_kb(42, include_snooze=False)

    assert len(markup.inline_keyboard) == 1
    assert [button.text for button in markup.inline_keyboard[0]] == ["Удалить"]


def test_cancel_after_claim_prevents_external_send(monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.calls = 0

        async def send_message(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(message_id=9200)

    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(worker, "settings", _worker_settings())
            monkeypatch.setattr(worker, "utc_now", lambda: now)
            reminder_id = await _insert_reminder(session_factory, now)
            claimed = (await worker.claim_due_reminders(1, now_utc=now))[0]
            async with session_factory() as session:
                user = await session.get(User, claimed.user_id)
                assert user is not None

            assert await reminder_service.cancel_reminder(user, reminder_id)
            bot = FakeBot()
            assert not await worker.process_claimed_reminder(bot, claimed)
            assert bot.calls == 0
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_cancelled_send_task_leaves_a_reclaimable_lease(monkeypatch) -> None:
    class BlockingBot:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.calls = 0

        async def send_message(self, **kwargs):
            self.calls += 1
            self.started.set()
            await asyncio.Future()

    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime.now(UTC)
        try:
            monkeypatch.setattr(worker, "settings", _worker_settings())
            monkeypatch.setattr(worker, "utc_now", lambda: now)
            reminder_id = await _insert_reminder(session_factory, now)
            claimed = (await worker.claim_due_reminders(1, now_utc=now))[0]
            bot = BlockingBot()
            task = asyncio.create_task(worker.process_claimed_reminder(bot, claimed))
            await asyncio.wait_for(bot.started.wait(), timeout=1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled send task unexpectedly completed")

            saved = await _get_reminder(session_factory, reminder_id)
            assert saved.status == "processing"
            assert saved.lease_token == claimed.lease_token
            assert _utc(saved.lease_until) > now

            async with session_factory() as session:
                await session.execute(
                    update(Reminder)
                    .where(Reminder.id == reminder_id)
                    .values(lease_until=now - timedelta(seconds=1))
                )
                await session.commit()
            assert len(await worker.claim_due_reminders(1, now_utc=now)) == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_worker_loop_stops_without_another_claim(monkeypatch) -> None:
    async def scenario() -> None:
        stop_event = asyncio.Event()
        calls = 0

        async def fake_process_due_reminders(bot, *, stop_event=None):
            nonlocal calls
            calls += 1
            assert stop_event is not None
            stop_event.set()
            return 0

        monkeypatch.setattr(worker, "settings", _worker_settings())
        monkeypatch.setattr(worker, "process_due_reminders", fake_process_due_reminders)

        await asyncio.wait_for(worker.reminder_loop(object(), stop_event=stop_event), timeout=1)
        assert calls == 1

    asyncio.run(scenario())
