"""Opt-in condition outbox delivery via the existing Telegram bot worker.

No second poller; claims are database-serialized, with recoverable processing
leases. Telegram has no idempotency key: ambiguous transport failures can
still lead to repeated remote messages; never promise exactly-once delivery.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import ConditionDelivery, ConditionDeliveryState, ConditionSubscription
from app.db.session import SessionLocal
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)
PROCESSING = "processing"
MAX_ATTEMPTS = 3
LEASE_SECONDS = 90
SEND_TIMEOUT_SECONDS = 20
POLL_SECONDS = 10


@dataclass(frozen=True)
class DeliveryClaim:
    id: int
    chat_id: int
    message: str
    attempt: int


async def claim_action_delivery(
    *, session_factory: Callable[[], AsyncSession] = SessionLocal,
    now: datetime | None = None,
) -> DeliveryClaim | None:
    clock = now or utc_now()
    async with session_factory() as session, session.begin():
        item = await session.scalar(
            select(ConditionDelivery)
            .join(ConditionSubscription)
            .options(selectinload(ConditionDelivery.subscription))
            .where(
                ConditionSubscription.provider_type == "github_review",
                or_(
                    and_(
                        ConditionDelivery.state == ConditionDeliveryState.PENDING.value,
                        or_(
                            ConditionDelivery.next_attempt_at_utc.is_(None),
                            ConditionDelivery.next_attempt_at_utc <= clock,
                        ),
                    ),
                    and_(
                        ConditionDelivery.state == PROCESSING,
                        ConditionDelivery.next_attempt_at_utc <= clock,
                    ),
                ),
            )
            .order_by(ConditionDelivery.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if item is None:
            return None
        # Never send a queued review request that was since withdrawn/closed.
        if (
            item.subscription.state != "active"
            or item.subscription.last_state != "action_required"
            or item.current_state != "action_required"
        ):
            item.state = ConditionDeliveryState.FAILED.value
            item.last_error_code = "inactive_suppressed"
            item.next_attempt_at_utc = None
            return None
        if item.attempt_count >= MAX_ATTEMPTS:
            item.state = ConditionDeliveryState.FAILED.value
            item.last_error_code = "attempts_exhausted"
            item.next_attempt_at_utc = None
            return None
        item.attempt_count += 1
        item.state = PROCESSING
        item.next_attempt_at_utc = clock + timedelta(seconds=LEASE_SECONDS)
        return DeliveryClaim(
            id=item.id,
            chat_id=item.chat_id,
            message=item.message_text,
            attempt=item.attempt_count,
        )


async def finalize_action_delivery(
    claim: DeliveryClaim,
    *,
    success: bool,
    retryable: bool = False,
    retry_after: int | None = None,
    session_factory: Callable[[], AsyncSession] = SessionLocal,
    now: datetime | None = None,
) -> bool:
    clock = now or utc_now()
    async with session_factory() as session, session.begin():
        item = await session.scalar(
            select(ConditionDelivery)
            .where(
                ConditionDelivery.id == claim.id,
                ConditionDelivery.state == PROCESSING,
                ConditionDelivery.attempt_count == claim.attempt,
            )
            .with_for_update()
        )
        if item is None:
            return False
        if success:
            item.state = ConditionDeliveryState.SENT.value
            item.sent_at_utc = clock
            item.next_attempt_at_utc = None
            item.last_error_code = None
        elif retryable and claim.attempt < MAX_ATTEMPTS:
            item.state = ConditionDeliveryState.PENDING.value
            seconds = min(max(retry_after or 30 * (2 ** (claim.attempt - 1)), 1), 3600)
            item.next_attempt_at_utc = clock + timedelta(seconds=seconds)
            item.last_error_code = "telegram_transient"
        else:
            item.state = ConditionDeliveryState.FAILED.value
            item.last_error_code = "telegram_terminal_or_exhausted"
            item.next_attempt_at_utc = None
    return True


async def deliver_one_action(
    bot: Any,
    *,
    session_factory: Callable[[], AsyncSession] = SessionLocal,
    now: datetime | None = None,
) -> bool:
    claim = await claim_action_delivery(session_factory=session_factory, now=now)
    if claim is None:
        return False
    try:
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
            await bot.send_message(
                chat_id=claim.chat_id,
                text=claim.message,
            )
    except asyncio.CancelledError:
        raise  # the lease recovers this claim
    except (TelegramForbiddenError, TelegramBadRequest):
        await finalize_action_delivery(claim, success=False, session_factory=session_factory, now=now)
    except TelegramRetryAfter as exc:
        await finalize_action_delivery(
            claim, success=False, retryable=True,
            retry_after=min(max(int(exc.retry_after), 1), 3600),
            session_factory=session_factory, now=now,
        )
    except (TelegramNetworkError, TelegramServerError, TimeoutError):
        await finalize_action_delivery(
            claim, success=False, retryable=True, session_factory=session_factory, now=now
        )
    except Exception as exc:
        logger.error("Condition outbox send error", extra={
            "extra_data": "error_type=" + type(exc).__name__[:64],
        })
        await finalize_action_delivery(
            claim, success=False, retryable=True, session_factory=session_factory, now=now
        )
    else:
        await finalize_action_delivery(claim, success=True, session_factory=session_factory, now=now)
    return True


async def github_action_delivery_loop(bot: Any, stop_event: asyncio.Event) -> None:
    """Explicitly enabled only from the existing worker process."""
    while not stop_event.is_set():
        try:
            for _ in range(10):
                if not await deliver_one_action(bot):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Condition delivery loop failed", extra={
                "extra_data": "error_type=" + type(exc).__name__[:64],
            })
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            pass
