import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.callbacks import CallbackAction, CallbackOrigin, CallbackTarget, parse_callback
from app.db.base import Base
from app.db.models import (
    DigestDeliveryState,
    Reminder,
    ReminderDigestDelivery,
    ReminderSnoozeEvent,
    ReminderState,
    ReminderSuggestion,
    User,
)
from app.keyboards.adaptive import suggestion_kb
from app.services import adaptive_service, reminder_service
from app.services.adaptive_service import (
    build_digest_text,
    claim_due_digests,
    digest_suppression_reason,
    evaluate_snooze_suggestion,
    finalize_digest_success,
    resolve_suggestion,
    set_adaptive_preferences,
)
from app.services.recurrence import legacy_rule
from app.services.reminder_service import snooze_reminder
from app.utils.datetime_utils import to_utc


def _sqlite_setup(monkeypatch):
    async def setup():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        connection = await engine.connect()
        await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(adaptive_service, "SessionLocal", session_factory)
        return engine, connection, session_factory

    return setup


async def _add_user(session_factory, *, suggestions: bool = True, digests: bool = False) -> User:
    async with session_factory() as session:
        user = User(
            telegram_user_id=1001,
            chat_id=2002,
            timezone="Europe/Moscow",
            suggestions_enabled=suggestions,
            digests_enabled=digests,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _add_recurring_reminder(session_factory, user: User, *, reminder_id: int | None = None):
    async with session_factory() as session:
        reminder = Reminder(
            id=reminder_id,
            user_id=user.id,
            chat_id=user.chat_id,
            text="утренний отчёт",
            remind_at_utc=to_utc(datetime(2026, 9, 11, 8, 0), user.timezone),
            delivery_at_utc=to_utc(datetime(2026, 9, 11, 8, 0), user.timezone),
            schedule_timezone=user.timezone,
            status="pending",
            state=ReminderState.SCHEDULED.value,
            recurrence_type="daily",
            recurrence_interval=1,
            recurrence_rule=adaptive_service.encode_rule(legacy_rule("daily", 1)),
        )
        session.add(reminder)
        await session.commit()
        await session.refresh(reminder)
        return reminder


async def _add_snooze_event(
    session_factory,
    user: User,
    reminder: Reminder,
    *,
    snoozed_at: datetime,
    target_hour: int,
) -> None:
    async with session_factory() as session:
        target = to_utc(
            datetime(snoozed_at.year, snoozed_at.month, snoozed_at.day, target_hour, 0),
            reminder.schedule_timezone,
        )
        session.add(
            ReminderSnoozeEvent(
                user_id=user.id,
                chat_id=user.chat_id,
                reminder_id=reminder.id,
                occurrence_at_utc=reminder.remind_at_utc,
                snoozed_at_utc=snoozed_at,
                target_at_utc=target,
                target_local_minutes=target_hour * 60,
                schedule_timezone=reminder.schedule_timezone,
            )
        )
        await session.commit()


def test_snooze_threshold_requires_consistent_opt_in_evidence(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
        try:
            user = await _add_user(session_factory)
            reminder = await _add_recurring_reminder(session_factory, user)
            for offset in (3, 2, 1):
                await _add_snooze_event(
                    session_factory,
                    user,
                    reminder,
                    snoozed_at=now - timedelta(days=offset),
                    target_hour=9,
                )
            suggestion = await evaluate_snooze_suggestion(user, reminder.id, now_utc=now)
            assert suggestion is not None
            assert suggestion.evidence_count == 3
            assert suggestion.current_local_minutes == 8 * 60
            assert suggestion.proposed_local_minutes == 9 * 60

            scattered = await _add_recurring_reminder(session_factory, user, reminder_id=99)
            for offset, target_hour in ((3, 9), (2, 10), (1, 11)):
                await _add_snooze_event(
                    session_factory,
                    user,
                    scattered,
                    snoozed_at=now - timedelta(days=offset),
                    target_hour=target_hour,
                )
            assert await evaluate_snooze_suggestion(user, scattered.id, now_utc=now) is None
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_suggestion_keyboard_is_bounded_and_server_scoped() -> None:
    markup = suggestion_kb(42, 3)
    callbacks = [
        parse_callback(button.callback_data) for row in markup.inline_keyboard for button in row
    ]
    assert all(callback is not None for callback in callbacks)
    assert {callback.action for callback in callbacks if callback is not None} == {
        CallbackAction.SUGGESTION_ACCEPT,
        CallbackAction.SUGGESTION_REJECT,
        CallbackAction.SUGGESTION_DISMISS,
    }
    assert all(
        callback.target == CallbackTarget.SUGGESTION
        and callback.origin == CallbackOrigin.SUGGESTION
        and callback.target_id == 42
        and callback.revision == 3
        for callback in callbacks
        if callback is not None
    )


def test_snooze_service_records_opt_in_evidence_and_creates_suggestion(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 10, 5, 0, tzinfo=UTC)
        monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
        monkeypatch.setattr(reminder_service, "utc_now", lambda: now)
        try:
            user = await _add_user(session_factory)
            reminder = await _add_recurring_reminder(session_factory, user)
            target = now + timedelta(hours=1)

            for _ in range(3):
                result = await snooze_reminder(user, reminder.id, target_at_utc=target)
                assert result is not None

            async with session_factory() as session:
                events = list(
                    (
                        await session.scalars(
                            select(ReminderSnoozeEvent).where(
                                ReminderSnoozeEvent.reminder_id == reminder.id
                            )
                        )
                    ).all()
                )
                suggestions = list(
                    (
                        await session.scalars(
                            select(ReminderSuggestion).where(
                                ReminderSuggestion.reminder_id == reminder.id
                            )
                        )
                    ).all()
                )
            assert len(events) == 3
            assert len(suggestions) == 1
            assert suggestions[0].status == "pending"
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_snooze_suggestion_resolution_is_user_scoped_and_idempotent(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
        try:
            user = await _add_user(session_factory)
            reminder = await _add_recurring_reminder(session_factory, user)
            for offset in (3, 2, 1):
                await _add_snooze_event(
                    session_factory,
                    user,
                    reminder,
                    snoozed_at=now - timedelta(days=offset),
                    target_hour=9,
                )
            suggestion = await evaluate_snooze_suggestion(user, reminder.id, now_utc=now)
            assert suggestion is not None

            accepted = await resolve_suggestion(
                user,
                suggestion.id,
                expected_revision=suggestion.revision,
                action="accept",
                now_utc=now,
            )
            assert accepted.status == "accepted"
            assert accepted.changed
            assert accepted.proposed_local_time == "09:00"

            repeated = await resolve_suggestion(
                user,
                suggestion.id,
                expected_revision=suggestion.revision,
                action="accept",
                now_utc=now,
            )
            assert repeated.status == "accepted"
            assert repeated.already_resolved
            assert not repeated.changed

            async with session_factory() as session:
                saved = await session.get(Reminder, reminder.id)
                assert saved is not None
                assert saved.action_revision == 1
                assert saved.status == "pending"
                assert (
                    adaptive_service.from_utc_to_user(
                        saved.remind_at_utc, saved.schedule_timezone
                    ).strftime("%H:%M")
                    == "09:00"
                )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_suggestion_accept_fails_closed_while_worker_owns_reminder(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
        try:
            user = await _add_user(session_factory)
            reminder = await _add_recurring_reminder(session_factory, user)
            for offset in (3, 2, 1):
                await _add_snooze_event(
                    session_factory,
                    user,
                    reminder,
                    snoozed_at=now - timedelta(days=offset),
                    target_hour=9,
                )
            suggestion = await evaluate_snooze_suggestion(user, reminder.id, now_utc=now)
            assert suggestion is not None
            async with session_factory() as session:
                saved = await session.get(Reminder, reminder.id)
                assert saved is not None
                saved.status = "processing"
                await session.commit()

            resolution = await resolve_suggestion(
                user,
                suggestion.id,
                expected_revision=suggestion.revision,
                action="accept",
                now_utc=now,
            )
            assert resolution.status == "expired"
            assert not resolution.changed
            async with session_factory() as session:
                saved = await session.get(Reminder, reminder.id)
                assert saved is not None
                assert saved.status == "processing"
                stored_suggestion = await session.get(ReminderSuggestion, suggestion.id)
                assert stored_suggestion is not None
                assert stored_suggestion.resolution == "reminder_processing"
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_opt_out_revokes_pending_suggestions_and_digest_slots(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 10, 7, 0, tzinfo=UTC)
        monkeypatch.setattr(adaptive_service, "utc_now", lambda: now)
        try:
            user = await _add_user(session_factory, suggestions=True, digests=True)
            reminder = await _add_recurring_reminder(session_factory, user)
            async with session_factory() as session:
                suggestion = ReminderSuggestion(
                    user_id=user.id,
                    chat_id=user.chat_id,
                    reminder_id=reminder.id,
                    kind=adaptive_service.SUGGESTION_KIND_SCHEDULE_TIME,
                    status="pending",
                    revision=1,
                    expected_reminder_revision=reminder.action_revision,
                    current_local_minutes=8 * 60,
                    proposed_local_minutes=9 * 60,
                    evidence_count=3,
                    evidence_window_start_utc=now - timedelta(days=3),
                    evidence_window_end_utc=now,
                    dedupe_key="test:opt-out:suggestion",
                )
                delivery = ReminderDigestDelivery(
                    user_id=user.id,
                    chat_id=user.chat_id,
                    period="morning",
                    local_date=date(2026, 9, 10),
                    scheduled_at_utc=now,
                    state=DigestDeliveryState.PENDING.value,
                )
                session.add_all([suggestion, delivery])
                await session.commit()

            preferences = await set_adaptive_preferences(
                user,
                suggestions_enabled=False,
                digests_enabled=False,
            )
            assert preferences is not None
            async with session_factory() as session:
                saved_suggestion = await session.get(ReminderSuggestion, suggestion.id)
                saved_delivery = await session.get(ReminderDigestDelivery, delivery.id)
                assert saved_suggestion is not None
                assert saved_suggestion.status == "dismissed"
                assert saved_suggestion.resolution == "opt_out"
                assert saved_delivery is not None
                assert saved_delivery.state == DigestDeliveryState.SUPPRESSED.value
                assert saved_delivery.suppression_reason == "opt_out"
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_digest_is_timezone_aware_bounded_idempotent_and_keeps_important(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 10, 7, 0, tzinfo=UTC)
        monkeypatch.setattr(
            adaptive_service,
            "settings",
            SimpleNamespace(
                digest_max_items=1,
                digest_max_delay_minutes=360,
                digest_morning_time="09:00",
                digest_evening_time="20:00",
                digest_quiet_hours_start="22:00",
                digest_quiet_hours_end="08:00",
                digest_lease_duration_seconds=60,
                worker_max_attempts=3,
                worker_batch_size=10,
                worker_send_timeout_seconds=1,
                worker_retry_base_seconds=1,
                worker_retry_max_seconds=10,
            ),
        )
        try:
            user = await _add_user(session_factory, suggestions=False, digests=True)
            async with session_factory() as session:
                ordinary = Reminder(
                    user_id=user.id,
                    chat_id=user.chat_id,
                    text="обычная задача",
                    remind_at_utc=now + timedelta(hours=1),
                    delivery_at_utc=now + timedelta(hours=1),
                    schedule_timezone=user.timezone,
                    status="pending",
                    state=ReminderState.SCHEDULED.value,
                    recurrence_type="none",
                    recurrence_interval=1,
                    mode="normal",
                )
                second_ordinary = Reminder(
                    user_id=user.id,
                    chat_id=user.chat_id,
                    text="вторая обычная задача",
                    remind_at_utc=now + timedelta(hours=2),
                    delivery_at_utc=now + timedelta(hours=2),
                    schedule_timezone=user.timezone,
                    status="pending",
                    state=ReminderState.SCHEDULED.value,
                    recurrence_type="none",
                    recurrence_interval=1,
                    mode="normal",
                )
                important = Reminder(
                    user_id=user.id,
                    chat_id=user.chat_id,
                    text="важная задача",
                    remind_at_utc=now + timedelta(hours=3),
                    delivery_at_utc=now + timedelta(hours=3),
                    schedule_timezone=user.timezone,
                    status="pending",
                    state=ReminderState.SCHEDULED.value,
                    recurrence_type="none",
                    recurrence_interval=1,
                    mode="persistent",
                )
                session.add_all([ordinary, second_ordinary, important])
                await session.commit()
                await session.refresh(important)

            claimed = await claim_due_digests(10, now_utc=now)
            assert len(claimed) == 1
            assert claimed[0].period == "morning"
            assert await claim_due_digests(10, now_utc=now) == []
            digest = await build_digest_text(user, "morning", local_date=date(2026, 9, 10))
            assert "обычная задача" in digest
            assert "важная задача" in digest
            assert "ВАЖНОЕ" in digest
            assert "вторая обычная задача" not in digest

            assert await finalize_digest_success(
                claimed[0].id,
                claimed[0].lease_token or "",
                7001,
                now_utc=now,
            )
            async with session_factory() as session:
                saved = await session.scalar(
                    select(ReminderDigestDelivery).where(ReminderDigestDelivery.id == claimed[0].id)
                )
                assert saved is not None
                assert saved.state == DigestDeliveryState.SENT.value
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_process_due_digest_sends_and_finalizes_with_a_bot(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        now = datetime(2026, 9, 10, 7, 0, tzinfo=UTC)
        monkeypatch.setattr(adaptive_service, "utc_now", lambda: now)
        monkeypatch.setattr(
            adaptive_service,
            "settings",
            SimpleNamespace(
                digest_max_items=20,
                digest_max_delay_minutes=360,
                digest_morning_time="09:00",
                digest_evening_time="20:00",
                digest_quiet_hours_start="22:00",
                digest_quiet_hours_end="08:00",
                digest_lease_duration_seconds=60,
                worker_max_attempts=3,
                worker_batch_size=10,
                worker_send_timeout_seconds=1,
                worker_retry_base_seconds=1,
                worker_retry_max_seconds=10,
            ),
        )

        class FakeBot:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str, str | None]] = []

            async def send_message(
                self,
                *,
                chat_id: int,
                text: str,
                parse_mode: str | None = None,
            ):
                self.messages.append((chat_id, text, parse_mode))
                return SimpleNamespace(message_id=7002)

        try:
            user = await _add_user(session_factory, suggestions=False, digests=True)
            async with session_factory() as session:
                session.add(
                    Reminder(
                        user_id=user.id,
                        chat_id=user.chat_id,
                        text="утренний список",
                        remind_at_utc=now + timedelta(hours=1),
                        delivery_at_utc=now + timedelta(hours=1),
                        schedule_timezone=user.timezone,
                        status="pending",
                        state=ReminderState.SCHEDULED.value,
                        recurrence_type="none",
                        recurrence_interval=1,
                        mode="normal",
                    )
                )
                await session.commit()

            bot = FakeBot()
            assert await adaptive_service.process_due_digests(bot, limit=10) == 1
            assert len(bot.messages) == 1
            assert bot.messages[0][0] == user.chat_id
            assert "утренний список" in bot.messages[0][1]
            assert bot.messages[0][2] == "HTML"
            async with session_factory() as session:
                delivery = await session.scalar(select(ReminderDigestDelivery))
                assert delivery is not None
                assert delivery.state == DigestDeliveryState.SENT.value
                assert delivery.message_id == 7002
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_digest_quiet_hours_and_history_cleanup_are_fail_closed(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory = await _sqlite_setup(monkeypatch)()
        try:
            user = await _add_user(session_factory, suggestions=False, digests=True)
            reminder = await _add_recurring_reminder(session_factory, user)
            quiet_now = datetime(2026, 9, 10, 5, 0, tzinfo=UTC)
            scheduled = to_utc(datetime(2026, 9, 10, 9, 0), user.timezone)
            assert (
                digest_suppression_reason(
                    user,
                    "morning",
                    scheduled_at_utc=scheduled,
                    now_utc=quiet_now,
                )
                == "not_due"
            )
            assert (
                digest_suppression_reason(
                    user,
                    "morning",
                    scheduled_at_utc=quiet_now - timedelta(hours=1),
                    now_utc=quiet_now,
                )
                == "quiet_hours"
            )

            old = datetime(2026, 1, 1, tzinfo=UTC)
            async with session_factory() as session:
                session.add(
                    ReminderSnoozeEvent(
                        user_id=user.id,
                        chat_id=user.chat_id,
                        reminder_id=reminder.id,
                        occurrence_at_utc=old,
                        snoozed_at_utc=old,
                        target_at_utc=old + timedelta(minutes=10),
                        target_local_minutes=10,
                        schedule_timezone=user.timezone,
                    )
                )
                await session.commit()
            cleaned = await adaptive_service.cleanup_adaptive_data(
                now_utc=datetime(2026, 9, 10, tzinfo=UTC)
            )
            assert cleaned["snooze_events"] == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())
