from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.services.condition_service import ConditionCycleSummary, ConditionService
from app.utils.datetime_utils import utc_now

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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def cleanup_conditions_if_due(
    *,
    service: ConditionService,
    last_cleanup_at_utc: datetime | None,
    now_utc: datetime | None = None,
) -> datetime | None:
    """Run history cleanup on its own cadence and isolate cleanup failures."""

    if not settings.condition_worker_enabled:
        return last_cleanup_at_utc
    current = _as_utc(now_utc or utc_now())
    if last_cleanup_at_utc is not None and current < _as_utc(last_cleanup_at_utc) + timedelta(
        seconds=getattr(settings, "condition_cleanup_interval_seconds", 3600)
    ):
        return last_cleanup_at_utc
    try:
        await service.cleanup_history(now_utc=current)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "Condition history cleanup failed; polling will continue",
            extra={"extra_data": f"error_type={type(exc).__name__[:80]}"},
        )
    return current


async def condition_loop(
    stop_event: asyncio.Event | None = None,
    *,
    service: ConditionService | None = None,
) -> None:
    """Run the optional condition poller without entering the time worker."""

    logger.info("Condition worker started")
    condition_service = service or ConditionService()
    last_cleanup_at_utc: datetime | None = None
    while stop_event is None or not stop_event.is_set():
        try:
            summary = await process_due_conditions(service=condition_service)
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
            summary = ConditionCycleSummary()
        if not summary.disabled:
            last_cleanup_at_utc = await cleanup_conditions_if_due(
                service=condition_service,
                last_cleanup_at_utc=last_cleanup_at_utc,
            )
        if await _wait_for_stop(stop_event, settings.condition_poll_interval_seconds):
            break
    logger.info("Condition worker stopped")
