from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused")

from app.db.models import (
    OccurrenceState,
    Reminder,
    ReminderDelivery,
    ReminderDeliveryState,
    ReminderOccurrence,
    ReminderState,
    SharedMembershipState,
    SharedReminderMembership,
    User,
)
from app.handlers import shared as shared_handler
from app.services import reminder_service, shared_reminder_service

POSTGRES_URL = os.environ.get("REMINDER_BOT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set REMINDER_BOT_TEST_DATABASE_URL to run PostgreSQL integration tests",
)

NOW = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
OWNER_TELEGRAM_ID = 9_914_000_001
PARTICIPANT_TELEGRAM_ID = 9_914_000_002


async def _open_postgres():
    assert POSTGRES_URL is not None
    engine = create_async_engine(POSTGRES_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session, session.begin():
        await session.execute(
            delete(User).where(
                User.telegram_user_id.in_((OWNER_TELEGRAM_ID, PARTICIPANT_TELEGRAM_ID))
            )
        )
        owner = User(
            telegram_user_id=OWNER_TELEGRAM_ID,
            chat_id=9_914_000_101,
            timezone="Europe/Moscow",
        )
        participant = User(
            telegram_user_id=PARTICIPANT_TELEGRAM_ID,
            chat_id=9_914_000_102,
            timezone="Europe/Moscow",
        )
        session.add_all([owner, participant])
        await session.flush()
        reminders: list[Reminder] = []
        for index in range(2):
            reminder = Reminder(
                user_id=owner.id,
                chat_id=owner.chat_id,
                text=f"concurrent shared {index}",
                remind_at_utc=NOW,
                delivery_at_utc=None,
                schedule_timezone=owner.timezone,
                status="sent",
                state=ReminderState.DELIVERED.value,
                action_revision=4,
                last_message_id=700 + index,
                last_delivery_occurrence_utc=NOW,
                sent_at=NOW,
            )
            session.add(reminder)
            reminders.append(reminder)
        await session.flush()
        occurrence_ids: list[int] = []
        for index, reminder in enumerate(reminders):
            occurrence = ReminderOccurrence(
                reminder_id=reminder.id,
                occurrence_at_utc=NOW,
                delivery_at_utc=NOW,
                status=OccurrenceState.DELIVERED.value,
                action_revision=4,
                message_id=700 + index,
                delivered_at=NOW,
            )
            membership = SharedReminderMembership(
                reminder_id=reminder.id,
                user_id=participant.id,
                role="participant",
                state=SharedMembershipState.ACTIVE.value,
                revision=1,
                joined_at=NOW,
            )
            session.add_all([occurrence, membership])
            await session.flush()
            occurrence_ids.append(occurrence.id)
            session.add(
                ReminderDelivery(
                    reminder_id=reminder.id,
                    occurrence_id=occurrence.id,
                    recipient_user_id=participant.id,
                    membership_revision=1,
                    action_revision=4,
                    chat_id=participant.chat_id,
                    state=ReminderDeliveryState.SENT.value,
                    message_id=800 + index,
                    sent_at=NOW,
                )
            )
        await session.flush()
        return (
            engine,
            session_factory,
            owner,
            participant,
            [item.id for item in reminders],
            occurrence_ids,
        )


async def _close_postgres(engine, session_factory, owner_id: int, participant_id: int) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(delete(User).where(User.id.in_((owner_id, participant_id))))
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_shared_actions_are_serialized_and_participant_cannot_edit(
    monkeypatch,
) -> None:
    (
        engine,
        session_factory,
        owner,
        participant,
        reminder_ids,
        occurrence_ids,
    ) = await _open_postgres()
    monkeypatch.setattr(shared_reminder_service, "SessionLocal", session_factory)
    monkeypatch.setattr(reminder_service, "SessionLocal", session_factory)
    try:
        complete_results = await asyncio.gather(
            shared_reminder_service.complete_shared_reminder(
                participant,
                reminder_ids[0],
                occurrence_ids[0],
                expected_revision=4,
                expected_message_id=800,
                now_utc=NOW,
            ),
            shared_reminder_service.complete_shared_reminder(
                participant,
                reminder_ids[0],
                occurrence_ids[0],
                expected_revision=4,
                expected_message_id=800,
                now_utc=NOW,
            ),
        )
        assert sorted(complete_results) == [False, True]

        snooze_target = NOW + timedelta(hours=1)
        snooze_results = await asyncio.gather(
            shared_reminder_service.snooze_shared_reminder(
                participant,
                reminder_ids[1],
                occurrence_ids[1],
                snooze_target,
                expected_revision=4,
                expected_message_id=801,
                now_utc=NOW,
            ),
            shared_reminder_service.snooze_shared_reminder(
                participant,
                reminder_ids[1],
                occurrence_ids[1],
                snooze_target,
                expected_revision=4,
                expected_message_id=801,
                now_utc=NOW,
            ),
        )
        assert sorted(snooze_results) == [False, True]

        assert (
            await reminder_service.edit_reminder(
                participant,
                reminder_ids[1],
                expected_revision=4,
                text="participant must not edit",
            )
            is None
        )
        async with session_factory() as session:
            states = list(
                (
                    await session.scalars(
                        select(Reminder.state).where(Reminder.id.in_(reminder_ids))
                    )
                ).all()
            )
            assert set(states) == {
                ReminderState.COMPLETED.value,
                ReminderState.SNOOZED.value,
            }
    finally:
        await _close_postgres(engine, session_factory, owner.id, participant.id)


@pytest.mark.asyncio
async def test_postgres_shared_command_rechecks_membership_before_send(monkeypatch) -> None:
    (
        engine,
        session_factory,
        owner,
        participant,
        reminder_ids,
        _occurrence_ids,
    ) = await _open_postgres()
    monkeypatch.setattr(shared_reminder_service, "SessionLocal", session_factory)
    monkeypatch.setattr(shared_handler, "get_or_create_user", AsyncMock(return_value=participant))
    try:
        async with session_factory() as session:
            membership = await session.scalar(
                select(SharedReminderMembership).where(
                    SharedReminderMembership.reminder_id == reminder_ids[0],
                    SharedReminderMembership.user_id == participant.id,
                    SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
                )
            )
            assert membership is not None
            assert await shared_reminder_service.revoke_membership(
                owner,
                membership.id,
                expected_revision=membership.revision,
                now_utc=NOW,
            )

        answers: list[str] = []

        async def answer(text: str, **kwargs: object) -> None:
            answers.append(text)

        message = SimpleNamespace(
            chat=SimpleNamespace(id=participant.chat_id, type="private"),
            from_user=SimpleNamespace(id=participant.telegram_user_id),
            answer=answer,
        )
        await shared_handler.cmd_shared(message, None)

        assert answers
        assert all("concurrent shared 0" not in text for text in answers)
    finally:
        await _close_postgres(engine, session_factory, owner.id, participant.id)


@pytest.mark.asyncio
async def test_postgres_shared_command_holds_membership_lock_through_send(monkeypatch) -> None:
    (
        engine,
        session_factory,
        owner,
        participant,
        reminder_ids,
        _occurrence_ids,
    ) = await _open_postgres()
    monkeypatch.setattr(shared_reminder_service, "SessionLocal", session_factory)
    monkeypatch.setattr(shared_handler, "get_or_create_user", AsyncMock(return_value=participant))
    revoke_started = asyncio.Event()
    original_revoke = shared_reminder_service.revoke_membership

    async def observed_revoke(*args, **kwargs):
        revoke_started.set()
        return await original_revoke(*args, **kwargs)

    monkeypatch.setattr(shared_reminder_service, "revoke_membership", observed_revoke)
    revoke_task: asyncio.Task[bool] | None = None
    try:
        async with session_factory() as session:
            membership = await session.scalar(
                select(SharedReminderMembership).where(
                    SharedReminderMembership.reminder_id == reminder_ids[0],
                    SharedReminderMembership.user_id == participant.id,
                    SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
                )
            )
            assert membership is not None
            membership_id = membership.id
            membership_revision = membership.revision

        answers: list[str] = []

        async def answer(text: str, **kwargs: object) -> None:
            nonlocal revoke_task
            answers.append(text)
            if revoke_task is not None:
                return
            revoke_task = asyncio.create_task(
                shared_reminder_service.revoke_membership(
                    owner,
                    membership_id,
                    expected_revision=membership_revision,
                    now_utc=NOW,
                )
            )
            await revoke_started.wait()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(revoke_task), timeout=0.1)

        message = SimpleNamespace(
            chat=SimpleNamespace(id=participant.chat_id, type="private"),
            from_user=SimpleNamespace(id=participant.telegram_user_id),
            answer=answer,
        )
        await shared_handler.cmd_shared(message, None)

        assert revoke_task is not None
        assert await asyncio.wait_for(revoke_task, timeout=1.0)
        assert any("concurrent shared 0" in text for text in answers)
        async with session_factory() as session:
            revoked = await session.get(SharedReminderMembership, membership_id)
            assert revoked is not None
            assert revoked.state != SharedMembershipState.ACTIVE.value
    finally:
        if revoke_task is not None and not revoke_task.done():
            revoke_task.cancel()
            await asyncio.gather(revoke_task, return_exceptions=True)
        await _close_postgres(engine, session_factory, owner.id, participant.id)


