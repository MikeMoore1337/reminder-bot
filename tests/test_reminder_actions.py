import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.callbacks import (
    CallbackAction,
    CallbackOrigin,
    CallbackTarget,
    encode_callback,
    parse_callback,
)
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
from app.handlers import reminders as reminders_handler
from app.handlers.reminders import _expected_delivery_message_id
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

    assert payload == "r1:done:o:42:7:d"
    assert parsed is not None
    assert parsed.action == CallbackAction.DONE
    assert parsed.target == CallbackTarget.OCCURRENCE
    assert parsed.target_id == 42
    assert parsed.revision == 7
    assert parsed.origin == CallbackOrigin.DELIVERY
    assert parsed.membership_id is None
    assert parsed.membership_revision is None
    assert len(payload.encode()) <= 64

    shared_payload = encode_callback(
        CallbackAction.DONE,
        CallbackTarget.OCCURRENCE,
        42,
        7,
        origin=CallbackOrigin.SHARED,
        membership_id=123,
        membership_revision=4,
    )
    parsed_shared = parse_callback(shared_payload)
    assert shared_payload == "r1:done:o:42:7:h:123.4"
    assert parsed_shared is not None
    assert parsed_shared.origin == CallbackOrigin.SHARED
    assert parsed_shared.membership_id == 123
    assert parsed_shared.membership_revision == 4
    assert len(shared_payload.encode()) <= 64

    list_payload = encode_callback(
        CallbackAction.DONE,
        CallbackTarget.OCCURRENCE,
        42,
        7,
        origin=CallbackOrigin.LIST,
    )
    parsed_list = parse_callback(list_payload)
    assert parsed_list is not None
    assert parsed_list.origin == CallbackOrigin.LIST
    parsed_legacy = parse_callback("r1:done:o:42:7")
    assert parsed_legacy is not None
    assert parsed_legacy.origin == CallbackOrigin.DELIVERY

    invalid_payloads = (
        "reminder:snooze:42",
        "r2:done:o:42:7",
        "r1:unknown:o:42:7",
        "r1:done:x:42:7",
        "r1:done:o:0:7",
        "r1:done:o:42:-1",
        "r1:done:o:42:7:extra",
        "r1:done:o:42:7:x",
        "r1:done:o:42:7:h:1",
        "r1:done:o:42:7:h:1.0",
        "r1:done:o:42:7:h:0.1",
        "r1:done:o:42:7:h:1.1.extra",
    )
    assert all(parse_callback(value) is None for value in invalid_payloads)
    with pytest.raises(ValueError):
        encode_callback(CallbackAction.DONE, CallbackTarget.OCCURRENCE, 0, 0)
    with pytest.raises(ValueError):
        encode_callback(CallbackAction.DONE, CallbackTarget.OCCURRENCE, 1, -1)
    with pytest.raises(ValueError):
        encode_callback(
            CallbackAction.DONE,
            CallbackTarget.OCCURRENCE,
            1,
            0,
            membership_id=1,
        )
    with pytest.raises(ValueError):
        encode_callback(
            CallbackAction.DONE,
            CallbackTarget.OCCURRENCE,
            1,
            0,
            membership_revision=1,
        )


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

    list_card = worker.reminder_actions_kb(
        42,
        occurrence_id=9,
        revision=3,
        state=ReminderState.DELIVERED.value,
        recurrence_type=RecurrenceType.DAILY.value,
        origin=CallbackOrigin.LIST,
    )
    list_origins = {
        parse_callback(button.callback_data).origin
        for row in list_card.inline_keyboard
        for button in row
    }
    assert list_origins == {CallbackOrigin.LIST}
    list_snooze_origins = {
        parse_callback(button.callback_data).origin
        for row in worker.snooze_presets_kb(
            42,
            occurrence_id=9,
            revision=3,
            origin=CallbackOrigin.LIST,
        ).inline_keyboard
        for button in row
    }
    assert list_snooze_origins == {CallbackOrigin.LIST}

    shared_card = worker.reminder_actions_kb(
        42,
        occurrence_id=9,
        revision=3,
        state=ReminderState.DELIVERED.value,
        origin=CallbackOrigin.SHARED,
        shared_participant=True,
        membership_id=12,
        membership_revision=2,
    )
    shared_callbacks = [
        parse_callback(button.callback_data)
        for row in shared_card.inline_keyboard
        for button in row
    ]
    assert all(parsed is not None for parsed in shared_callbacks)
    assert {(parsed.membership_id, parsed.membership_revision) for parsed in shared_callbacks} == {
        (12, 2)
    }


