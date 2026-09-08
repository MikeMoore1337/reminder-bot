import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import (
    DeadlinePlanState,
    DeadlineStepState,
    Reminder,
    ReminderDeadlinePlan,
    ReminderDeadlineStep,
    ReminderKind,
    ReminderOccurrence,
    ReminderState,
    User,
)
from app.services import deadline_service, reminder_service
from app.services.deadline_service import (
    build_deadline_plan,
    confirm_deadline_draft,
    create_deadline_draft,
    disable_deadline_plan,
    enable_deadline_plan,
)
from app.services.reminder_parser import DeadlineRequest, parse_deadline_input, parse_reminder_input
from app.utils.datetime_utils import from_utc_to_user, to_utc
from app.workers import reminder_worker as worker


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


async def _open_sqlite(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    connection = await engine.connect()
    await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(deadline_service, "SessionLocal", session_factory)
    monkeypatch.setattr(deadline_service, "deadline_metrics", deadline_service.DeadlineMetrics())
    monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "worker_metrics", worker.WorkerMetrics())
    monkeypatch.setattr(worker, "settings", _worker_settings())
    return engine, connection, session_factory


async def _add_user(session_factory, *, timezone: str = "Europe/Moscow") -> User:
    async with session_factory() as session:
        user = User(telegram_user_id=1001, chat_id=2002, timezone=timezone)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def test_deadline_parser_requires_confirmation_and_accepts_bounded_policy() -> None:
    now_local = datetime(2026, 9, 8, 10, 0)
    parsed = parse_reminder_input(
        "/deadline 2026-09-10 18:00 оплатить счёт | за день, за час, просрочено через час",
        now_local,
    )

    assert isinstance(parsed, DeadlineRequest)
    assert parsed.point_codes == ("day_before", "before_deadline", "overdue", "at_deadline")
    assert parsed.overdue_after_minutes == 60

    natural = parse_deadline_input(
        "напомни до завтра в 18:00 отправить отчёт",
        now_local,
    )
    assert isinstance(natural, DeadlineRequest)
    assert natural.local_dt == datetime(2026, 9, 9, 18, 0)

    command_without_time = parse_deadline_input(
        "/deadline 2026-09-10 оплатить счёт",
        now_local,
    )
    assert isinstance(command_without_time, DeadlineRequest)
    assert command_without_time.local_dt == datetime(2026, 9, 10, 23, 59)

    prose = parse_reminder_input("Оплатить VPS до 10 сентября", now_local)
    assert isinstance(prose, DeadlineRequest)
    assert prose.local_dt == datetime(2026, 9, 10, 23, 59)
    assert prose.text == "Оплатить VPS"

    unsupported = parse_deadline_input(
        "/deadline 2026-09-10 18:00 задача | когда-нибудь",
        now_local,
    )
    assert unsupported is not None
    assert not isinstance(unsupported, DeadlineRequest)


def test_deadline_plan_is_timezone_aware_and_marks_elapsed_steps() -> None:
    deadline = to_utc(datetime(2026, 10, 25, 15, 0), "Europe/Helsinki")
    now = to_utc(datetime(2026, 10, 25, 10, 30), "Europe/Helsinki")
    plan = build_deadline_plan(
        deadline,
        "Europe/Helsinki",
        now_utc=now,
        point_codes=("day_before", "before_deadline", "at_deadline"),
    )

    assert [step.code for step in plan.steps] == ["day_before", "before_deadline", "at_deadline"]
    assert (
        from_utc_to_user(plan.steps[0].scheduled_at_utc, "Europe/Helsinki").date()
        == datetime(2026, 10, 24).date()
    )
    assert plan.steps[0].state == DeadlineStepState.SKIPPED.value
    assert plan.steps[0].skip_reason == "already_elapsed"
    assert all(
        left.scheduled_at_utc < right.scheduled_at_utc
        for left, right in zip(plan.steps, plan.steps[1:], strict=False)
    )

    with pytest.raises(ValueError, match="один раз"):
        build_deadline_plan(
            deadline,
            "Europe/Helsinki",
            now_utc=now,
            point_codes=(
                "week_before",
                "day_before",
                "deadline_morning",
                "before_deadline",
                "at_deadline",
                "overdue",
                "week",
            ),
        )


