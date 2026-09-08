from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.config import get_settings
from app.services.condition_service import ConditionCycleSummary, ConditionService

logger = logging.getLogger(__name__)
settings = get_settings()


async def _wait_for_stop(stop_event: asyncio.Event | None, timeout_seconds: float) -> bool:
    if stop_event is None:
        await asyncio.sleep(timeout_seconds)
        return False
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return False
    return True


async def process_due_conditions(
    *,
    service: ConditionService | None = None,
    now_utc: datetime | None = None,
) -> ConditionCycleSummary:
    if not settings.condition_worker_enabled:
        return ConditionCycleSummary(disabled=True)
    condition_service = service or ConditionService()
    return await condition_service.poll_due_conditions(now_utc=now_utc)


async def condition_loop(
    stop_event: asyncio.Event | None = None,
    *,
    service: ConditionService | None = None,
) -> None:
    """Run the optional condition poller without entering the time worker."""

    logger.info("Condition worker started")
    while stop_event is None or not stop_event.is_set():
        try:
            summary = await process_due_conditions(service=service)
            if summary.claimed:
                logger.info(
                    "Processed condition subscriptions",
                    extra={"extra_data": str(summary.snapshot())},
                )
        except asyncio.CancelledError:
            logger.info("Condition worker task cancelled; active claims remain lease-recoverable")
            raise
        except Exception as exc:
            logger.error(
                "Unexpected error in condition worker loop",
                extra={"extra_data": f"error_type={type(exc).__name__[:80]}"},
            )
        if await _wait_for_stop(stop_event, settings.condition_poll_interval_seconds):
            break
    logger.info("Condition worker stopped")