def test_list_occurrence_callbacks_do_not_bind_to_new_control_message() -> None:
    delivery_callback = parse_callback("r1:done:o:9:3:d")
    list_callback = parse_callback("r1:done:o:9:3:l")
    assert delivery_callback is not None
    assert list_callback is not None

    assert _expected_delivery_message_id(delivery_callback, 9, 7001) == 7001
    assert _expected_delivery_message_id(list_callback, 9, 8001) is None
    assert _expected_delivery_message_id(list_callback, None, 8001) is None


def test_list_done_callback_uses_occurrence_identity_without_card_message_id(monkeypatch) -> None:
    async def scenario() -> None:
        user = SimpleNamespace(id=1, chat_id=2)
        callback_message = Message.model_construct(
            message_id=8001,
            chat=SimpleNamespace(id=user.chat_id),
        )
        captured: dict[str, object] = {}

        async def fake_get_or_create_user(**kwargs):
            assert kwargs == {"telegram_user_id": 1001, "chat_id": user.chat_id}
            return user

        async def fake_resolve_callback_target(parsed, resolved_user):
            assert resolved_user is user
            assert parsed.origin == CallbackOrigin.LIST
            return 42, 9, datetime(2026, 9, 7, 10, 0, tzinfo=UTC), 7001

        async def fake_complete_reminder(*args, **kwargs):
            captured["args"] = args
            captured.update(kwargs)
            return False

        async def fake_answer(*args, **kwargs):
            captured["answer"] = (args, kwargs)

        callback = SimpleNamespace(
            data="r1:done:o:9:3:l",
            message=callback_message,
            from_user=SimpleNamespace(id=1001),
            answer=fake_answer,
        )
        monkeypatch.setattr(reminders_handler, "get_or_create_user", fake_get_or_create_user)
        monkeypatch.setattr(
            reminders_handler,
            "_resolve_callback_target",
            fake_resolve_callback_target,
        )
        monkeypatch.setattr(reminders_handler, "complete_reminder", fake_complete_reminder)

        await reminders_handler.reminder_callback(callback)

        assert captured["expected_message_id"] is None
        assert captured["expected_occurrence_id"] == 9
        assert captured["expected_revision"] == 3

    asyncio.run(scenario())


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


def test_custom_snooze_preserves_instant_and_wall_clock_semantics(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 11, 1, 4, 30, tzinfo=UTC)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            fallback_user = await _add_user(
                session_factory,
                telegram_user_id=1101,
                chat_id=2102,
                timezone="America/New_York",
            )
            fallback_reminder, _ = await _add_reminder(
                session_factory,
                fallback_user,
                now=now,
                canonical_at=now + timedelta(hours=1),
            )
            fallback_draft = await create_action_draft(
                fallback_user,
                fallback_reminder.id,
                action_type="snooze",
                expected_action_revision=0,
                current_step="time",
            )
            assert fallback_draft is not None
            fallback_local_now = reminder_service.from_utc_to_user(now, fallback_user.timezone)
            parsed_relative = reminder_service.parse_custom_datetime(
                "через 2 часа", now_local=fallback_local_now
            )
            assert parsed_relative is not None
            assert parsed_relative.datetime_semantics == "instant"
            fallback_result = await reminder_service.apply_custom_snooze_draft(
                fallback_user,
                fallback_draft,
                "через 2 часа",
            )
            assert fallback_result is not None
            assert _utc(fallback_result.delivery_at_utc) == now + timedelta(hours=2)

            spring_now = datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
            spring_local_now = reminder_service.from_utc_to_user(spring_now, fallback_user.timezone)
            parsed_spring = reminder_service.parse_custom_datetime(
                "через 2 часа", now_local=spring_local_now
            )
            assert parsed_spring is not None
            assert parsed_spring.datetime_semantics == "instant"
            assert reminder_service.resolve_schedule_datetime(
                parsed_spring.local_dt,
                fallback_user.timezone,
                semantics=parsed_spring.datetime_semantics,
            ) == spring_now + timedelta(hours=2)

            absolute = reminder_service.parse_custom_datetime(
                "2026-11-01 01:30", now_local=fallback_local_now
            )
            assert absolute is not None
            assert absolute.datetime_semantics == "wall_clock"
            assert reminder_service.resolve_schedule_datetime(
                absolute.local_dt,
                fallback_user.timezone,
                semantics=absolute.datetime_semantics,
            ) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)

            ordinary_user = await _add_user(
                session_factory,
                telegram_user_id=1102,
                chat_id=2103,
                timezone="Europe/Helsinki",
            )
            ordinary_reminder, _ = await _add_reminder(
                session_factory,
                ordinary_user,
                now=now,
                canonical_at=now + timedelta(hours=1),
            )
            ordinary_draft = await create_action_draft(
                ordinary_user,
                ordinary_reminder.id,
                action_type="snooze",
                expected_action_revision=0,
                current_step="time",
            )
            assert ordinary_draft is not None
            ordinary_result = await reminder_service.apply_custom_snooze_draft(
                ordinary_user,
                ordinary_draft,
                "через 2 часа",
            )
            assert ordinary_result is not None
            assert _utc(ordinary_result.delivery_at_utc) == now + timedelta(hours=2)
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


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


