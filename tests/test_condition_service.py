import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.config import Settings
from app.db.base import Base
from app.db.models import (
    ConditionDelivery,
    ConditionObservation,
    ConditionSubscription,
    ConditionTransition,
    User,
)
from app.services.condition_provider import (
    ConditionObservation as ProviderObservation,
)
from app.services.condition_provider import (
    ConditionProviderError,
    ConditionProviderRegistry,
    validate_https_target,
)
from app.services.condition_service import (
    ConditionPollOutcome,
    ConditionService,
)

NOW = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)


class _SequenceProvider:
    provider_type = "fake"

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls = 0

    async def observe(self, target: str, *, config=None) -> ProviderObservation:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, ProviderObservation):
            return result
        return ProviderObservation(state=result)  # type: ignore[arg-type]


class _ValidatingSequenceProvider(_SequenceProvider):
    @staticmethod
    def validate_target(target: str):
        return validate_https_target(target)


class _SlowSequenceProvider(_SequenceProvider):
    async def observe(self, target: str, *, config=None) -> ProviderObservation:
        await asyncio.sleep(0.01)
        return await super().observe(target, config=config)


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "bot_token": "test-token",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "condition_poll_interval_seconds": 30,
        "condition_request_timeout_seconds": 1,
        "condition_lease_duration_seconds": 5,
        "condition_retry_base_seconds": 10,
        "condition_retry_max_seconds": 60,
    }
    values.update(overrides)
    return Settings(**values)


async def _open_sqlite():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    connection = await engine.connect()
    await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session, session.begin():
        session.add(User(telegram_user_id=1001, chat_id=2001, timezone="Europe/Moscow"))
    return engine, connection, session_factory


def _service(session_factory, provider, *, settings_obj: Settings | None = None, now=NOW):
    return ConditionService(
        session_factory=session_factory,
        registry=ConditionProviderRegistry({"fake": provider}),
        settings_obj=settings_obj or _settings(),
        now_fn=lambda: now,
    )


@pytest.mark.asyncio
async def test_https_query_is_rejected_before_subscription_persistence() -> None:
    engine, connection, session_factory = await _open_sqlite()
    try:
        provider = _ValidatingSequenceProvider("ready")
        service = ConditionService(
            session_factory=session_factory,
            registry=ConditionProviderRegistry({"http_json": provider}),
            settings_obj=_settings(),
            now_fn=lambda: NOW,
        )
        with pytest.raises(ConditionProviderError) as error:
            await service.create_subscription(
                user_id=1,
                chat_id=2001,
                provider_type="http_json",
                target="https://example.com/status?client_secret=hidden",
                next_poll_at_utc=NOW,
            )
        assert error.value.code == "query_not_allowed"
        async with session_factory() as session:
            assert await session.scalar(select(ConditionSubscription.id)) is None

        subscription = await service.create_subscription(
            user_id=1,
            chat_id=2001,
            provider_type="http_json",
            target="https://example.com/status",
            next_poll_at_utc=NOW,
        )
        assert subscription.target == "https://example.com/status"
    finally:
        await connection.close()
        await engine.dispose()


async def _subscription(session_factory, subscription_id: int) -> ConditionSubscription:
    async with session_factory() as session:
        return await session.scalar(
            select(ConditionSubscription).where(ConditionSubscription.id == subscription_id)
        )


