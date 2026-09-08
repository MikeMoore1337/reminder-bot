import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import (
    ActionDraft,
    DeadlinePlanState,
    DeadlineStepState,
    DigestDeliveryState,
    OccurrenceState,
    RecurrenceType,
    Reminder,
    ReminderClarification,
    ReminderContext,
    ReminderDeadlinePlan,
    ReminderDeadlineStep,
    ReminderDigestDelivery,
    ReminderKind,
    ReminderOccurrence,
    ReminderState,
    User,
    VoiceReminderDraft,
)
from app.services import (
    adaptive_service,
    clarification_service,
    deadline_service,
    message_context,
    reminder_service,
    voice_service,
)
from app.services.message_context import (
    ContextKind,
    MessageContextSnapshot,
    cleanup_expired_reminder_contexts,
    get_context_for_delivery,
)
from app.services.reminder_parser import (
    ClarificationRequest,
    DeadlineRequest,
    ParsedReminder,
    parse_clarification_answer,
    parse_reminder_input,
)
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
        "worker_lease_safety_margin_seconds": 10,
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
        monkeypatch.setattr(adaptive_service, "SessionLocal", session_factory)
        monkeypatch.setattr(deadline_service, "SessionLocal", session_factory)
        monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
        monkeypatch.setattr(clarification_service, "SessionLocal", session_factory)
        monkeypatch.setattr(message_context, "SessionLocal", session_factory)
        monkeypatch.setattr(voice_service, "SessionLocal", session_factory)
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
    telegram_user_id: int = 5001,
) -> int:
    async with session_factory() as session:
        user = User(
            telegram_user_id=telegram_user_id,
            chat_id=6002,
            timezone="Europe/Moscow",
        )
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


