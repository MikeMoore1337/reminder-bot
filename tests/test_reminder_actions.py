import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.callbacks import CallbackAction, CallbackTarget, encode_callback, parse_callback
from app.db.base import Base
from app.db.models import (
    ActionDraft,
    OccurrenceState,
    RecurrenceType,
    Reminder,
    ReminderOccurrence,
    ReminderState,
    User,
)
from app.services import reminder_service
from app.services.reminder_service import (
    calculate_snooze_target,
    cancel_active_action_drafts,
    cancel_reminder,
    complete_reminder,
    create_action_draft,
    edit_reminder,
    get_active_action_draft,
    pause_reminder,
    resume_reminder,
    snooze_reminder,
    update_action_draft,
)
from app.workers import reminder_worker as worker


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _open_sqlite(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    connection = await engine.connect()
    await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    return engine, connection, session_factory


async def _add_user(
    session_factory,
    *,
    telegram_user_id: int = 1001,
    chat_id: int = 2002,
    timezone: str = "Europe/Moscow",
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


async def _add_reminder(
    session_factory,
    user: User,
    *,
    now: datetime,
    state: str = ReminderState.SCHEDULED.value,
    recurrence_type: str = RecurrenceType.NONE.value,
    canonical_at: datetime | None = None,
    action_revision: int = 0,
    message_id: int | None = None,
    occurrence_at: datetime | None = None,
) -> tuple[Reminder, ReminderOccurrence | None]:
    canonical = canonical_at or now
    delivered = state == ReminderState.DELIVERED.value or occurrence_at is not None
    async with session_factory() as session:
        reminder = Reminder(
            user_id=user.id,
            chat_id=user.chat_id,
            text="test action reminder",
            remind_at_utc=canonical,
            delivery_at_utc=None if delivered else canonical,
            schedule_timezone=user.timezone,
            status="sent" if delivered else "pending",
            state=state,
            action_revision=action_revision,
            recurrence_type=recurrence_type,
            recurrence_interval=1,
            last_message_id=message_id if delivered else None,
            last_delivery_occurrence_utc=occurrence_at if delivered else None,
            sent_at=now if delivered else None,
        )
        session.add(reminder)
        await session.flush()
        occurrence = None
        if occurrence_at is not None:
            occurrence = ReminderOccurrence(
                reminder_id=reminder.id,
                occurrence_at_utc=occurrence_at,
                delivery_at_utc=occurrence_at,
                status=OccurrenceState.DELIVERED.value,
                action_revision=action_revision,
                message_id=message_id,
                delivered_at=now,
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


def test_callback_protocol_is_compact_and_fail_closed() -> None:
    payload = encode_callback(CallbackAction.DONE, CallbackTarget.OCCURRENCE, 42, 7)
    parsed = parse_callback(payload)

    assert payload == "r1:done:o:42:7"
    assert parsed is not None
    assert parsed.action == CallbackAction.DONE
    assert parsed.target == CallbackTarget.OCCURRENCE
    assert parsed.target_id == 42
    assert parsed.revision == 7
    assert len(payload.encode()) <= 64

    invalid_payloads = (
        "reminder:snooze:42",
        "r2:done:o:42:7",
        "r1:unknown:o:42:7",
        "r1:done:x:42:7",
        "r1:done:o:0:7",
        "r1:done:o:42:-1",
        "r1:done:o:42:7:extra",
    )
    assert all(parse_callback(value) is None for value in invalid_payloads)
    with pytest.raises(ValueError):
        encode_callback(CallbackAction.DONE, CallbackTarget.OCCURRENCE, 0, 0)
    with pytest.raises(ValueError):
        encode_callback(CallbackAction.DONE, CallbackTarget.OCCURRENCE, 1, -1)


def test_action_keyboard_exposes_only_state_valid_actions() -> None:
    delivered = worker.reminder_actions_kb(
        42,
        occurrence_id=9,
        revision=3,
        state=ReminderState.DELIVERED.value,
        recurrence_type=RecurrenceType.DAILY.value,
    )
    delivered_actions = {
        parse_callback(button.callback_data).action
        for row in delivered.inline_keyboard
        for button in row
    }
    assert delivered_actions == {
        CallbackAction.DONE,
        CallbackAction.SNOOZE,
        CallbackAction.EDIT,
        CallbackAction.PAUSE,
        CallbackAction.DELETE,
    }

    scheduled = worker.reminder_actions_kb(
        42,
        revision=4,
        state=ReminderState.SCHEDULED.value,
        recurrence_type=RecurrenceType.NONE.value,
    )
    scheduled_actions = {
        parse_callback(button.callback_data).action
        for row in scheduled.inline_keyboard
        for button in row
    }
    assert scheduled_actions == {
        CallbackAction.SNOOZE,
        CallbackAction.EDIT,
        CallbackAction.DELETE,
    }

    paused = worker.reminder_actions_kb(
        42,
        revision=5,
        state=ReminderState.PAUSED.value,
        recurrence_type=RecurrenceType.DAILY.value,
    )
    paused_actions = {
        parse_callback(button.callback_data).action
        for row in paused.inline_keyboard
        for button in row
    }
    assert paused_actions == {
        CallbackAction.RESUME,
        CallbackAction.EDIT,
        CallbackAction.DELETE,
    }


def test_snooze_presets_use_user_timezone_and_dst_policy() -> None:
    now = datetime(2026, 1, 10, 23, 0, tzinfo=UTC)

    assert calculate_snooze_target(
        "10m", now_utc=now, timezone_name="America/New_York"
    ) == datetime(2026, 1, 10, 23, 10, tzinfo=UTC)
    assert calculate_snooze_target("1h", now_utc=now, timezone_name="America/New_York") == datetime(
        2026, 1, 11, 0, 0, tzinfo=UTC
    )
    assert calculate_snooze_target(
        "evening", now_utc=now, timezone_name="America/New_York"
    ) == datetime(2026, 1, 11, 1, 0, tzinfo=UTC)
    assert calculate_snooze_target(
        "tomorrow", now_utc=now, timezone_name="America/New_York"
    ) == datetime(2026, 1, 11, 14, 0, tzinfo=UTC)

    spring_now = datetime(2026, 3, 8, 4, 30, tzinfo=UTC)
    fall_now = datetime(2026, 11, 1, 4, 30, tzinfo=UTC)
    assert calculate_snooze_target(
        "tomorrow", now_utc=spring_now, timezone_name="America/New_York"
    ) == datetime(2026, 3, 8, 13, 0, tzinfo=UTC)
    assert calculate_snooze_target(
        "tomorrow", now_utc=fall_now, timezone_name="America/New_York"
    ) == datetime(2026, 11, 2, 14, 0, tzinfo=UTC)


def test_one_off_done_is_persisted_and_terminal(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            user = await _add_user(session_factory)
            reminder, occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.DELIVERED.value,
                action_revision=3,
                message_id=9001,
                occurrence_at=now,
            )
            assert occurrence is not None

            assert await complete_reminder(
                user,
                reminder.id,
                expected_revision=3,
                expected_occurrence_id=occurrence.id,
                expected_message_id=9001,
            )
            assert not await complete_reminder(
                user,
                reminder.id,
                expected_revision=3,
                expected_occurrence_id=occurrence.id,
                expected_message_id=9001,
            )
            assert (
                await snooze_reminder(
                    user,
                    reminder.id,
                    expected_revision=3,
                    expected_occurrence_id=occurrence.id,
                    expected_message_id=9001,
                    target_at_utc=now + timedelta(minutes=10),
                )
                is None
            )
            assert not await edit_reminder(
                user,
                reminder.id,
                expected_revision=3,
                text="should stay unchanged",
                expected_occurrence_id=occurrence.id,
                expected_message_id=9001,
            )

            saved = await _get_reminder(session_factory, reminder.id)
            assert saved.state == ReminderState.COMPLETED.value
            assert saved.status == "sent"
            assert saved.completed_at is not None
            async with session_factory() as session:
                saved_occurrence = await session.get(ReminderOccurrence, occurrence.id)
                assert saved_occurrence is not None
                assert saved_occurrence.status == OccurrenceState.COMPLETED.value
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_delete_is_soft_and_cancelled_rows_are_not_claimed(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            monkeypatch.setattr(worker, "settings", SimpleNamespace(worker_max_attempts=3))
            user = await _add_user(session_factory)
            reminder, _ = await _add_reminder(session_factory, user, now=now)

            assert await cancel_reminder(user, reminder.id, expected_revision=0)
            assert not await cancel_reminder(user, reminder.id, expected_revision=0)
            saved = await _get_reminder(session_factory, reminder.id)
            assert saved.state == ReminderState.CANCELLED.value
            assert saved.cancelled_at is not None
            assert saved.status == "sent"
            assert await worker.claim_due_reminders(1, now_utc=now) == []

            failed, _ = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.FAILED.value,
                canonical_at=now - timedelta(minutes=1),
            )
            async with session_factory() as session:
                failed_row = await session.get(Reminder, failed.id)
                assert failed_row is not None
                failed_row.status = "failed"
                await session.commit()
            assert (
                await snooze_reminder(
                    user,
                    failed.id,
                    target_at_utc=now + timedelta(minutes=10),
                )
                is None
            )
            assert await worker.claim_due_reminders(1, now_utc=now) == []
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_recurring_snooze_creates_one_child_without_mutating_series(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        source_at = now - timedelta(hours=1)
        next_canonical = now + timedelta(hours=1)
        child_delivery = now + timedelta(hours=2)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            monkeypatch.setattr(worker, "utc_now", lambda: child_delivery)
            monkeypatch.setattr(
                worker,
                "settings",
                SimpleNamespace(worker_lease_duration_seconds=60, worker_max_attempts=3),
            )
            user = await _add_user(session_factory)
            parent, occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.SCHEDULED.value,
                recurrence_type=RecurrenceType.HOURLY.value,
                canonical_at=next_canonical,
                action_revision=4,
                message_id=9100,
                occurrence_at=source_at,
            )
            assert occurrence is not None

            child = await snooze_reminder(
                user,
                parent.id,
                expected_revision=4,
                expected_occurrence_id=occurrence.id,
                expected_message_id=9100,
                target_at_utc=child_delivery,
            )
            assert child is not None
            assert child.parent_reminder_id == parent.id
            assert _utc(child.remind_at_utc) == child_delivery

            saved_parent = await _get_reminder(session_factory, parent.id)
            assert _utc(saved_parent.remind_at_utc) == next_canonical
            assert saved_parent.recurrence_type == RecurrenceType.HOURLY.value
            assert saved_parent.state == ReminderState.SCHEDULED.value

            async with session_factory() as session:
                source = await session.get(ReminderOccurrence, occurrence.id)
                assert source is not None
                assert source.status == OccurrenceState.SNOOZED.value
                children = list(
                    (
                        await session.scalars(
                            select(Reminder).where(Reminder.parent_reminder_id == parent.id)
                        )
                    ).all()
                )
                assert len(children) == 1

            assert (
                await snooze_reminder(
                    user,
                    parent.id,
                    expected_revision=4,
                    expected_occurrence_id=occurrence.id,
                    expected_message_id=9100,
                    target_at_utc=child_delivery + timedelta(minutes=10),
                )
                is None
            )

            claimed = await worker.claim_due_reminders(1, now_utc=child_delivery)
            assert len(claimed) == 1
            assert claimed[0].parent_reminder_id == parent.id
            assert claimed[0].id == child.id
            token = claimed[0].lease_token
            assert token
            assert await reminder_service.set_last_message_id(
                child.id,
                9200,
                lease_token=token,
                occurrence_at_utc=child_delivery,
                now_utc=child_delivery,
            )
            assert await worker.finalize_delivery_success(child.id, token, now_utc=child_delivery)
            saved_child = await _get_reminder(session_factory, child.id)
            assert saved_child.state == ReminderState.DELIVERED.value
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_recurring_pause_resume_skips_missed_burst(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        old_canonical = now - timedelta(days=3)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            monkeypatch.setattr(worker, "utc_now", lambda: now)
            monkeypatch.setattr(
                worker,
                "settings",
                SimpleNamespace(worker_lease_duration_seconds=60, worker_max_attempts=3),
            )
            user = await _add_user(session_factory)
            reminder, _ = await _add_reminder(
                session_factory,
                user,
                now=now,
                recurrence_type=RecurrenceType.DAILY.value,
                canonical_at=old_canonical,
            )

            assert await pause_reminder(user, reminder.id, expected_revision=0)
            paused = await _get_reminder(session_factory, reminder.id)
            assert paused.state == ReminderState.PAUSED.value
            assert _utc(paused.remind_at_utc) == old_canonical
            assert await worker.claim_due_reminders(1, now_utc=now) == []
            assert not await pause_reminder(user, reminder.id, expected_revision=0)

            assert await resume_reminder(user, reminder.id, expected_revision=1)
            resumed = await _get_reminder(session_factory, reminder.id)
            assert resumed.state == ReminderState.SCHEDULED.value
            assert _utc(resumed.remind_at_utc) == datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            assert resumed.action_revision == 2
            assert await worker.claim_due_reminders(1, now_utc=now) == []
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_edit_draft_survives_session_boundary_and_stale_revision_is_rejected(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            user = await _add_user(session_factory)
            reminder, occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.DELIVERED.value,
                action_revision=2,
                message_id=9300,
                occurrence_at=now,
            )
            assert occurrence is not None

            draft = await create_action_draft(
                user,
                reminder.id,
                action_type="edit",
                expected_action_revision=2,
                expected_occurrence_at_utc=now,
                expected_message_id=9300,
                expected_occurrence_id=occurrence.id,
                current_step="text",
            )
            assert draft is not None
            restarted_draft = await get_active_action_draft(user, action_type="edit")
            assert restarted_draft is not None
            updated = await update_action_draft(
                user,
                restarted_draft.id,
                current_step="schedule",
                payload={"text": "updated text", "occurrence_id": occurrence.id},
            )
            assert updated is not None
            edited = await reminder_service.apply_edit_draft(user, updated)
            assert edited is not None
            assert edited.text == "updated text"
            assert edited.state == ReminderState.DELIVERED.value
            assert edited.action_revision == 3

            stale_draft = await create_action_draft(
                user,
                reminder.id,
                action_type="edit",
                expected_action_revision=3,
                expected_occurrence_at_utc=now,
                expected_message_id=9300,
                expected_occurrence_id=occurrence.id,
                current_step="text",
                payload={"text": "stale text", "occurrence_id": occurrence.id},
            )
            assert stale_draft is not None
            assert await edit_reminder(
                user,
                reminder.id,
                expected_revision=3,
                text="newer text",
                expected_occurrence_id=occurrence.id,
                expected_message_id=9300,
            )
            assert await reminder_service.apply_edit_draft(user, stale_draft) is None
            active_draft = await create_action_draft(
                user,
                reminder.id,
                action_type="snooze",
                expected_action_revision=4,
                expected_occurrence_at_utc=now,
                expected_message_id=9300,
                expected_occurrence_id=occurrence.id,
                current_step="time",
            )
            assert active_draft is not None
            assert await cancel_active_action_drafts(user) == 1
            assert await get_active_action_draft(user) is None

            other_user = await _add_user(
                session_factory,
                telegram_user_id=1002,
                chat_id=2003,
            )
            assert await get_active_action_draft(other_user) is None
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_scheduled_text_only_edit_keeps_schedule_and_persists(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            user = await _add_user(session_factory)
            reminder, _ = await _add_reminder(
                session_factory,
                user,
                now=now,
                canonical_at=now + timedelta(hours=2),
            )
            draft = await create_action_draft(
                user,
                reminder.id,
                action_type="edit",
                expected_action_revision=0,
                current_step="schedule",
                payload={"text": "updated scheduled text"},
            )
            assert draft is not None

            edited = await reminder_service.apply_edit_draft(user, draft)

            assert edited is not None
            assert edited.text == "updated scheduled text"
            assert edited.state == ReminderState.SCHEDULED.value
            assert edited.status == "pending"
            assert _utc(edited.remind_at_utc) == now + timedelta(hours=2)
            assert edited.action_revision == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_schedule_edit_supports_existing_recurrence_grammar(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            user = await _add_user(session_factory)
            reminder, _ = await _add_reminder(
                session_factory,
                user,
                now=now,
                canonical_at=now + timedelta(hours=1),
            )
            parsed = reminder_service.parse_edit_schedule(
                "каждый день в 9", now_local=datetime(2026, 9, 7, 10, 0)
            )
            assert parsed is not None
            edited = await edit_reminder(
                user,
                reminder.id,
                expected_revision=0,
                text="daily text",
                local_dt=parsed.local_dt,
                recurrence_type=parsed.recurrence_type,
                recurrence_interval=parsed.recurrence_interval,
                datetime_semantics=parsed.datetime_semantics,
            )
            assert edited is not None
            assert edited.recurrence_type == RecurrenceType.DAILY.value
            assert edited.text == "daily text"
            assert edited.state == ReminderState.SCHEDULED.value
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_action_draft_expiry_is_persisted_and_observable(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            user = await _add_user(session_factory)
            reminder, _ = await _add_reminder(session_factory, user, now=now)
            expired = await create_action_draft(
                user,
                reminder.id,
                action_type="snooze",
                expected_action_revision=0,
                current_step="time",
                now_utc=now,
                expires_at=now - timedelta(seconds=1),
            )
            assert expired is not None
            assert await get_active_action_draft(user, now_utc=now) is None
            async with session_factory() as session:
                assert (
                    await session.scalar(select(ActionDraft).where(ActionDraft.id == expired.id))
                    is None
                )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())
