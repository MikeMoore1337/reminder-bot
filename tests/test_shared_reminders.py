from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.callbacks import CallbackAction, CallbackOrigin, CallbackTarget, parse_callback
from app.db.base import Base
from app.db.models import (
    OccurrenceState,
    RecurrenceType,
    Reminder,
    ReminderDelivery,
    ReminderDeliveryState,
    ReminderKind,
    ReminderOccurrence,
    ReminderState,
    SharedInviteState,
    SharedMembershipState,
    SharedReminderInvite,
    SharedReminderMembership,
    User,
)
from app.services import reminder_service, shared_reminder_service
from app.services.message_context import MessageContextSnapshot
from app.workers import reminder_worker as worker

NOW = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)


def _settings(**overrides):
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
    monkeypatch.setattr(shared_reminder_service, "SessionLocal", session_factory)
    monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    return engine, connection, session_factory


async def _add_user(
    session_factory,
    *,
    telegram_user_id: int,
    chat_id: int,
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
    owner: User,
    *,
    now: datetime,
    text: str = "shared reminder",
    state: str = ReminderState.SCHEDULED.value,
    status: str = "pending",
    delivery_at_utc: datetime | None = None,
    recurrence_type: str = RecurrenceType.NONE.value,
    mode: str = "normal",
    kind: str = ReminderKind.ORDINARY.value,
    context_kind: str | None = None,
    action_revision: int = 0,
) -> int:
    delivered = state == ReminderState.DELIVERED.value
    async with session_factory() as session:
        reminder = Reminder(
            user_id=owner.id,
            chat_id=owner.chat_id,
            text=text,
            remind_at_utc=now,
            delivery_at_utc=None if delivered else (delivery_at_utc or now),
            schedule_timezone=owner.timezone,
            status="sent" if delivered else status,
            state=state,
            recurrence_type=recurrence_type,
            recurrence_interval=1,
            mode=mode,
            kind=kind,
            context_kind=context_kind,
            action_revision=action_revision,
            last_message_id=700 if delivered else None,
            last_delivery_occurrence_utc=now if delivered else None,
            sent_at=now if delivered else None,
        )
        session.add(reminder)
        await session.flush()
        reminder_id = reminder.id
        await session.commit()
    return reminder_id


async def _add_delivered_shared(
    session_factory,
    owner: User,
    participant: User,
    *,
    now: datetime,
    action_revision: int = 4,
) -> tuple[int, int, int, int]:
    reminder_id = await _add_reminder(
        session_factory,
        owner,
        now=now,
        state=ReminderState.DELIVERED.value,
        action_revision=action_revision,
    )
    async with session_factory() as session:
        occurrence = ReminderOccurrence(
            reminder_id=reminder_id,
            occurrence_at_utc=now,
            delivery_at_utc=now,
            status=OccurrenceState.DELIVERED.value,
            action_revision=action_revision,
            message_id=700,
            delivered_at=now,
        )
        membership = SharedReminderMembership(
            reminder_id=reminder_id,
            user_id=participant.id,
            role="participant",
            state=SharedMembershipState.ACTIVE.value,
            revision=1,
            joined_at=now,
        )
        session.add_all([occurrence, membership])
        await session.flush()
        delivery = ReminderDelivery(
            reminder_id=reminder_id,
            occurrence_id=occurrence.id,
            recipient_user_id=participant.id,
            membership_revision=1,
            action_revision=action_revision,
            chat_id=participant.chat_id,
            state=ReminderDeliveryState.SENT.value,
            message_id=777,
            sent_at=now,
        )
        session.add(delivery)
        await session.commit()
        return reminder_id, occurrence.id, membership.id, delivery.id


