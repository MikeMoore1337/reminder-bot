import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.services.condition_service import ConditionCycleSummary
from app.workers import condition_worker


@pytest.mark.asyncio
async def test_condition_worker_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        condition_worker,
        "settings",
        SimpleNamespace(
            condition_worker_enabled=False,
            condition_poll_interval_seconds=30,
            condition_cleanup_interval_seconds=3600,
        ),
    )

    class _UnexpectedService:
        async def poll_due_conditions(self, **kwargs):
            raise AssertionError("disabled condition worker must not poll")

    summary = await condition_worker.process_due_conditions(service=_UnexpectedService())
    assert summary == ConditionCycleSummary(disabled=True)


@pytest.mark.asyncio
async def test_condition_worker_uses_separate_service_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        condition_worker,
        "settings",
        SimpleNamespace(
            condition_worker_enabled=True,
            condition_poll_interval_seconds=30,
            condition_cleanup_interval_seconds=3600,
        ),
    )
    expected = ConditionCycleSummary(claimed=1, succeeded=1)

    class _Service:
        async def poll_due_conditions(self, **kwargs):
            return expected

    assert await condition_worker.process_due_conditions(service=_Service()) == expected


@pytest.mark.asyncio
async def test_condition_history_cleanup_is_cadenced_and_failure_isolated(monkeypatch) -> None:
    monkeypatch.setattr(
        condition_worker,
        "settings",
        SimpleNamespace(
            condition_worker_enabled=True,
            condition_poll_interval_seconds=30,
            condition_cleanup_interval_seconds=3600,
        ),
    )

    class _CleanupService:
        def __init__(self) -> None:
            self.calls = 0

        async def cleanup_history(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("cleanup failure")
            return 1

    service = _CleanupService()
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    last_cleanup = await condition_worker.cleanup_conditions_if_due(
        service=service,
        last_cleanup_at_utc=None,
        now_utc=now,
    )
    assert last_cleanup == now
    assert service.calls == 1

    unchanged = await condition_worker.cleanup_conditions_if_due(
        service=service,
        last_cleanup_at_utc=last_cleanup,
        now_utc=now + timedelta(minutes=30),
    )
    assert unchanged == last_cleanup
    assert service.calls == 1

    retried = await condition_worker.cleanup_conditions_if_due(
        service=service,
        last_cleanup_at_utc=last_cleanup,
        now_utc=now + timedelta(hours=1),
    )
    assert retried == now + timedelta(hours=1)
    assert service.calls == 2