def test_deadline_draft_confirm_persists_plan_and_is_restart_safe(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
        try:
            user = await _add_user(session_factory)
            request = DeadlineRequest(
                local_dt=datetime(2026, 9, 10, 18, 0),
                text="оплатить счёт",
                point_codes=("day_before", "before_deadline", "at_deadline"),
            )
            draft = await create_deadline_draft(
                user,
                request,
                raw_text="/deadline ...",
                source_message_id=41,
                now_utc=now,
            )
            reminder = await confirm_deadline_draft(
                user,
                draft.id,
                expected_revision=draft.action_revision,
                expected_message_id=900,
                now_utc=now,
            )
            assert reminder is not None
            assert deadline_service.get_deadline_metrics()["planned"] == 3
            assert reminder.kind == ReminderKind.DEADLINE.value

            async with session_factory() as session:
                persisted = await session.get(Reminder, reminder.id)
                assert persisted is not None
                assert persisted.deadline_plan_state == DeadlinePlanState.ACTIVE.value
                plan = await session.scalar(
                    select(ReminderDeadlinePlan).where(
                        ReminderDeadlinePlan.reminder_id == reminder.id
                    )
                )
                assert plan is not None
                steps = list(
                    (
                        await session.scalars(
                            select(ReminderDeadlineStep)
                            .where(ReminderDeadlineStep.plan_id == plan.id)
                            .order_by(ReminderDeadlineStep.sequence)
                        )
                    ).all()
                )
                assert len(steps) == 3
                assert steps[0].state == DeadlineStepState.PENDING.value

            assert (
                await confirm_deadline_draft(
                    user,
                    draft.id,
                    expected_revision=draft.action_revision,
                    expected_message_id=900,
                    now_utc=now,
                )
                is None
            )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_deadline_worker_advances_one_step_and_done_stops_remaining_plan(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        start = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
        try:
            user = await _add_user(session_factory)
            request = DeadlineRequest(
                local_dt=datetime(2026, 9, 10, 18, 0),
                text="оплатить счёт",
            )
            draft = await create_deadline_draft(
                user,
                request,
                raw_text="/deadline ...",
                now_utc=start,
            )
            reminder = await confirm_deadline_draft(
                user,
                draft.id,
                expected_revision=draft.action_revision,
                expected_message_id=901,
                now_utc=start,
            )
            assert reminder is not None

            first_at = reminder.remind_at_utc
            claimed = await worker.claim_due_reminders(1, now_utc=first_at)
            assert len(claimed) == 1
            claimed_reminder = claimed[0]
            assert claimed_reminder.lease_token is not None
            occurrence_id = await reminder_service.prepare_delivery_occurrence(
                claimed_reminder.id,
                claimed_reminder.lease_token,
                now_utc=first_at,
            )
            assert occurrence_id is not None
            assert await reminder_service.set_last_message_id(
                claimed_reminder.id,
                1001,
                lease_token=claimed_reminder.lease_token,
                occurrence_at_utc=first_at,
                now_utc=first_at,
            )
            assert await worker.finalize_delivery_success(
                claimed_reminder.id,
                claimed_reminder.lease_token,
                now_utc=first_at,
            )
            assert deadline_service.get_deadline_metrics()["delivered"] == 1

            async with session_factory() as session:
                delivered = await session.get(ReminderOccurrence, occurrence_id)
                current = await session.get(Reminder, claimed_reminder.id)
                assert delivered is not None
                assert current is not None
                assert delivered.status == "delivered"
                assert current.state == ReminderState.SCHEDULED.value
                assert current.last_message_id == 1001
                expected_revision = delivered.action_revision
                current_message = current.last_message_id

            assert await reminder_service.complete_reminder(
                user,
                claimed_reminder.id,
                expected_revision=expected_revision,
                expected_occurrence_id=occurrence_id,
                expected_message_id=current_message,
            )
            assert not await reminder_service.complete_reminder(
                user,
                claimed_reminder.id,
                expected_revision=expected_revision,
                expected_occurrence_id=occurrence_id,
                expected_message_id=current_message,
            )
            metrics = deadline_service.get_deadline_metrics()
            assert metrics["skipped"] == 2
            assert metrics["completed"] == 1

            async with session_factory() as session:
                current = await session.get(Reminder, claimed_reminder.id)
                assert current is not None
                assert current.state == ReminderState.COMPLETED.value
                plan = await session.scalar(
                    select(ReminderDeadlinePlan).where(
                        ReminderDeadlinePlan.reminder_id == claimed_reminder.id
                    )
                )
                assert plan is not None
                assert plan.state == DeadlinePlanState.COMPLETED.value
                pending = list(
                    (
                        await session.scalars(
                            select(ReminderDeadlineStep).where(
                                ReminderDeadlineStep.plan_id == plan.id,
                                ReminderDeadlineStep.state == DeadlineStepState.PENDING.value,
                            )
                        )
                    ).all()
                )
                assert pending == []
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_deadline_disable_enable_are_revision_guarded(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
        try:
            user = await _add_user(session_factory)
            request = DeadlineRequest(local_dt=datetime(2026, 9, 10, 18), text="задача")
            draft = await create_deadline_draft(
                user,
                request,
                raw_text="/deadline ...",
                now_utc=now,
            )
            reminder = await confirm_deadline_draft(
                user,
                draft.id,
                expected_revision=draft.action_revision,
                expected_message_id=902,
                now_utc=now,
            )
            assert reminder is not None
            initial_revision = reminder.action_revision

            assert await disable_deadline_plan(
                user,
                reminder.id,
                expected_revision=initial_revision,
                now_utc=now,
            )
            async with session_factory() as session:
                paused = await session.get(Reminder, reminder.id)
                assert paused is not None
                assert paused.state == ReminderState.PAUSED.value
                disabled_revision = paused.action_revision

            assert not await disable_deadline_plan(
                user,
                reminder.id,
                expected_revision=initial_revision,
                now_utc=now,
            )
            assert await enable_deadline_plan(
                user,
                reminder.id,
                expected_revision=disabled_revision,
                now_utc=now,
            )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())