@pytest.mark.asyncio
async def test_postgres_shared_card_locks_are_scoped_and_owner_is_independent(monkeypatch) -> None:
    (
        engine,
        session_factory,
        owner,
        participant,
        reminder_ids,
        _occurrence_ids,
    ) = await _open_postgres()
    monkeypatch.setattr(shared_reminder_service, "SessionLocal", session_factory)
    try:
        async with session_factory() as session:
            memberships = list(
                (
                    await session.scalars(
                        select(SharedReminderMembership)
                        .where(SharedReminderMembership.user_id == participant.id)
                        .order_by(SharedReminderMembership.reminder_id)
                    )
                ).all()
            )
            assert len(memberships) == 2
            first_membership, second_membership = memberships

        async with shared_reminder_service.authorize_shared_card_send(
            participant,
            reminder_ids[0],
        ) as participant_card:
            assert participant_card is not None
            assert participant_card.entry.is_owner is False
            assert participant_card.entry.membership_id == first_membership.id
            assert await asyncio.wait_for(
                shared_reminder_service.revoke_membership(
                    owner,
                    second_membership.id,
                    expected_revision=second_membership.revision,
                    now_utc=NOW,
                ),
                timeout=1.0,
            )

        async with shared_reminder_service.authorize_shared_card_send(
            owner,
            reminder_ids[0],
        ) as owner_card:
            assert owner_card is not None
            assert owner_card.entry.is_owner is True
            assert await asyncio.wait_for(
                shared_reminder_service.revoke_membership(
                    owner,
                    first_membership.id,
                    expected_revision=first_membership.revision,
                    now_utc=NOW,
                ),
                timeout=1.0,
            )
    finally:
        await _close_postgres(engine, session_factory, owner.id, participant.id)