async def _insert_user(session_factory, *, telegram_user_id: int, chat_id: int) -> User:
    async with session_factory() as session:
        user = User(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            timezone="Europe/Moscow",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _get_reminder(session_factory, reminder_id: int) -> Reminder:
    async with session_factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        assert reminder is not None
        return reminder


async def _insert_batch_reminders(session_factory, now_utc: datetime, count: int) -> list[int]:
    async with session_factory() as session:
        user = User(telegram_user_id=5001, chat_id=6002, timezone="Europe/Moscow")
        session.add(user)
        await session.flush()
        reminders = [
            Reminder(
                user_id=user.id,
                chat_id=user.chat_id,
                text=f"postgres batch reminder {index}",
                remind_at_utc=now_utc,
                delivery_at_utc=now_utc,
                schedule_timezone="Europe/Moscow",
                status="pending",
                recurrence_type=RecurrenceType.NONE.value,
                recurrence_interval=1,
            )
            for index in range(count)
        ]
        session.add_all(reminders)
        await session.commit()
        return [reminder.id for reminder in reminders]


def test_postgres_digest_claim_is_idempotent_under_concurrency(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 10, 7, 0, tzinfo=UTC)
        async with session_factory() as session:
            session.add(
                User(
                    telegram_user_id=9701,
                    chat_id=9702,
                    timezone="Europe/Moscow",
                    digests_enabled=True,
                )
            )
            await session.commit()

        claimed_batches = await asyncio.gather(
            adaptive_service.claim_due_digests(1, now_utc=now),
            adaptive_service.claim_due_digests(1, now_utc=now),
        )
        assert sorted(len(batch) for batch in claimed_batches) == [0, 1]
        async with session_factory() as session:
            deliveries = list((await session.scalars(select(ReminderDigestDelivery))).all())
            assert len(deliveries) == 1
            assert deliveries[0].state == DigestDeliveryState.PROCESSING.value
            assert deliveries[0].lease_token

    asyncio.run(_with_postgres(monkeypatch, scenario))


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


def test_postgres_deadline_claim_and_progress_are_concurrency_safe(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 7, 0, tzinfo=UTC)
        user = await _insert_user(session_factory, telegram_user_id=5020, chat_id=6020)
        request = DeadlineRequest(
            local_dt=datetime(2026, 9, 7, 12, 0),
            text="проверить оплату",
        )
        draft = await deadline_service.create_deadline_draft(
            user,
            request,
            raw_text="/deadline ...",
            now_utc=now,
        )
        reminder = await deadline_service.confirm_deadline_draft(
            user,
            draft.id,
            expected_revision=draft.action_revision,
            expected_message_id=8100,
            now_utc=now,
        )
        assert reminder is not None
        assert reminder.kind == ReminderKind.DEADLINE.value
        first_at = reminder.remind_at_utc

        first, second = await asyncio.gather(
            worker.claim_due_reminders(1, now_utc=first_at),
            worker.claim_due_reminders(1, now_utc=first_at),
        )
        assert sorted([len(first), len(second)]) == [0, 1]
        claimed = first or second
        assert claimed[0].lease_token
        claimed_reminder = claimed[0]

        occurrence_id = await reminder_service.prepare_delivery_occurrence(
            claimed_reminder.id,
            claimed_reminder.lease_token,
            now_utc=first_at,
        )
        assert occurrence_id is not None
        assert await reminder_service.set_last_message_id(
            claimed_reminder.id,
            8101,
            lease_token=claimed_reminder.lease_token,
            occurrence_at_utc=first_at,
            now_utc=first_at,
        )
        assert await worker.finalize_delivery_success(
            claimed_reminder.id,
            claimed_reminder.lease_token,
            now_utc=first_at,
        )

        async with session_factory() as session:
            saved = await session.get(Reminder, claimed_reminder.id)
            assert saved is not None
            assert saved.state == ReminderState.SCHEDULED.value
            plan = await session.scalar(
                select(ReminderDeadlinePlan).where(
                    ReminderDeadlinePlan.reminder_id == claimed_reminder.id
                )
            )
            assert plan is not None
            assert plan.state == DeadlinePlanState.ACTIVE.value
            steps = list(
                (
                    await session.scalars(
                        select(ReminderDeadlineStep)
                        .where(ReminderDeadlineStep.plan_id == plan.id)
                        .order_by(ReminderDeadlineStep.sequence)
                    )
                ).all()
            )
            assert steps[0].state == DeadlineStepState.SKIPPED.value
            assert steps[1].state == DeadlineStepState.DELIVERED.value
            assert plan.current_step_sequence == steps[2].sequence

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_double_done_acknowledges_one_delivery(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        reminder_id = await _insert_reminder(session_factory, now)
        async with session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            assert reminder is not None
            reminder.status = "sent"
            reminder.state = ReminderState.DELIVERED.value
            reminder.action_revision = 5
            reminder.last_message_id = 7101
            reminder.last_delivery_occurrence_utc = now
            occurrence = ReminderOccurrence(
                reminder_id=reminder_id,
                occurrence_at_utc=now,
                delivery_at_utc=now,
                status=OccurrenceState.DELIVERED.value,
                action_revision=5,
                message_id=7101,
                delivered_at=now,
            )
            session.add(occurrence)
            user = await session.get(User, reminder.user_id)
            assert user is not None
            await session.commit()

        results = await asyncio.gather(
            reminder_service.complete_reminder(
                user,
                reminder_id,
                expected_revision=5,
                expected_occurrence_id=occurrence.id,
                expected_message_id=7101,
            ),
            reminder_service.complete_reminder(
                user,
                reminder_id,
                expected_revision=5,
                expected_occurrence_id=occurrence.id,
                expected_message_id=7101,
            ),
        )

        assert sorted(results) == [False, True]
        saved = await _get_reminder(session_factory, reminder_id)
        assert saved.state == ReminderState.COMPLETED.value

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_concurrent_clarification_answers_create_one_reminder(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now_utc = datetime(2026, 9, 7, 7, 0, tzinfo=UTC)
        now_local = datetime(2026, 9, 7, 10, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        async with session_factory() as session:
            user = User(
                telegram_user_id=5010,
                chat_id=6010,
                timezone="Europe/Moscow",
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

        request = parse_reminder_input("напомни после обеда позвонить", now_local)
        assert isinstance(request, ClarificationRequest)
        clarification = await clarification_service.create_clarification(
            user,
            request,
            now_utc=now_utc,
        )
        parsed = parse_clarification_answer(
            clarification.raw_text,
            "14:00",
            now_local=now_local,
        )
        assert isinstance(parsed, ParsedReminder)

        results = await asyncio.gather(
            clarification_service.consume_clarification_and_create_reminder(
                user,
                clarification.id,
                clarification.raw_text,
                parsed,
                now_utc=now_utc,
            ),
            clarification_service.consume_clarification_and_create_reminder(
                user,
                clarification.id,
                clarification.raw_text,
                parsed,
                now_utc=now_utc,
            ),
        )

        assert sorted(result is not None for result in results) == [False, True]
        async with session_factory() as session:
            reminders = list(
                (await session.scalars(select(Reminder).where(Reminder.user_id == user.id))).all()
            )
            clarifications = list(
                (
                    await session.scalars(
                        select(ReminderClarification).where(
                            ReminderClarification.user_id == user.id,
                        )
                    )
                ).all()
            )
        assert len(reminders) == 1
        assert reminders[0].text == "позвонить"
        assert clarifications == []

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_message_context_is_scoped_and_ttl_cleanup_keeps_reminder(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now_utc = datetime(2026, 9, 7, 7, 0, tzinfo=UTC)
        async with session_factory() as session:
            owner = User(
                telegram_user_id=8010,
                chat_id=9010,
                timezone="Europe/Moscow",
            )
            other = User(
                telegram_user_id=8011,
                chat_id=9011,
                timezone="Europe/Moscow",
            )
            session.add_all([owner, other])
            await session.flush()
            reminder = await reminder_service.create_reminder_in_session(
                session,
                owner,
                datetime(2026, 9, 10, 10, 0),
                "Прочитать источник",
                now_utc=now_utc,
                context=MessageContextSnapshot(
                    kind=ContextKind.FORWARDED.value,
                    source_chat_id=-10077,
                    source_message_id=44,
                    source_text="Приватный источник",
                ),
            )
            await session.commit()

        restored = await get_context_for_delivery(
            reminder.id,
            owner.id,
            owner.chat_id,
            now_utc=now_utc,
        )
        assert restored is not None
        assert restored.source_text == "Приватный источник"
        assert (
            await get_context_for_delivery(
                reminder.id,
                other.id,
                other.chat_id,
                now_utc=now_utc,
            )
            is None
        )

        assert (
            await cleanup_expired_reminder_contexts(
                now_utc=now_utc + timedelta(days=31),
            )
            == 1
        )
        async with session_factory() as session:
            assert await session.get(Reminder, reminder.id) is not None
            assert await session.scalar(select(ReminderContext)) is None

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_double_recurring_snooze_creates_one_child(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        source_at = now - timedelta(hours=1)
        reminder_id = await _insert_reminder(
            session_factory,
            now + timedelta(hours=1),
            recurrence_type=RecurrenceType.HOURLY.value,
        )
        async with session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            assert reminder is not None
            reminder.action_revision = 4
            reminder.last_message_id = 7201
            reminder.last_delivery_occurrence_utc = source_at
            occurrence = ReminderOccurrence(
                reminder_id=reminder_id,
                occurrence_at_utc=source_at,
                delivery_at_utc=source_at,
                status=OccurrenceState.DELIVERED.value,
                action_revision=4,
                message_id=7201,
                delivered_at=now,
            )
            session.add(occurrence)
            user = await session.get(User, reminder.user_id)
            assert user is not None
            await session.commit()

        monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
        target = now + timedelta(minutes=20)
        results = await asyncio.gather(
            reminder_service.snooze_reminder(
                user,
                reminder_id,
                expected_revision=4,
                expected_occurrence_id=occurrence.id,
                expected_message_id=7201,
                target_at_utc=target,
            ),
            reminder_service.snooze_reminder(
                user,
                reminder_id,
                expected_revision=4,
                expected_occurrence_id=occurrence.id,
                expected_message_id=7201,
                target_at_utc=target,
            ),
        )

        assert sorted(result is not None for result in results) == [False, True]
        async with session_factory() as session:
            children = list(
                (
                    await session.scalars(
                        select(Reminder).where(Reminder.parent_reminder_id == reminder_id)
                    )
                ).all()
            )
            assert len(children) == 1
            assert children[0].state == ReminderState.SNOOZED.value

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
            reminder.status = "sent"
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
            saved = await session.get(Reminder, reminder_id)
            assert saved is not None
            assert saved.state == "cancelled"
            assert saved.status == "sent"

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_batch_renewal_skips_reclaimed_item_before_send(monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def send_message(self, *, chat_id, text, reply_markup):
            del chat_id, text, reply_markup
            self.calls.append(len(self.calls) + 1)
            return SimpleNamespace(message_id=9300 + len(self.calls))

    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        monkeypatch.setattr(worker, "settings", _worker_settings())
        monkeypatch.setattr(worker, "utc_now", lambda: now)
        ids = await _insert_batch_reminders(session_factory, now, 2)
        first_owner = await worker.claim_due_reminders(2, now_utc=now)
        assert [reminder.id for reminder in first_owner] == ids

        async with session_factory() as session:
            await session.execute(
                update(Reminder)
                .where(Reminder.id == ids[1])
                .values(lease_until=now - timedelta(seconds=1))
            )
            await session.commit()

        second_owner = await worker.claim_due_reminders(1, now_utc=now)
        assert len(second_owner) == 1
        assert second_owner[0].id == ids[1]
        assert second_owner[0].lease_token != first_owner[1].lease_token

        first_bot = FakeBot()
        assert await worker.process_claimed_reminder(first_bot, first_owner[0])
        assert not await worker.process_claimed_reminder(first_bot, first_owner[1])
        assert first_bot.calls == [1]

        second_bot = FakeBot()
        assert await worker.process_claimed_reminder(second_bot, second_owner[0])
        assert second_bot.calls == [1]

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_cancel_after_claim_prevents_stale_send(monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.calls = 0

        async def send_message(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(message_id=9400)

    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
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

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_snooze_and_claim_race_preserves_single_owner(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime.now(UTC) - timedelta(seconds=1)
        monkeypatch.setattr(worker, "settings", _worker_settings())
        monkeypatch.setattr(worker, "utc_now", lambda: now)
        monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
        reminder_id = await _insert_reminder(session_factory, now)
        async with session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            assert reminder is not None
            user = await session.get(User, reminder.user_id)
            assert user is not None

        snoozed, claimed = await asyncio.gather(
            reminder_service.snooze_reminder(user, reminder_id),
            worker.claim_due_reminders(1, now_utc=now),
        )
        saved = await _get_reminder(session_factory, reminder_id)
        if snoozed is not None:
            assert claimed == []
            assert saved.status == "pending"
            assert saved.lease_token is None
            assert saved.delivery_at_utc == now + timedelta(minutes=10)
        else:
            assert len(claimed) == 1
            assert saved.status == "processing"
            assert saved.lease_token == claimed[0].lease_token

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_snooze_cannot_overwrite_processing_during_finalization(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        monkeypatch.setattr(worker, "settings", _worker_settings())
        monkeypatch.setattr(worker, "utc_now", lambda: now)
        monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
        reminder_id = await _insert_reminder(session_factory, now)
        claimed = (await worker.claim_due_reminders(1, now_utc=now))[0]
        async with session_factory() as session:
            user = await session.get(User, claimed.user_id)
            assert user is not None

        snoozed, finalized = await asyncio.gather(
            reminder_service.snooze_reminder(
                user,
                reminder_id,
                expected_message_id=9500,
            ),
            worker.finalize_delivery_success(reminder_id, claimed.lease_token, now_utc=now),
        )
        assert snoozed is None
        assert finalized is True
        saved = await _get_reminder(session_factory, reminder_id)
        assert saved.status == "sent"
        assert saved.lease_token is None

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_callback_delete_and_claim_race_does_not_clear_foreign_lease(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime.now(UTC) - timedelta(seconds=1)
        monkeypatch.setattr(worker, "settings", _worker_settings())
        monkeypatch.setattr(worker, "utc_now", lambda: now)
        reminder_id = await _insert_reminder(session_factory, now)
        async with session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            assert reminder is not None
            reminder.last_message_id = 9600
            reminder.last_delivery_occurrence_utc = now
            user = await session.get(User, reminder.user_id)
            assert user is not None
            await session.commit()

        deleted, claimed = await asyncio.gather(
            reminder_service.cancel_reminder(
                user,
                reminder_id,
                expected_message_id=9600,
            ),
            worker.claim_due_reminders(1, now_utc=now),
        )
        if deleted:
            assert claimed == []
        else:
            assert len(claimed) == 1
            saved = await _get_reminder(session_factory, reminder_id)
            assert saved.status == "processing"
            assert saved.lease_token == claimed[0].lease_token

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_duplicate_snooze_and_stale_old_message_are_noops(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
        reminder_id = await _insert_reminder(session_factory, now)
        async with session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            assert reminder is not None
            reminder.status = "sent"
            reminder.last_message_id = 9702
            reminder.last_delivery_occurrence_utc = now
            user = await session.get(User, reminder.user_id)
            assert user is not None
            await session.commit()

        results = await asyncio.gather(
            reminder_service.snooze_reminder(
                user,
                reminder_id,
                expected_message_id=9702,
            ),
            reminder_service.snooze_reminder(
                user,
                reminder_id,
                expected_message_id=9702,
            ),
        )
        assert sorted(result is not None for result in results) == [False, True]
        saved = await _get_reminder(session_factory, reminder_id)
        assert saved.status == "pending"
        assert saved.last_message_id is None
        assert saved.last_delivery_occurrence_utc is None

        async with session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            assert reminder is not None
            reminder.status = "sent"
            reminder.last_message_id = 9704
            reminder.last_delivery_occurrence_utc = reminder.remind_at_utc
            await session.commit()

        assert (
            await reminder_service.snooze_reminder(
                user,
                reminder_id,
                expected_message_id=9702,
            )
            is None
        )
        saved = await _get_reminder(session_factory, reminder_id)
        assert saved.last_message_id == 9704

        recurring_id = await _insert_reminder(
            session_factory,
            now + timedelta(hours=1),
            recurrence_type=RecurrenceType.HOURLY.value,
            telegram_user_id=5002,
        )
        async with session_factory() as session:
            recurring = await session.get(Reminder, recurring_id)
            assert recurring is not None
            recurring.last_message_id = 9703
            recurring.last_delivery_occurrence_utc = now
            user = await session.get(User, recurring.user_id)
            assert user is not None
            await session.commit()

        assert await reminder_service.cancel_reminder(user, recurring_id, expected_message_id=9703)
        assert (
            await reminder_service.cancel_reminder(user, recurring_id, expected_message_id=9703)
        ) is False

    asyncio.run(_with_postgres(monkeypatch, scenario))


def test_postgres_concurrent_action_draft_starts_keep_one_active_flow(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        async with session_factory() as session:
            user = User(
                telegram_user_id=5901,
                chat_id=6902,
                timezone="Europe/Moscow",
            )
            session.add(user)
            await session.flush()
            first = Reminder(
                user_id=user.id,
                chat_id=user.chat_id,
                text="first concurrent draft reminder",
                remind_at_utc=now + timedelta(hours=1),
                delivery_at_utc=now + timedelta(hours=1),
                schedule_timezone=user.timezone,
                status="pending",
                state=ReminderState.SCHEDULED.value,
                action_revision=0,
                recurrence_type=RecurrenceType.NONE.value,
                recurrence_interval=1,
            )
            second = Reminder(
                user_id=user.id,
                chat_id=user.chat_id,
                text="second concurrent draft reminder",
                remind_at_utc=now + timedelta(hours=2),
                delivery_at_utc=now + timedelta(hours=2),
                schedule_timezone=user.timezone,
                status="pending",
                state=ReminderState.SCHEDULED.value,
                action_revision=0,
                recurrence_type=RecurrenceType.NONE.value,
                recurrence_interval=1,
            )
            session.add_all([first, second])
            await session.commit()
            first_id = first.id
            second_id = second.id

        started = await asyncio.gather(
            reminder_service.create_action_draft(
                user,
                first_id,
                action_type="snooze",
                expected_action_revision=0,
                current_step="time",
            ),
            reminder_service.create_action_draft(
                user,
                second_id,
                action_type="edit",
                expected_action_revision=0,
                current_step="text",
            ),
        )
        assert all(draft is not None for draft in started)

        async with session_factory() as session:
            drafts = list(
                (
                    await session.scalars(
                        select(ActionDraft).where(
                            ActionDraft.user_id == user.id,
                            ActionDraft.chat_id == user.chat_id,
                        )
                    )
                ).all()
            )
        assert len(drafts) == 1
        assert drafts[0].action_type in {"snooze", "edit"}

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


def test_postgres_concurrent_voice_confirmation_creates_one_reminder(monkeypatch) -> None:
    async def scenario(session_factory) -> None:
        now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
        user = await _insert_user(session_factory, telegram_user_id=5902, chat_id=6903)
        parsed = ParsedReminder(
            local_dt=datetime(2026, 9, 8, 15, 0),
            text="голосовой тест",
        )
        draft = await voice_service.create_voice_draft(
            user,
            "напомни сегодня в 15 голосовой тест",
            parsed,
            source_message_id=8801,
            now_utc=now,
        )
        assert await voice_service.bind_voice_preview_message(
            user,
            draft.id,
            revision=draft.action_revision,
            message_id=9901,
            now_utc=now,
        )

        results = await asyncio.gather(
            voice_service.confirm_voice_draft(
                user,
                draft.id,
                expected_revision=draft.action_revision,
                expected_message_id=9901,
                now_utc=now,
            ),
            voice_service.confirm_voice_draft(
                user,
                draft.id,
                expected_revision=draft.action_revision,
                expected_message_id=9901,
                now_utc=now,
            ),
        )

        assert sorted(result is not None for result in results) == [False, True]
        async with session_factory() as session:
            reminders = list((await session.scalars(select(Reminder))).all())
            drafts = list((await session.scalars(select(VoiceReminderDraft))).all())
        assert len(reminders) == 1
        assert reminders[0].text == "голосовой тест"
        assert drafts == []

    asyncio.run(_with_postgres(monkeypatch, scenario))
