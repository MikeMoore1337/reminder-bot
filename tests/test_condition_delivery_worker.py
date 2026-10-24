"""I4 GitHub review alerts: outbox, crash recovery and no false notifications."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.db.base import Base
from app.db.models import (
    ConditionDelivery,
    ConditionSubscription,
    ConditionSubscriptionState,
    ConditionTransition,
    User,
)
from app.workers.condition_delivery_worker import (
    claim_action_delivery,
    deliver_one_action,
    finalize_action_delivery,
)

NOW = datetime(2026, 10, 24, 13, 0, tzinfo=UTC)


async def _database(*, current_state="action_required", provider="github_review"):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        user = User(telegram_user_id=921, chat_id=922, timezone="Europe/Moscow")
        session.add(user)
        await session.flush()
        sub = ConditionSubscription(
            user_id=user.id,
            chat_id=user.chat_id,
            provider_type=provider,
            target="MikeMoore1337/hermes#559",
            target_hash="1" * 64,
            config_json="{}",
            message_template="Требуется проверка PR",
            poll_interval_seconds=300,
            trigger_on_initial=True,
            state=ConditionSubscriptionState.ACTIVE.value,
            next_poll_at_utc=NOW,
            last_state=current_state,
            transition_sequence=1,
        )
        session.add(sub)
        await session.flush()
        transition = ConditionTransition(
            subscription_id=sub.id,
            sequence=1,
            previous_state="inactive",
            current_state="action_required",
            observed_at_utc=NOW,
        )
        session.add(transition)
        await session.flush()
        session.add(
            ConditionDelivery(
                subscription_id=sub.id,
                transition_id=transition.id,
                transition_sequence=1,
                chat_id=user.chat_id,
                previous_state="inactive",
                current_state="action_required",
                message_text="Требуется проверка PR",
                state="pending",
                attempt_count=0,
            )
        )
    return engine, factory


class _FakeBot:
    def __init__(self, fail_once=False):
        self.messages = []
        self.fail_once = fail_once

    async def send_message(self, *, chat_id, text):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("temporary failure")
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_owner_action_delivered_once_and_repeat_poll_has_no_duplicate():
    engine, factory = await _database()
    try:
        bot = _FakeBot()
        assert await deliver_one_action(bot, session_factory=factory, now=NOW)
        assert not await deliver_one_action(bot, session_factory=factory, now=NOW)
        assert bot.messages == [(922, "Требуется проверка PR")]
        async with factory() as session:
            delivery = await session.scalar(select(ConditionDelivery))
            assert delivery.state == "sent"
            assert delivery.sent_at_utc is not None
            assert delivery.sent_at_utc.replace(tzinfo=UTC) == NOW
            assert delivery.attempt_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_review_suppresses_queued_alert():
    engine, factory = await _database(current_state="inactive")
    try:
        bot = _FakeBot()
        assert not await deliver_one_action(bot, session_factory=factory, now=NOW)
        assert bot.messages == []
        async with factory() as session:
            delivery = await session.scalar(select(ConditionDelivery))
            assert delivery.state == "failed"
            assert delivery.last_error_code == "inactive_suppressed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_transient_send_retry_is_bounded_and_recoverable():
    engine, factory = await _database()
    try:
        bot = _FakeBot(fail_once=True)
        assert await deliver_one_action(bot, session_factory=factory, now=NOW)
        assert bot.messages == []
        assert not await deliver_one_action(bot, session_factory=factory, now=NOW)
        assert await deliver_one_action(
            bot, session_factory=factory, now=NOW + timedelta(seconds=31)
        )
        assert bot.messages == [(922, "Требуется проверка PR")]
        async with factory() as session:
            delivery = await session.scalar(select(ConditionDelivery))
            assert delivery.state == "sent"
            assert delivery.attempt_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_processing_lease_recovery_and_fencing():
    engine, factory = await _database()
    try:
        first = await claim_action_delivery(session_factory=factory, now=NOW)
        assert first is not None
        assert (
            await claim_action_delivery(session_factory=factory, now=NOW + timedelta(seconds=60))
            is None
        )
        second = await claim_action_delivery(
            session_factory=factory, now=NOW + timedelta(seconds=91)
        )
        assert second is not None
        assert second.id == first.id
        assert second.attempt == first.attempt + 1
        assert not await finalize_action_delivery(
            first, success=True, session_factory=factory, now=NOW + timedelta(seconds=92)
        )
        assert await finalize_action_delivery(
            second, success=True, session_factory=factory, now=NOW + timedelta(seconds=92)
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_non_github_condition_outbox_is_never_sent_by_this_adapter():
    engine, factory = await _database(provider="http_json")
    try:
        bot = _FakeBot()
        assert not await deliver_one_action(bot, session_factory=factory, now=NOW)
        assert bot.messages == []
    finally:
        await engine.dispose()
