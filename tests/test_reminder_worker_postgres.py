import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import RecurrenceType, Reminder, User
from app.services import reminder_service
from app.workers import reminder_worker as worker

POSTGRES_URL = os.environ.get("REMINDER_BOT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set REMINDER_BOT_TEST_DATABASE_URL to run PostgreSQL integration tests",
)


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


async def _with_postgres(monkeypatch, scenario) -> None:
    pytest.importorskip("asyncpg")
    assert POSTGRES_URL is not None
    engine = create_async_engine(POSTGRES_URL, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("TRUNCATE reminders, users RESTART IDENTITY CASCADE"))

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(worker, "SessionLocal", session_factory)
        monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
        monkeypatch.setattr(worker, "worker_metrics", worker.WorkerMetrics())
        await scenario(session_factory)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE reminders, users RESTART IDENTITY CASCADE"))
        await engine.dispose()


async def _insert_reminder(
    session_factory,
    now_utc: datetime,
    *,
    status: str = "pending",
    recurrence_type: str = RecurrenceType.NONE.value,
    attempt_count: int = 0,
) -> int:
    async with session_factory() as session:
        user = User(telegram_user_id=5001, chat_id=6002, timezone="Europe/Moscow")
        session.add(user)
        await session.flush()
        reminder = Reminder(
            user_id=user.id,
            chat_id=user.chat_id,
            text="postgres integration reminder",
            remind_at_utc=now_utc,
            delivery_at_utc=now_utc,
            schedule_timezone="Europe/Moscow",
            status=status,
            recurrence_type=recurrence_type,
            recurrence_interval=1,
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


def test_postgres_workers_cannot_claim_one_occurrence_concurrently(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        reminder_id = await _insert_reminder(session_factory, now)

        first, second = await asyncio.gather(
            worker.claim_due_reminders(1, now_utc=now),
            worker.claim_due_reminders(1, now_utc=now),
        )

        assert sorted([len(first), len(second)]) == [0, 1]
        claimed = first or second
        assert claimed[0].id == reminder_id
        assert claimed[0].lease_token
        saved = await _get_reminder(session_factory, reminder_id)
        assert saved.status == "processing"
        assert saved.lease_token == claimed[0].lease_token

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_expired_lease_recovery_rejects_stale_owner(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        monkeypatch.setattr(worker, "settings", _worker_settings())
        reminder_id = await _insert_reminder(session_factory, now)
        first = (await worker.claim_due_reminders(1, now_utc=now))[0]

        async with session_factory() as session:
            await session.execute(
                update(Reminder)
                .where(Reminder.id == reminder_id)
                .values(lease_until=now - timedelta(seconds=1))
            )
            await session.commit()

        recovered = (await worker.claim_due_reminders(1, now_utc=now))[0]
        assert recovered.lease_token != first.lease_token
        assert recovered.attempt_count == 2
        assert (
            await worker.finalize_delivery_success(
                reminder_id,
                first.lease_token,
                now_utc=now,
            )
            is False
        )
        assert await worker.finalize_delivery_success(
            reminder_id, recovered.lease_token, now_utc=now
        )
        saved = await _get_reminder(session_factory, reminder_id)
        assert saved.status == "sent"

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_duplicate_success_finalization_advances_recurring_once(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        monkeypatch.setattr(worker, "settings", _worker_settings())
        reminder_id = await _insert_reminder(
            session_factory,
            now - timedelta(hours=1),
            recurrence_type=RecurrenceType.HOURLY.value,
        )
        claimed = (await worker.claim_due_reminders(1, now_utc=now))[0]

        results = await asyncio.gather(
            worker.finalize_delivery_success(reminder_id, claimed.lease_token, now_utc=now),
            worker.finalize_delivery_success(reminder_id, claimed.lease_token, now_utc=now),
        )

        assert sorted(results) == [False, True]
        saved = await _get_reminder(session_factory, reminder_id)
        assert saved.status == "pending"
        assert saved.remind_at_utc == now + timedelta(hours=1)
        assert saved.attempt_count == 0

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_duplicate_callback_cancel_is_idempotent(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        reminder_id = await _insert_reminder(session_factory, now)
        async with session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            assert reminder is not None
            reminder.last_message_id = 7001
            reminder.last_delivery_occurrence_utc = now
            user = await session.get(User, reminder.user_id)
            assert user is not None
            await session.commit()

        results = await asyncio.gather(
            reminder_service.cancel_reminder(
                user=user,
                reminder_id=reminder_id,
                expected_message_id=7001,
            ),
            reminder_service.cancel_reminder(
                user=user,
                reminder_id=reminder_id,
                expected_message_id=7001,
            ),
        )

        assert sorted(results) == [False, True]
        async with session_factory() as session:
            assert await session.get(Reminder, reminder_id) is None

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_send_before_finalization_leaves_recoverable_at_least_once_boundary(
    monkeypatch,
) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.calls = 0

        async def send_message(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(message_id=9100 + self.calls)

    async def scenario(session_factory) -> None:
        now = datetime.now(UTC) - timedelta(seconds=1)
        monkeypatch.setattr(
            worker,
            "settings",
            _worker_settings(worker_lease_duration_seconds=30),
        )
        reminder_id = await _insert_reminder(session_factory, now)
        bot = FakeBot()
        original_set_last_message_id = worker.set_last_message_id

        async def fail_after_external_send(*args, **kwargs):
            raise RuntimeError("simulated database finalization interruption")

        monkeypatch.setattr(worker, "set_last_message_id", fail_after_external_send)
        assert await worker.process_due_reminders(bot) == 0
        saved = await _get_reminder(session_factory, reminder_id)
        assert bot.calls == 1
        assert saved.status == "processing"
        assert saved.lease_token is not None

        async with session_factory() as session:
            await session.execute(
                update(Reminder)
                .where(Reminder.id == reminder_id)
                .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

        monkeypatch.setattr(worker, "set_last_message_id", original_set_last_message_id)
        assert await worker.process_due_reminders(bot) == 1
        assert bot.calls == 2
        saved = await _get_reminder(session_factory, reminder_id)
        assert saved.status == "sent"

    asyncio.run(_with_postgres(monkeypatch, scenario))
