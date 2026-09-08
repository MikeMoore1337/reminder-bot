import os
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
        SimpleNamespace(condition_worker_enabled=False, condition_poll_interval_seconds=30),
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
        SimpleNamespace(condition_worker_enabled=True, condition_poll_interval_seconds=30),
    )
    expected = ConditionCycleSummary(claimed=1, succeeded=1)

    class _Service:
        async def poll_due_conditions(self, **kwargs):
            return expected

    assert await condition_worker.process_due_conditions(service=_Service()) == expected