@pytest.mark.asyncio
async def test_shared_invites_are_scoped_hashed_expiring_and_revocable(monkeypatch) -> None:
    engine, connection, session_factory = await _open_sqlite(monkeypatch)
    try:
        owner = await _add_user(session_factory, telegram_user_id=1001, chat_id=2001)
        guest = await _add_user(session_factory, telegram_user_id=1002, chat_id=2002)
        outsider = await _add_user(session_factory, telegram_user_id=1003, chat_id=2003)
        reminder_id = await _add_reminder(session_factory, owner, now=NOW + timedelta(hours=1))

        invite = await shared_reminder_service.create_invite(owner, reminder_id, now_utc=NOW)
        assert shared_reminder_service.invite_start_payload(invite.token).startswith("sr_")

        async with session_factory() as session:
            stored = await session.scalar(
                select(SharedReminderInvite).where(SharedReminderInvite.reminder_id == reminder_id)
            )
            assert stored is not None
            assert stored.token_hash != invite.token
            assert len(stored.token_hash) == 64

        accepted = await shared_reminder_service.accept_invite(
            guest,
            shared_reminder_service.invite_start_payload(invite.token),
            now_utc=NOW,
        )
        assert accepted.accepted is True
        repeated = await shared_reminder_service.accept_invite(guest, invite.token, now_utc=NOW)
        assert repeated.accepted is False
        assert repeated.already_member is True

        assert len(await shared_reminder_service.list_shared_reminders(guest)) == 1
        assert await shared_reminder_service.list_shared_reminders(outsider) == []
        owner_view = await shared_reminder_service.list_shared_reminders(owner)
        assert len(owner_view) == 1
        assert owner_view[0].members[0].membership_id > 0
        assert not hasattr(owner_view[0].members[0], "user_id")

        second_invite = await shared_reminder_service.create_invite(owner, reminder_id, now_utc=NOW)
        expired_user = await _add_user(session_factory, telegram_user_id=1004, chat_id=2004)
        expired = await shared_reminder_service.accept_invite(
            expired_user,
            second_invite.token,
            now_utc=second_invite.expires_at + timedelta(seconds=1),
        )
        assert expired.accepted is False
        assert expired.reason == "expired"

        membership_id = owner_view[0].members[0].membership_id
        assert await shared_reminder_service.revoke_membership(
            owner,
            membership_id,
            expected_revision=1,
            now_utc=NOW,
        )
        assert not await shared_reminder_service.revoke_membership(
            owner,
            membership_id,
            expected_revision=1,
            now_utc=NOW,
        )
        old_token = await shared_reminder_service.accept_invite(guest, invite.token, now_utc=NOW)
        assert old_token.accepted is False
        async with session_factory() as session:
            membership = await session.get(SharedReminderMembership, membership_id)
            assert membership is not None
            assert membership.state == SharedMembershipState.REVOKED.value
        assert await shared_reminder_service.list_shared_reminders(guest) == []

        third_invite = await shared_reminder_service.create_invite(owner, reminder_id, now_utc=NOW)
        async with session_factory() as session:
            pending = await session.scalar(
                select(SharedReminderInvite).where(
                    SharedReminderInvite.token_hash
                    == shared_reminder_service._token_hash(third_invite.token)
                )
            )
            assert pending is not None
            pending_id = pending.id

        private_reminder_id = await _add_reminder(
            session_factory, owner, now=NOW + timedelta(hours=2)
        )
        owner_reminder_ids = {
            view.reminder.id for view in await shared_reminder_service.list_shared_reminders(owner)
        }
        assert reminder_id in owner_reminder_ids
        assert private_reminder_id not in owner_reminder_ids

        assert await shared_reminder_service.revoke_invite(
            owner,
            pending_id,
            expected_revision=1,
            now_utc=NOW,
        )
        assert (
            await shared_reminder_service.accept_invite(outsider, third_invite.token, now_utc=NOW)
        ).reason == "invalid"
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_shared_scope_rejects_recurrence_persistent_and_deadline_modes(monkeypatch) -> None:
    engine, connection, session_factory = await _open_sqlite(monkeypatch)
    try:
        owner = await _add_user(session_factory, telegram_user_id=1101, chat_id=2101)
        recurring = await _add_reminder(
            session_factory,
            owner,
            now=NOW,
            recurrence_type=RecurrenceType.DAILY.value,
        )
        persistent = await _add_reminder(
            session_factory,
            owner,
            now=NOW,
            mode="persistent",
        )
        deadline = await _add_reminder(
            session_factory,
            owner,
            now=NOW,
            kind=ReminderKind.DEADLINE.value,
        )
        for reminder_id in (recurring, persistent, deadline):
            with pytest.raises(shared_reminder_service.SharedReminderError):
                await shared_reminder_service.create_invite(owner, reminder_id, now_utc=NOW)
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_shared_participant_and_pending_invite_limits_are_bounded(monkeypatch) -> None:
    engine, connection, session_factory = await _open_sqlite(monkeypatch)
    try:
        owner = await _add_user(session_factory, telegram_user_id=1151, chat_id=2151)
        reminder_id = await _add_reminder(session_factory, owner, now=NOW)
        pending_invites = [
            await shared_reminder_service.create_invite(owner, reminder_id, now_utc=NOW)
            for _ in range(shared_reminder_service.MAX_PENDING_SHARED_INVITES)
        ]
        with pytest.raises(shared_reminder_service.SharedReminderError, match="приглашений"):
            await shared_reminder_service.create_invite(owner, reminder_id, now_utc=NOW)

        guests = [
            await _add_user(session_factory, telegram_user_id=1152 + index, chat_id=2152 + index)
            for index in range(shared_reminder_service.MAX_SHARED_PARTICIPANTS + 1)
        ]
        for guest, invite in zip(
            guests[: shared_reminder_service.MAX_SHARED_PARTICIPANTS],
            pending_invites,
            strict=True,
        ):
            assert (
                await shared_reminder_service.accept_invite(guest, invite.token, now_utc=NOW)
            ).accepted
        with pytest.raises(shared_reminder_service.SharedReminderError, match="лимит участников"):
            await shared_reminder_service.create_invite(owner, reminder_id, now_utc=NOW)
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_participant_actions_require_revision_message_and_are_idempotent(monkeypatch) -> None:
    engine, connection, session_factory = await _open_sqlite(monkeypatch)
    try:
        owner = await _add_user(session_factory, telegram_user_id=1201, chat_id=2201)
        participant = await _add_user(session_factory, telegram_user_id=1202, chat_id=2202)
        reminder_id, occurrence_id, membership_id, _delivery_id = await _add_delivered_shared(
            session_factory, owner, participant, now=NOW
        )
        assert not await shared_reminder_service.validate_shared_action_target(
            participant,
            reminder_id,
            occurrence_id,
            expected_revision=4,
            expected_message_id=778,
        )
        assert await shared_reminder_service.validate_shared_action_target(
            participant,
            reminder_id,
            occurrence_id,
            expected_revision=4,
            expected_message_id=777,
        )
        assert await shared_reminder_service.complete_shared_reminder(
            participant,
            reminder_id,
            occurrence_id,
            expected_revision=4,
            expected_message_id=777,
            now_utc=NOW,
        )
        assert not await shared_reminder_service.complete_shared_reminder(
            participant,
            reminder_id,
            occurrence_id,
            expected_revision=4,
            expected_message_id=777,
            now_utc=NOW,
        )
        async with session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            occurrence = await session.get(ReminderOccurrence, occurrence_id)
            membership = await session.get(SharedReminderMembership, membership_id)
            assert reminder is not None and reminder.state == ReminderState.COMPLETED.value
            assert occurrence is not None and occurrence.status == OccurrenceState.COMPLETED.value
            assert membership is not None and membership.state == SharedMembershipState.ACTIVE.value
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_participant_snooze_invalidates_previous_callback_generation(monkeypatch) -> None:
    engine, connection, session_factory = await _open_sqlite(monkeypatch)
    try:
        owner = await _add_user(session_factory, telegram_user_id=1301, chat_id=2301)
        participant = await _add_user(session_factory, telegram_user_id=1302, chat_id=2302)
        reminder_id, occurrence_id, _membership_id, _delivery_id = await _add_delivered_shared(
            session_factory, owner, participant, now=NOW
        )
        target = NOW + timedelta(hours=1)
        assert await shared_reminder_service.snooze_shared_reminder(
            participant,
            reminder_id,
            occurrence_id,
            target,
            expected_revision=4,
            expected_message_id=777,
            now_utc=NOW,
        )
        assert not await shared_reminder_service.validate_shared_action_target(
            participant,
            reminder_id,
            occurrence_id,
            expected_revision=4,
            expected_message_id=777,
        )
        async with session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            occurrence = await session.get(ReminderOccurrence, occurrence_id)
            delivery = await session.scalar(
                select(ReminderDelivery).where(ReminderDelivery.occurrence_id == occurrence_id)
            )
            assert reminder is not None and reminder.state == ReminderState.SNOOZED.value
            assert occurrence is not None and occurrence.status == OccurrenceState.SNOOZED.value
            assert delivery is not None and delivery.state == ReminderDeliveryState.PENDING.value
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_owner_snooze_resets_shared_recipient_delivery_rows(monkeypatch) -> None:
    engine, connection, session_factory = await _open_sqlite(monkeypatch)
    try:
        owner = await _add_user(session_factory, telegram_user_id=1351, chat_id=2351)
        participant = await _add_user(session_factory, telegram_user_id=1352, chat_id=2352)
        reminder_id, occurrence_id, _membership_id, _delivery_id = await _add_delivered_shared(
            session_factory, owner, participant, now=NOW
        )
        monkeypatch.setattr(reminder_service, "utc_now", lambda: NOW)
        result = await reminder_service.snooze_reminder(
            owner,
            reminder_id,
            expected_revision=4,
            expected_occurrence_id=occurrence_id,
            expected_message_id=700,
            target_at_utc=NOW + timedelta(hours=1),
        )
        assert result is not None
        async with session_factory() as session:
            delivery = await session.scalar(
                select(ReminderDelivery).where(ReminderDelivery.occurrence_id == occurrence_id)
            )
            assert delivery is not None
            assert delivery.state == ReminderDeliveryState.PENDING.value
            assert delivery.message_id is None
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_owner_cannot_convert_active_shared_oneoff_to_recurring(monkeypatch) -> None:
    engine, connection, session_factory = await _open_sqlite(monkeypatch)
    try:
        owner = await _add_user(session_factory, telegram_user_id=1361, chat_id=2361)
        participant = await _add_user(session_factory, telegram_user_id=1362, chat_id=2362)
        reminder_id = await _add_reminder(session_factory, owner, now=NOW + timedelta(days=1))
        invite = await shared_reminder_service.create_invite(owner, reminder_id, now_utc=NOW)
        assert (
            await shared_reminder_service.accept_invite(participant, invite.token, now_utc=NOW)
        ).accepted
        monkeypatch.setattr(reminder_service, "utc_now", lambda: NOW)
        with pytest.raises(ValueError, match="общего напоминания"):
            await reminder_service.edit_reminder(
                owner,
                reminder_id,
                expected_revision=0,
                local_dt=datetime(2026, 9, 9, 15, 0),
                recurrence_type=RecurrenceType.DAILY.value,
            )
        async with session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            assert reminder is not None
            assert reminder.recurrence_type == RecurrenceType.NONE.value
    finally:
        await connection.close()
        await engine.dispose()


