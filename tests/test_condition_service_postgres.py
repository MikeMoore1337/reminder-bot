import asyncio
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("BOT_TOKEN", "test-token")

from app.config import Settings
from app.db.models import (
    ConditionDelivery,
    ConditionSubscription,
    ConditionTransition,
    User,
)
from app.services.condition_provider import ConditionObservation, ConditionProviderRegistry
from app.services.condition_service import ConditionService

POSTGRES_URL = os.environ.get("REMINDER_BOT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set REMINDER_BOT_TEST_DATABASE_URL to run PostgreSQL integration tests",
)

NOW = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
TEST_TELEGRAM_USER_ID = 9_912_000_001


class _HealthyProvider:
    provider_type = "pg_fake"

    async def observe(self, target: str, *, config=None) -> ConditionObservation:
        await asyncio.sleep(0)
        return ConditionObservation(state="ready")


def _settings() -> Settings:
    return Settings(
        bot_token="test-token",
        database_url=POSTGRES_URL or "postgresql+asyncpg://unused",
        condition_poll_interval_seconds=30,
        condition_request_timeout_seconds=1,
        condition_lease_duration_seconds=5,
        condition_retry_base_seconds=10,
        condition_retry_max_seconds=60,
    )


async def _open_postgres():
    assert POSTGRES_URL is not None
    engine = create_async_engine(POSTGRES_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session, session.begin():
        await session.execute(delete(User).where(User.telegram_user_id == TEST_TELEGRAM_USER_ID))
        user = User(
            telegram_user_id=TEST_TELEGRAM_USER_ID,
            chat_id=9_912_000_002,
            timezone="Europe/Moscow",
        )
        session.add(user)
        await session.flush()
        user_id = user.id
    return engine, session_factory, user_id


async def _close_postgres(engine, session_factory, user_id: int) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            delete(ConditionSubscription).where(ConditionSubscription.user_id == user_id)
        )
        await session.execute(delete(User).where(User.id == user_id))
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_concurrent_claims_are_lease_exclusive_and_transition_idempotent() -> None:
    engine, session_factory, user_id = await _open_postgres()
    try:
        provider = _HealthyProvider()
        registry = ConditionProviderRegistry({"pg_fake": provider})
        settings = _settings()
        service_a = ConditionService(
            session_factory=session_factory,
            registry=registry,
            settings_obj=settings,
            now_fn=lambda: NOW,
        )
        service_b = ConditionService(
            session_factory=session_factory,
            registry=registry,
            settings_obj=settings,
            now_fn=lambda: NOW,
        )
        for index in range(2):
            await service_a.create_subscription(
                user_id=user_id,
                chat_id=9_912_000_002,
                provider_type="pg_fake",
                target=f"fake://postgres/{index}",
                trigger_on_initial=True,
                next_poll_at_utc=NOW,
            )

        claimed_a, claimed_b = await asyncio.gather(
            service_a.claim_due_subscriptions(limit=1, now_utc=NOW),
            service_b.claim_due_subscriptions(limit=1, now_utc=NOW),
        )
        claims = claimed_a + claimed_b
        assert len(claims) == 2
        assert len({claim.id for claim in claims}) == 2
        assert len({claim.lease_token for claim in claims}) == 2

        await asyncio.gather(*(service_a.process_claim(claim) for claim in claims))
        async with session_factory() as session:
            transitions = list((await session.scalars(select(ConditionTransition))).all())
            deliveries = list((await session.scalars(select(ConditionDelivery))).all())
        assert len(transitions) == 2
        assert len(deliveries) == 2
        assert {(item.subscription_id, item.transition_sequence) for item in deliveries} == {
            (item.subscription_id, item.sequence) for item in transitions
        }
    finally:
        await _close_postgres(engine, session_factory, user_id)