def test_list_occurrence_actions_use_occurrence_identity_not_card_message(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            user = await _add_user(session_factory)

            done_reminder, done_occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.DELIVERED.value,
                message_id=9401,
                occurrence_at=now,
            )
            assert done_occurrence is not None
            done_callback = parse_callback(
                encode_callback(
                    CallbackAction.DONE,
                    CallbackTarget.OCCURRENCE,
                    done_occurrence.id,
                    0,
                    origin=CallbackOrigin.LIST,
                )
            )
            assert done_callback is not None
            assert _expected_delivery_message_id(done_callback, done_occurrence.id, 9901) is None
            assert await complete_reminder(
                user,
                done_reminder.id,
                expected_revision=0,
                expected_occurrence_id=done_occurrence.id,
                expected_message_id=None,
            )

            snooze_reminder_row, snooze_occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.DELIVERED.value,
                message_id=9402,
                occurrence_at=now + timedelta(minutes=1),
            )
            assert snooze_occurrence is not None
            snoozed = await snooze_reminder(
                user,
                snooze_reminder_row.id,
                expected_revision=0,
                expected_occurrence_id=snooze_occurrence.id,
                expected_message_id=None,
                target_at_utc=now + timedelta(minutes=20),
            )
            assert snoozed is not None
            assert _utc(snoozed.delivery_at_utc) == now + timedelta(minutes=20)

            edit_reminder_row, edit_occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.DELIVERED.value,
                message_id=9403,
                occurrence_at=now + timedelta(minutes=2),
            )
            assert edit_occurrence is not None
            edit_draft = await create_action_draft(
                user,
                edit_reminder_row.id,
                action_type="edit",
                expected_action_revision=0,
                expected_occurrence_id=edit_occurrence.id,
                expected_message_id=None,
                current_step="schedule",
                payload={"text": "edited from list"},
            )
            assert edit_draft is not None
            edited = await reminder_service.apply_edit_draft(user, edit_draft)
            assert edited is not None
            assert edited.text == "edited from list"

            delete_reminder_row, delete_occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.DELIVERED.value,
                message_id=9404,
                occurrence_at=now + timedelta(minutes=3),
            )
            assert delete_occurrence is not None
            assert await cancel_reminder(
                user,
                delete_reminder_row.id,
                expected_revision=0,
                expected_occurrence_id=delete_occurrence.id,
                expected_message_id=None,
            )
            assert (await _get_reminder(session_factory, delete_reminder_row.id)).state == (
                ReminderState.CANCELLED.value
            )

            recurring_snooze, recurring_snooze_occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.SCHEDULED.value,
                recurrence_type=RecurrenceType.DAILY.value,
                canonical_at=now + timedelta(days=1),
                occurrence_at=now + timedelta(minutes=4),
            )
            assert recurring_snooze_occurrence is not None
            child = await snooze_reminder(
                user,
                recurring_snooze.id,
                expected_revision=0,
                expected_occurrence_id=recurring_snooze_occurrence.id,
                expected_message_id=None,
                target_at_utc=now + timedelta(minutes=30),
            )
            assert child is not None
            assert child.parent_reminder_id == recurring_snooze.id

            recurring_pause, recurring_pause_occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.SCHEDULED.value,
                recurrence_type=RecurrenceType.DAILY.value,
                canonical_at=now + timedelta(days=1),
                occurrence_at=now + timedelta(minutes=5),
            )
            assert recurring_pause_occurrence is not None
            assert await pause_reminder(
                user,
                recurring_pause.id,
                expected_revision=0,
                expected_occurrence_id=recurring_pause_occurrence.id,
                expected_message_id=None,
            )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_delivery_stale_occurrence_and_cross_scope_actions_are_rejected(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        try:
            monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
            user = await _add_user(session_factory)
            one_off, delivered_occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.DELIVERED.value,
                message_id=9501,
                occurrence_at=now,
            )
            assert delivered_occurrence is not None
            assert await edit_reminder(
                user,
                one_off.id,
                expected_revision=0,
                text="revision changed",
                expected_occurrence_id=delivered_occurrence.id,
                expected_message_id=9501,
            )
            assert not await complete_reminder(
                user,
                one_off.id,
                expected_revision=0,
                expected_occurrence_id=delivered_occurrence.id,
                expected_message_id=9501,
            )

            recurring, first_occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.SCHEDULED.value,
                recurrence_type=RecurrenceType.DAILY.value,
                canonical_at=now + timedelta(days=1),
                message_id=9502,
                occurrence_at=now,
            )
            assert first_occurrence is not None
            second_at = now + timedelta(hours=1)
            async with session_factory() as session:
                second_occurrence = ReminderOccurrence(
                    reminder_id=recurring.id,
                    occurrence_at_utc=second_at,
                    delivery_at_utc=second_at,
                    status=OccurrenceState.DELIVERED.value,
                    action_revision=0,
                    message_id=9503,
                    delivered_at=now,
                )
                session.add(second_occurrence)
                saved_recurring = await session.get(Reminder, recurring.id)
                assert saved_recurring is not None
                saved_recurring.last_delivery_occurrence_utc = second_at
                saved_recurring.last_message_id = 9503
                await session.commit()

            assert not await complete_reminder(
                user,
                recurring.id,
                expected_revision=0,
                expected_occurrence_id=first_occurrence.id,
                expected_message_id=None,
            )

            other_user = await _add_user(
                session_factory,
                telegram_user_id=1003,
                chat_id=2004,
            )
            assert not await cancel_reminder(
                other_user,
                one_off.id,
                expected_revision=1,
                expected_occurrence_id=delivered_occurrence.id,
                expected_message_id=None,
            )
            assert (await _get_reminder(session_factory, one_off.id)).state == (
                ReminderState.DELIVERED.value
            )
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