class _FakeBot:
    def __init__(self, *, fail_chat_id: int | None = None) -> None:
        self.fail_chat_id = fail_chat_id
        self.calls: list[dict[str, object]] = []
        self._message_id = 1000

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["chat_id"] == self.fail_chat_id:
            raise ConnectionError("synthetic recipient outage")
        self._message_id += 1
        return SimpleNamespace(message_id=self._message_id)


def _callback_actions(call: dict[str, object]) -> set[CallbackAction]:
    markup = call["reply_markup"]
    return {
        parsed.action
        for row in markup.inline_keyboard
        for button in row
        if (parsed := parse_callback(button.callback_data)) is not None
    }


@pytest.mark.asyncio
async def test_shared_worker_fanout_is_bounded_private_and_idempotent(monkeypatch) -> None:
    engine, connection, session_factory = await _open_sqlite(monkeypatch)
    try:
        owner = await _add_user(session_factory, telegram_user_id=1401, chat_id=2401)
        guest_a = await _add_user(session_factory, telegram_user_id=1402, chat_id=2402)
        guest_b = await _add_user(session_factory, telegram_user_id=1403, chat_id=2403)
        reminder_id = await _add_reminder(
            session_factory,
            owner,
            now=NOW,
            text="private reminder text",
            context_kind="forwarded",
        )
        for guest in (guest_a, guest_b):
            invite = await shared_reminder_service.create_invite(owner, reminder_id, now_utc=NOW)
            assert (
                await shared_reminder_service.accept_invite(guest, invite.token, now_utc=NOW)
            ).accepted

        async def fake_context(*args, **kwargs):
            return MessageContextSnapshot(kind="forwarded", source_text="OWNER SECRET CONTEXT")

        monkeypatch.setattr(worker, "get_context_for_delivery", fake_context)
        monkeypatch.setattr(worker, "settings", _settings())
        monkeypatch.setattr(worker, "utc_now", lambda: NOW)
        monkeypatch.setattr(reminder_service, "utc_now", lambda: NOW)
        monkeypatch.setattr(shared_reminder_service, "utc_now", lambda: NOW)
        bot = _FakeBot()

        assert await worker.process_due_reminders(bot) == 1
        assert len(bot.calls) == 3
        owner_call = next(call for call in bot.calls if call["chat_id"] == owner.chat_id)
        participant_calls = [call for call in bot.calls if call["chat_id"] != owner.chat_id]
        assert "OWNER SECRET CONTEXT" in owner_call["text"]
        assert all("OWNER SECRET CONTEXT" not in call["text"] for call in participant_calls)
        assert _callback_actions(owner_call) >= {
            CallbackAction.DONE,
            CallbackAction.SNOOZE,
            CallbackAction.EDIT,
            CallbackAction.DELETE,
        }
        assert all(
            _callback_actions(call) == {CallbackAction.DONE, CallbackAction.SNOOZE}
            for call in participant_calls
        )

        async with session_factory() as session:
            rows = list((await session.scalars(select(ReminderDelivery))).all())
            reminder = await session.get(Reminder, reminder_id)
            assert len(rows) == 3
            assert {row.state for row in rows} == {ReminderDeliveryState.SENT.value}
            assert len({row.message_id for row in rows}) == 3
            assert reminder is not None and reminder.state == ReminderState.DELIVERED.value

        assert await worker.process_due_reminders(bot) == 0
        assert len(bot.calls) == 3
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_shared_worker_retry_preserves_successful_recipient(monkeypatch) -> None:
    engine, connection, session_factory = await _open_sqlite(monkeypatch)
    try:
        owner = await _add_user(session_factory, telegram_user_id=1501, chat_id=2501)
        guest = await _add_user(session_factory, telegram_user_id=1502, chat_id=2502)
        reminder_id = await _add_reminder(session_factory, owner, now=NOW)
        invite = await shared_reminder_service.create_invite(owner, reminder_id, now_utc=NOW)
        assert (
            await shared_reminder_service.accept_invite(guest, invite.token, now_utc=NOW)
        ).accepted

        clock = [NOW]
        monkeypatch.setattr(worker, "settings", _settings())
        monkeypatch.setattr(worker, "utc_now", lambda: clock[0])
        monkeypatch.setattr(reminder_service, "utc_now", lambda: clock[0])
        monkeypatch.setattr(shared_reminder_service, "utc_now", lambda: clock[0])
        bot = _FakeBot(fail_chat_id=guest.chat_id)

        assert await worker.process_due_reminders(bot) == 0
        assert [call["chat_id"] for call in bot.calls] == [owner.chat_id, guest.chat_id]
        async with session_factory() as session:
            first_rows = list((await session.scalars(select(ReminderDelivery))).all())
            failed_reminder = await session.get(Reminder, reminder_id)
            assert {row.state for row in first_rows} == {
                ReminderDeliveryState.SENT.value,
                ReminderDeliveryState.FAILED.value,
            }
            assert failed_reminder is not None
            assert failed_reminder.next_retry_at is not None

        bot.fail_chat_id = None
        clock[0] = NOW + timedelta(seconds=11)
        assert await worker.process_due_reminders(bot) == 1
        assert [call["chat_id"] for call in bot.calls] == [
            owner.chat_id,
            guest.chat_id,
            guest.chat_id,
        ]
        async with session_factory() as session:
            rows = list((await session.scalars(select(ReminderDelivery))).all())
            assert {row.state for row in rows} == {ReminderDeliveryState.SENT.value}
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_shared_cleanup_removes_expired_invites_and_bounded_terminal_data(
    monkeypatch,
) -> None:
    engine, connection, session_factory = await _open_sqlite(monkeypatch)
    try:
        owner = await _add_user(session_factory, telegram_user_id=1601, chat_id=2601)
        participant = await _add_user(session_factory, telegram_user_id=1602, chat_id=2602)
        reminder_id, occurrence_id, membership_id, delivery_id = await _add_delivered_shared(
            session_factory, owner, participant, now=NOW
        )
        invite = await shared_reminder_service.create_invite(owner, reminder_id, now_utc=NOW)
        assert await shared_reminder_service.revoke_membership(
            owner,
            membership_id,
            expected_revision=1,
            now_utc=NOW,
        )
        async with session_factory() as session:
            await session.execute(
                update(SharedReminderInvite)
                .where(
                    SharedReminderInvite.token_hash
                    == shared_reminder_service._token_hash(invite.token)
                )
                .values(
                    state=SharedInviteState.REVOKED.value,
                    expires_at=NOW - timedelta(days=8),
                    created_at=NOW - timedelta(days=8),
                )
            )
            await session.execute(
                update(SharedReminderMembership)
                .where(SharedReminderMembership.id == membership_id)
                .values(revoked_at=NOW - timedelta(days=31))
            )
            await session.execute(
                update(ReminderOccurrence)
                .where(ReminderOccurrence.id == occurrence_id)
                .values(status=OccurrenceState.COMPLETED.value)
            )
            await session.execute(
                update(ReminderDelivery)
                .where(ReminderDelivery.id == delivery_id)
                .values(created_at=NOW - timedelta(days=31))
            )
            await session.commit()
        result = await shared_reminder_service.cleanup_expired_shared_data(
            now_utc=NOW + timedelta(days=31)
        )
        assert result == {
            "expired_invites": 0,
            "deleted_invites": 1,
            "deleted_memberships": 1,
            "deleted_deliveries": 1,
        }
    finally:
        await connection.close()
        await engine.dispose()


def test_shared_callback_protocol_remains_compact_and_scoped() -> None:
    from app.callbacks import encode_callback

    payload = encode_callback(
        CallbackAction.REVOKE,
        CallbackTarget.MEMBERSHIP,
        42,
        7,
        origin=CallbackOrigin.SHARED,
    )
    parsed = parse_callback(payload)
    assert parsed is not None
    assert parsed.action == CallbackAction.REVOKE
    assert parsed.target == CallbackTarget.MEMBERSHIP
    assert parsed.origin == CallbackOrigin.SHARED
    assert len(payload.encode("utf-8")) <= 64