@pytest.mark.asyncio
async def test_state_baseline_transition_and_same_state_are_idempotent() -> None:
    engine, connection, session_factory = await _open_sqlite()
    try:
        provider = _SequenceProvider("OPEN", "closed", "closed")
        service = _service(session_factory, provider)
        subscription = await service.create_subscription(
            user_id=1,
            chat_id=2001,
            provider_type="fake",
            target="fake://condition/1",
            next_poll_at_utc=NOW,
            message_template="state {previous_state} -> {current_state}",
        )

        first = await service.process_claim((await service.claim_due_subscriptions(now_utc=NOW))[0])
        assert first.outcome == ConditionPollOutcome.SUCCESS
        assert first.transition_created is False

        second_now = NOW + timedelta(seconds=31)
        second_claim = (await service.claim_due_subscriptions(now_utc=second_now))[0]
        second = await service.process_claim(second_claim)
        assert second.transition_created is True
        assert second.transition_sequence == 1

        third_now = second_now + timedelta(seconds=31)
        third_claim = (await service.claim_due_subscriptions(now_utc=third_now))[0]
        third = await service.process_claim(third_claim)
        assert third.deduplicated is True

        async with session_factory() as session:
            transitions = list((await session.scalars(select(ConditionTransition))).all())
            deliveries = list((await session.scalars(select(ConditionDelivery))).all())
            observations = list((await session.scalars(select(ConditionObservation))).all())
        assert [
            (item.previous_state, item.current_state, item.sequence) for item in transitions
        ] == [("open", "closed", 1)]
        assert len(deliveries) == 1
        assert deliveries[0].message_text == "state open -> closed"
        assert len(observations) == 3
        assert (await _subscription(session_factory, subscription.id)).last_state == "closed"
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_initial_transition_can_be_enabled_explicitly() -> None:
    engine, connection, session_factory = await _open_sqlite()
    try:
        service = _service(session_factory, _SequenceProvider("ready"))
        await service.create_subscription(
            user_id=1,
            chat_id=2001,
            provider_type="fake",
            target="fake://condition/initial",
            trigger_on_initial=True,
            next_poll_at_utc=NOW,
        )
        result = await service.process_claim(
            (await service.claim_due_subscriptions(now_utc=NOW))[0]
        )
        assert result.transition_created is True
        async with session_factory() as session:
            transition = await session.scalar(select(ConditionTransition))
            delivery = await session.scalar(select(ConditionDelivery))
        assert transition.previous_state is None
        assert transition.current_state == "ready"
        assert "unknown" in delivery.message_text
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_provider_failure_backoff_isolated_from_other_subscription() -> None:
    engine, connection, session_factory = await _open_sqlite()
    try:
        failing = _SequenceProvider(
            ConditionProviderError("rate_limited", retry_after_seconds=22),
        )
        healthy = _SequenceProvider("healthy")
        registry = ConditionProviderRegistry({"failing": failing, "healthy": healthy})
        service = ConditionService(
            session_factory=session_factory,
            registry=registry,
            settings_obj=_settings(),
            now_fn=lambda: NOW,
        )
        await service.create_subscription(
            user_id=1,
            chat_id=2001,
            provider_type="failing",
            target="fake://condition/failing",
            next_poll_at_utc=NOW,
        )
        await service.create_subscription(
            user_id=1,
            chat_id=2001,
            provider_type="healthy",
            target="fake://condition/healthy",
            next_poll_at_utc=NOW,
        )

        summary = await service.poll_due_conditions(now_utc=NOW)
        assert summary.claimed == 2
        assert summary.failed == 1
        assert summary.succeeded == 1

        async with session_factory() as session:
            subscriptions = list(
                (
                    await session.scalars(
                        select(ConditionSubscription).order_by(ConditionSubscription.provider_type)
                    )
                ).all()
            )
        assert subscriptions[0].failure_count == 1
        assert subscriptions[0].last_error_code == "rate_limited"
        assert subscriptions[0].next_retry_at_utc is not None
        assert subscriptions[1].last_state == "healthy"
        assert subscriptions[1].failure_count == 0
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_failure_backoff_recovers_and_clears_after_next_success() -> None:
    engine, connection, session_factory = await _open_sqlite()
    try:
        provider = _SequenceProvider(
            ConditionProviderError("temporary_failure"),
            "recovered",
        )
        service = _service(session_factory, provider)
        subscription = await service.create_subscription(
            user_id=1,
            chat_id=2001,
            provider_type="fake",
            target="fake://condition/recovery",
            next_poll_at_utc=NOW,
        )

        failed = await service.poll_due_conditions(now_utc=NOW)
        assert failed.failed == 1
        retry_now = NOW + timedelta(seconds=11)
        recovered = await service.poll_due_conditions(now_utc=retry_now)
        assert recovered.succeeded == 1

        row = await _subscription(session_factory, subscription.id)
        assert row.failure_count == 0
        assert row.last_error_code is None
        assert row.last_state == "recovered"
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_lease_and_restart_cannot_duplicate_transition() -> None:
    engine, connection, session_factory = await _open_sqlite()
    try:
        provider = _SequenceProvider("ready", "ready")
        service = _service(session_factory, provider)
        await service.create_subscription(
            user_id=1,
            chat_id=2001,
            provider_type="fake",
            target="fake://condition/restart",
            trigger_on_initial=True,
            next_poll_at_utc=NOW,
        )
        first_claim = (await service.claim_due_subscriptions(now_utc=NOW))[0]
        assert await service.claim_due_subscriptions(now_utc=NOW + timedelta(seconds=1)) == []
        recovered_claim = (
            await service.claim_due_subscriptions(now_utc=NOW + timedelta(seconds=6))
        )[0]

        stale = await service.process_claim(first_claim)
        assert stale.outcome == ConditionPollOutcome.STALE
        committed = await service.process_claim(recovered_claim)
        assert committed.transition_created is True

        async with session_factory() as session:
            assert len(list((await session.scalars(select(ConditionTransition))).all())) == 1
            assert len(list((await session.scalars(select(ConditionDelivery))).all())) == 1
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_batched_claims_renew_before_slow_provider_and_fence_expired_entry() -> None:
    engine, connection, session_factory = await _open_sqlite()
    try:
        provider = _SlowSequenceProvider("one", "two", "three")
        settings_obj = _settings(
            condition_poll_batch_size=3,
            condition_request_timeout_seconds=1,
            condition_lease_duration_seconds=3,
        )
        clock = _MutableClock(NOW)
        registry = ConditionProviderRegistry({"fake": provider})
        service_a = ConditionService(
            session_factory=session_factory,
            registry=registry,
            settings_obj=settings_obj,
            now_fn=clock,
        )
        service_b = ConditionService(
            session_factory=session_factory,
            registry=registry,
            settings_obj=settings_obj,
            now_fn=clock,
        )
        for index in range(3):
            await service_a.create_subscription(
                user_id=1,
                chat_id=2001,
                provider_type="fake",
                target=f"fake://condition/slow/{index}",
                trigger_on_initial=True,
                next_poll_at_utc=NOW,
            )

        claims = await service_a.claim_due_subscriptions(limit=3, now_utc=NOW)
        assert len(claims) == 3

        first = await service_a.process_claim(claims[0])
        assert first.outcome == ConditionPollOutcome.SUCCESS
        clock.value = NOW + timedelta(seconds=2)
        second = await service_a.process_claim(claims[1])
        assert second.outcome == ConditionPollOutcome.SUCCESS

        # The third entry's original lease expired while the first two slow
        # calls were in flight. A second worker can reclaim it once, while
        # the stale first worker must not observe or finalize it.
        clock.value = NOW + timedelta(seconds=4)
        reclaimed = await service_b.claim_due_subscriptions(limit=1, now_utc=clock.value)
        assert len(reclaimed) == 1
        assert reclaimed[0].id == claims[2].id
        stale = await service_a.process_claim(claims[2])
        assert stale.outcome == ConditionPollOutcome.STALE
        committed = await service_b.process_claim(reclaimed[0])
        assert committed.outcome == ConditionPollOutcome.SUCCESS
        assert provider.calls == 3

        async with session_factory() as session:
            transitions = list((await session.scalars(select(ConditionTransition))).all())
            deliveries = list((await session.scalars(select(ConditionDelivery))).all())
        assert len(transitions) == 3
        assert len(deliveries) == 3
    finally:
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_history_cleanup_removes_old_observations_but_keeps_pending_outbox() -> None:
    engine, connection, session_factory = await _open_sqlite()
    try:
        service = _service(session_factory, _SequenceProvider("ready"), now=NOW)
        subscription = await service.create_subscription(
            user_id=1,
            chat_id=2001,
            provider_type="fake",
            target="fake://condition/cleanup",
            trigger_on_initial=True,
            next_poll_at_utc=NOW,
        )
        await service.process_claim((await service.claim_due_subscriptions(now_utc=NOW))[0])
        old_time = NOW - timedelta(days=3)
        async with session_factory() as session, session.begin():
            await session.execute(
                update(ConditionObservation)
                .where(ConditionObservation.subscription_id == subscription.id)
                .values(observed_at_utc=old_time)
            )
        removed = await service.cleanup_history(now_utc=NOW, retention_days=1)
        assert removed == 1
        async with session_factory() as session:
            assert await session.scalar(select(ConditionObservation.id)) is None
            assert await session.scalar(select(ConditionDelivery.id)) is not None
    finally:
        await connection.close()
        await engine.dispose()