def test_action_draft_replaces_other_flow_for_same_user_chat(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _open_sqlite(monkeypatch)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
        monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
        try:
            user = await _add_user(session_factory)
            other_user = await _add_user(
                session_factory,
                telegram_user_id=1201,
                chat_id=2202,
            )
            first, first_occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.DELIVERED.value,
                message_id=9601,
                occurrence_at=now,
            )
            second, second_occurrence = await _add_reminder(
                session_factory,
                user,
                now=now,
                state=ReminderState.DELIVERED.value,
                message_id=9602,
                occurrence_at=now + timedelta(minutes=1),
            )
            other_reminder, other_occurrence = await _add_reminder(
                session_factory,
                other_user,
                now=now,
                state=ReminderState.DELIVERED.value,
                message_id=9603,
                occurrence_at=now + timedelta(minutes=2),
            )
            assert first_occurrence is not None
            assert second_occurrence is not None
            assert other_occurrence is not None

            snooze_draft = await create_action_draft(
                user,
                first.id,
                action_type="snooze",
                expected_action_revision=0,
                expected_occurrence_id=first_occurrence.id,
                expected_message_id=None,
                current_step="time",
            )
            assert snooze_draft is not None
            edit_draft = await create_action_draft(
                user,
                second.id,
                action_type="edit",
                expected_action_revision=0,
                expected_occurrence_id=second_occurrence.id,
                expected_message_id=None,
                current_step="schedule",
                payload={"text": "new text"},
            )
            assert edit_draft is not None
            active = await get_active_action_draft(user)
            assert active is not None
            assert active.id == edit_draft.id
            assert active.action_type == "edit"

            assert await reminder_service.apply_edit_draft(user, edit_draft)
            assert await get_active_action_draft(user) is None

            edit_again = await create_action_draft(
                user,
                first.id,
                action_type="edit",
                expected_action_revision=0,
                expected_occurrence_id=first_occurrence.id,
                expected_message_id=None,
                current_step="text",
            )
            assert edit_again is not None
            snooze_again = await create_action_draft(
                user,
                second.id,
                action_type="snooze",
                expected_action_revision=1,
                expected_occurrence_id=second_occurrence.id,
                expected_message_id=None,
                current_step="time",
            )
            assert snooze_again is not None
            active = await get_active_action_draft(user)
            assert active is not None
            assert active.id == snooze_again.id
            assert active.action_type == "snooze"
            assert await reminder_service.apply_custom_snooze_draft(
                user,
                snooze_again,
                "2026-09-08 12:00",
            )
            assert await get_active_action_draft(user) is None

            other_draft = await create_action_draft(
                other_user,
                other_reminder.id,
                action_type="snooze",
                expected_action_revision=0,
                expected_occurrence_id=other_occurrence.id,
                expected_message_id=None,
                current_step="time",
            )
            assert other_draft is not None
            active_for_cancel = await create_action_draft(
                user,
                first.id,
                action_type="edit",
                expected_action_revision=0,
                expected_occurrence_id=first_occurrence.id,
                expected_message_id=None,
                current_step="text",
            )
            assert active_for_cancel is not None
            assert await cancel_active_action_drafts(user) == 1
            assert await get_active_action_draft(user) is None
            other_active = await get_active_action_draft(other_user)
            assert other_active is not None
            assert other_active.id == other_draft.id
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
