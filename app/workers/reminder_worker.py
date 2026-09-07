from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from html import escape
from typing import Any, cast
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult

from app.callbacks import CallbackAction, CallbackOrigin, CallbackTarget, encode_callback
from app.config import get_settings
from app.db.models import (
    OccurrenceState,
    RecurrenceType,
    Reminder,
    ReminderOccurrence,
    ReminderState,
)
from app.db.session import SessionLocal
from app.services.reminder_service import (
    advance_occurrence_until_future,
    delivery_at_utc,
    prepare_delivery_occurrence,
    set_last_message_id,
)
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)
settings = get_settings()


class DeliveryErrorKind(StrEnum):
    TRANSIENT = "transient"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class DeliveryFailure:
    kind: DeliveryErrorKind
    error_type: str
    retry_after_seconds: int | None = None


@dataclass
class WorkerMetrics:
    claimed: int = 0
    recovered: int = 0
    retried: int = 0
    delivered: int = 0
    failed: int = 0
    expired_leases: int = 0
    processing_age_seconds: float = 0.0

    def snapshot(self) -> dict[str, int | float]:
        return {
            "claimed": self.claimed,
            "recovered": self.recovered,
            "retried": self.retried,
            "delivered": self.delivered,
            "failed": self.failed,
            "expired_leases": self.expired_leases,
            "processing_age_seconds": self.processing_age_seconds,
        }


worker_metrics = WorkerMetrics()


def reminder_actions_kb(
    reminder_id: int,
    *,
    occurrence_id: int | None = None,
    revision: int = 0,
    state: str = ReminderState.DELIVERED.value,
    recurrence_type: str = RecurrenceType.NONE.value,
    include_snooze: bool = True,
    origin: CallbackOrigin | str = CallbackOrigin.DELIVERY,
) -> InlineKeyboardMarkup:
    # Keep the old helper call useful for callers that only want a compact
    # cancellation button. Real delivery cards always pass an occurrence id.
    if occurrence_id is None and not include_snooze and state == ReminderState.DELIVERED.value:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Удалить",
                        callback_data=encode_callback(
                            CallbackAction.DELETE,
                            CallbackTarget.REMINDER,
                            reminder_id,
                            revision,
                            origin=origin,
                        ),
                    )
                ]
            ]
        )

    target = CallbackTarget.OCCURRENCE if occurrence_id is not None else CallbackTarget.REMINDER
    target_id = occurrence_id or reminder_id

    def button(text: str, action: CallbackAction) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text,
            callback_data=encode_callback(
                action,
                target,
                target_id,
                revision,
                origin=origin,
            ),
        )

    rows: list[list[InlineKeyboardButton]] = []
    if state == ReminderState.DELIVERED.value:
        rows.append([button("✅ Готово", CallbackAction.DONE)])
        if include_snooze:
            rows.append([button("⏰ Отложить", CallbackAction.SNOOZE)])
        rows.append([button("✏️ Изменить", CallbackAction.EDIT)])
        if recurrence_type != RecurrenceType.NONE.value:
            rows.append([button("⏸ Пауза", CallbackAction.PAUSE)])
        rows.append([button("🗑 Удалить", CallbackAction.DELETE)])
    elif state == ReminderState.PAUSED.value:
        rows.append([button("▶️ Продолжить", CallbackAction.RESUME)])
        rows.append([button("✏️ Изменить", CallbackAction.EDIT)])
        rows.append([button("🗑 Удалить", CallbackAction.DELETE)])
    elif state in {ReminderState.SCHEDULED.value, ReminderState.SNOOZED.value}:
        if include_snooze:
            rows.append([button("⏰ Отложить", CallbackAction.SNOOZE)])
        rows.append([button("✏️ Изменить", CallbackAction.EDIT)])
        if recurrence_type != RecurrenceType.NONE.value:
            rows.append([button("⏸ Пауза", CallbackAction.PAUSE)])
        rows.append([button("🗑 Удалить", CallbackAction.DELETE)])
    else:
        rows.append([button("🗑 Удалить", CallbackAction.DELETE)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def snooze_presets_kb(
    reminder_id: int,
    *,
    occurrence_id: int | None,
    revision: int,
    origin: CallbackOrigin | str = CallbackOrigin.DELIVERY,
) -> InlineKeyboardMarkup:
    target = CallbackTarget.OCCURRENCE if occurrence_id is not None else CallbackTarget.REMINDER
    target_id = occurrence_id or reminder_id
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="10 минут",
                    callback_data=encode_callback(
                        CallbackAction.SNOOZE_10,
                        target,
                        target_id,
                        revision,
                        origin=origin,
                    ),
                ),
                InlineKeyboardButton(
                    text="1 час",
                    callback_data=encode_callback(
                        CallbackAction.SNOOZE_1H,
                        target,
                        target_id,
                        revision,
                        origin=origin,
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Вечером",
                    callback_data=encode_callback(
                        CallbackAction.SNOOZE_EVENING,
                        target,
                        target_id,
                        revision,
                        origin=origin,
                    ),
                ),
                InlineKeyboardButton(
                    text="Завтра",
                    callback_data=encode_callback(
                        CallbackAction.SNOOZE_TOMORROW,
                        target,
                        target_id,
                        revision,
                        origin=origin,
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Своё время",
                    callback_data=encode_callback(
                        CallbackAction.SNOOZE_CUSTOM,
                        target,
                        target_id,
                        revision,
                        origin=origin,
                    ),
                )
            ],
        ]
    )


def classify_delivery_error(exc: BaseException) -> DeliveryFailure:
    error_type = type(exc).__name__[:80]

    if isinstance(exc, TelegramRetryAfter) or error_type == "TelegramRetryAfter":
        retry_after = getattr(exc, "retry_after", None)
        retry_after_seconds: int | None = None
        if isinstance(retry_after, (int, float, str)):
            try:
                retry_after_seconds = max(0, math.ceil(float(retry_after)))
            except (TypeError, ValueError):
                retry_after_seconds = None
        return DeliveryFailure(
            kind=DeliveryErrorKind.TRANSIENT,
            error_type=error_type,
            retry_after_seconds=retry_after_seconds,
        )

    terminal_types = {
        "TelegramBadRequest",
        "TelegramForbiddenError",
        "TelegramNotFound",
        "TelegramUnauthorizedError",
    }
    if (
        isinstance(exc, (TelegramBadRequest, TelegramForbiddenError))
        or error_type in terminal_types
    ):
        return DeliveryFailure(kind=DeliveryErrorKind.TERMINAL, error_type=error_type)

    transient_types = {
        "TelegramNetworkError",
        "TelegramServerError",
        "TimeoutError",
        "ConnectionError",
    }
    if isinstance(exc, (TelegramNetworkError, TelegramServerError, TimeoutError, OSError)) or (
        error_type in transient_types
    ):
        return DeliveryFailure(kind=DeliveryErrorKind.TRANSIENT, error_type=error_type)

    # Unknown provider failures are bounded transient failures. This keeps a
    # temporary SDK/runtime error recoverable without retrying forever.
    return DeliveryFailure(kind=DeliveryErrorKind.TRANSIENT, error_type=error_type)


def retry_delay_seconds(
    failed_attempt: int,
    *,
    base_seconds: int,
    max_seconds: int,
    retry_after_seconds: int | None = None,
) -> int:
    exponent = min(max(failed_attempt - 1, 0), 30)
    exponential_delay = base_seconds * (2**exponent)
    requested_delay: int = max(exponential_delay, retry_after_seconds or 0)
    bounded_delay: int = min(max_seconds, requested_delay)
    return max(1, bounded_delay)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _lease_age_seconds(now_utc: datetime, started_at: datetime | None) -> float:
    if started_at is None:
        return 0.0
    return max(0.0, (_as_utc(now_utc) - _as_utc(started_at)).total_seconds())


def _clear_processing_state(reminder: Reminder) -> None:
    reminder.processing_started_at = None
    reminder.lease_until = None
    reminder.lease_token = None
    reminder.next_retry_at = None


def _safe_failure_text(failure: DeliveryFailure) -> str:
    text = f"{failure.error_type} ({failure.kind.value})"
    if failure.retry_after_seconds is not None:
        text += f" retry_after={failure.retry_after_seconds}s"
    return text[:2000]


async def claim_due_reminders(
    limit: int,
    *,
    now_utc: datetime | None = None,
) -> list[Reminder]:
    if limit < 1:
        return []

    current_time = now_utc or utc_now()
    delivery_at = func.coalesce(Reminder.delivery_at_utc, Reminder.remind_at_utc)
    pending_due = and_(
        Reminder.status == "pending",
        Reminder.state.in_((ReminderState.SCHEDULED.value, ReminderState.SNOOZED.value)),
        delivery_at <= current_time,
        or_(Reminder.next_retry_at.is_(None), Reminder.next_retry_at <= current_time),
    )
    recoverable_processing = and_(
        Reminder.status == "processing",
        Reminder.state.in_((ReminderState.SCHEDULED.value, ReminderState.SNOOZED.value)),
        or_(Reminder.lease_until.is_(None), Reminder.lease_until <= current_time),
    )

    claimed: list[Reminder] = []
    recovered_count = 0
    expired_count = 0
    exhausted_count = 0
    max_recovered_age = 0.0
    current_processing_age = 0.0

    async with SessionLocal() as session, session.begin():
        result = await session.execute(
            select(Reminder)
            .where(or_(pending_due, recoverable_processing))
            .order_by(delivery_at.asc(), Reminder.id.asc())
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        candidates = list(result.scalars().all())

        for reminder in candidates:
            if (
                reminder.parent_reminder_id is not None
                and reminder.source_occurrence_at_utc is not None
            ):
                source = await session.scalar(
                    select(ReminderOccurrence)
                    .where(
                        ReminderOccurrence.reminder_id == reminder.parent_reminder_id,
                        ReminderOccurrence.occurrence_at_utc == reminder.source_occurrence_at_utc,
                    )
                    .with_for_update()
                )
                if source is not None and source.status in {
                    OccurrenceState.COMPLETED.value,
                    OccurrenceState.CANCELLED.value,
                }:
                    reminder.status = "sent"
                    reminder.state = ReminderState.CANCELLED.value
                    reminder.cancelled_at = current_time
                    reminder.action_revision += 1
                    reminder.delivery_at_utc = None
                    reminder.snoozed_until_utc = None
                    continue

            was_recovery = reminder.status == "processing"
            if reminder.attempt_count >= settings.worker_max_attempts:
                reminder.status = "failed"
                reminder.state = ReminderState.FAILED.value
                reminder.retry_count = max(reminder.retry_count, reminder.attempt_count)
                reminder.error_text = "delivery attempt limit exhausted"
                reminder.last_message_id = None
                reminder.last_delivery_occurrence_utc = None
                _clear_processing_state(reminder)
                exhausted_count += 1
                continue

            if was_recovery:
                recovered_count += 1
                expired_count += 1
                max_recovered_age = max(
                    max_recovered_age,
                    _lease_age_seconds(current_time, reminder.processing_started_at),
                )

            reminder.status = "processing"
            reminder.processing_started_at = current_time
            reminder.lease_until = current_time + timedelta(
                seconds=settings.worker_lease_duration_seconds
            )
            reminder.lease_token = uuid4().hex
            reminder.next_retry_at = None
            reminder.last_message_id = None
            reminder.last_delivery_occurrence_utc = None
            reminder.action_revision += 1
            reminder.attempt_count += 1
            claimed.append(reminder)

            processing_started_values = await session.scalars(
                select(Reminder.processing_started_at).where(
                    Reminder.status == "processing",
                    Reminder.processing_started_at.is_not(None),
                )
            )
            current_processing_age = max(
                (
                    _lease_age_seconds(current_time, started_at)
                    for started_at in processing_started_values
                ),
                default=0.0,
            )

    worker_metrics.claimed += len(claimed)
    worker_metrics.recovered += recovered_count
    worker_metrics.expired_leases += expired_count
    worker_metrics.failed += exhausted_count
    worker_metrics.processing_age_seconds = current_processing_age

    if claimed or recovered_count or exhausted_count:
        logger.info(
            "Reminder claim batch",
            extra={
                "extra_data": (
                    f"claimed={len(claimed)} recovered={recovered_count} "
                    f"exhausted={exhausted_count} "
                    f"processing_age_seconds={current_processing_age:.3f}"
                )
            },
        )
    return claimed


async def fetch_due_reminders(limit: int) -> list[Reminder]:
    """Compatibility wrapper for callers that used the old claim function name."""

    return await claim_due_reminders(limit)


async def renew_claim_before_send(
    reminder_id: int,
    lease_token: str,
    *,
    now_utc: datetime | None = None,
) -> bool:
    """Renew a still-owned lease immediately before an external send.

    The transaction commits before the caller invokes Telegram. The predicate
    rejects cancelled, reclaimed, and already-expired ownership generations.
    """

    if not lease_token:
        return False

    current_time = now_utc or utc_now()
    renewed_until = current_time + timedelta(seconds=settings.worker_lease_duration_seconds)
    async with SessionLocal() as session, session.begin():
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder_id,
                    Reminder.status == "processing",
                    Reminder.state.in_(
                        (ReminderState.SCHEDULED.value, ReminderState.SNOOZED.value)
                    ),
                    Reminder.lease_token == lease_token,
                    Reminder.lease_until > current_time,
                )
                .values(lease_until=renewed_until)
            ),
        )
        return bool(result.rowcount)


async def finalize_delivery_success(
    reminder_id: int,
    lease_token: str,
    *,
    now_utc: datetime | None = None,
) -> bool:
    if not lease_token:
        return False

    current_time = now_utc or utc_now()
    async with SessionLocal() as session, session.begin():
        result = await session.execute(
            select(Reminder)
            .where(
                Reminder.id == reminder_id,
                Reminder.status == "processing",
                Reminder.state.in_((ReminderState.SCHEDULED.value, ReminderState.SNOOZED.value)),
                Reminder.lease_token == lease_token,
                Reminder.lease_until > current_time,
            )
            .with_for_update()
        )
        reminder = result.scalar_one_or_none()
        if reminder is None:
            return False

        current_occurrence = reminder.remind_at_utc
        occurrence = await session.scalar(
            select(ReminderOccurrence)
            .where(
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.occurrence_at_utc == current_occurrence,
            )
            .with_for_update()
        )
        if occurrence is None:
            occurrence = ReminderOccurrence(
                reminder_id=reminder.id,
                occurrence_at_utc=current_occurrence,
                delivery_at_utc=delivery_at_utc(reminder),
                status=OccurrenceState.DELIVERED.value,
                action_revision=reminder.action_revision,
                message_id=reminder.last_message_id,
                delivered_at=current_time,
            )
            session.add(occurrence)
        else:
            occurrence.status = OccurrenceState.DELIVERED.value
            occurrence.action_revision = reminder.action_revision
            occurrence.delivered_at = current_time
            occurrence.snoozed_until_utc = None
        reminder.sent_at = current_time
        reminder.error_text = None
        reminder.retry_count = 0
        reminder.attempt_count = 0
        if reminder.last_delivery_occurrence_utc is None:
            reminder.last_delivery_occurrence_utc = current_occurrence

        if reminder.recurrence_type == RecurrenceType.NONE.value:
            reminder.status = "sent"
            reminder.state = ReminderState.DELIVERED.value
            reminder.delivery_at_utc = None
            reminder.snoozed_until_utc = None
            _clear_processing_state(reminder)
            return True

        next_occurrence = advance_occurrence_until_future(
            current_occurrence,
            reminder.recurrence_type,
            reminder.recurrence_interval,
            timezone_name=reminder.schedule_timezone,
            recurrence_day_of_month=reminder.recurrence_day_of_month,
            now_utc=current_time,
        )
        if next_occurrence is None:
            reminder.status = "sent"
            reminder.state = ReminderState.DELIVERED.value
            reminder.delivery_at_utc = None
            reminder.snoozed_until_utc = None
            _clear_processing_state(reminder)
            return True

        reminder.remind_at_utc = next_occurrence
        reminder.delivery_at_utc = next_occurrence
        reminder.snoozed_until_utc = None
        reminder.status = "pending"
        reminder.state = ReminderState.SCHEDULED.value
        _clear_processing_state(reminder)
        return True


async def finalize_delivery_failure(
    reminder_id: int,
    lease_token: str,
    failure: DeliveryFailure,
    *,
    now_utc: datetime | None = None,
) -> bool:
    if not lease_token:
        return False

    current_time = now_utc or utc_now()
    terminal = False
    retried = False
    async with SessionLocal() as session, session.begin():
        result = await session.execute(
            select(Reminder)
            .where(
                Reminder.id == reminder_id,
                Reminder.status == "processing",
                Reminder.state.in_((ReminderState.SCHEDULED.value, ReminderState.SNOOZED.value)),
                Reminder.lease_token == lease_token,
                Reminder.lease_until > current_time,
            )
            .with_for_update()
        )
        reminder = result.scalar_one_or_none()
        if reminder is None:
            return False

        reminder.retry_count += 1
        reminder.error_text = _safe_failure_text(failure)
        reminder.last_message_id = None
        reminder.last_delivery_occurrence_utc = None
        _clear_processing_state(reminder)
        terminal = (
            failure.kind == DeliveryErrorKind.TERMINAL
            or reminder.attempt_count >= settings.worker_max_attempts
        )
        occurrence = await session.scalar(
            select(ReminderOccurrence)
            .where(
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.occurrence_at_utc == reminder.remind_at_utc,
            )
            .with_for_update()
        )
        if occurrence is None:
            occurrence = ReminderOccurrence(
                reminder_id=reminder.id,
                occurrence_at_utc=reminder.remind_at_utc,
                delivery_at_utc=delivery_at_utc(reminder),
                status=OccurrenceState.FAILED.value,
                action_revision=reminder.action_revision,
            )
            session.add(occurrence)
        else:
            occurrence.status = OccurrenceState.FAILED.value
            occurrence.message_id = None
        if terminal:
            reminder.status = "failed"
            reminder.state = ReminderState.FAILED.value
        else:
            delay = retry_delay_seconds(
                reminder.retry_count,
                base_seconds=settings.worker_retry_base_seconds,
                max_seconds=settings.worker_retry_max_seconds,
                retry_after_seconds=failure.retry_after_seconds,
            )
            reminder.status = "pending"
            reminder.state = ReminderState.SCHEDULED.value
            reminder.next_retry_at = current_time + timedelta(seconds=delay)
            retried = True

    if terminal:
        worker_metrics.failed += 1
    if retried:
        worker_metrics.retried += 1
    return True


async def mark_after_send(reminder_id: int, lease_token: str | None = None) -> bool:
    """Compatibility wrapper with the ownership token required by the new contract."""

    if lease_token is None:
        return False
    return await finalize_delivery_success(reminder_id, lease_token)


async def mark_failed(
    reminder_id: int,
    error_text: str,
    lease_token: str | None = None,
) -> bool:
    """Compatibility wrapper that stores only a safe failure classification."""

    del error_text
    if lease_token is None:
        return False
    return await finalize_delivery_failure(
        reminder_id,
        lease_token,
        DeliveryFailure(kind=DeliveryErrorKind.TRANSIENT, error_type="WorkerError"),
    )


async def _finalize_send_failure(
    reminder: Reminder,
    failure: DeliveryFailure,
) -> bool:
    lease_token = reminder.lease_token
    if lease_token is None:
        return False
    try:
        finalized = await finalize_delivery_failure(reminder.id, lease_token, failure)
    except Exception as exc:
        logger.error(
            "Unable to finalize delivery failure; lease remains recoverable",
            extra={
                "extra_data": (
                    f"reminder_id={reminder.id} finalization_error_type={type(exc).__name__[:80]}"
                )
            },
        )
        return False
    if not finalized:
        logger.info(
            "Ignored stale delivery failure finalization",
            extra={"extra_data": f"reminder_id={reminder.id} error_kind={failure.kind.value}"},
        )
    return finalized


async def process_claimed_reminder(
    bot: Bot,
    reminder: Reminder,
    *,
    stop_event: asyncio.Event | None = None,
) -> bool:
    if stop_event is not None and stop_event.is_set():
        return False

    lease_token = reminder.lease_token
    if lease_token is None:
        return False

    if not await renew_claim_before_send(reminder.id, lease_token):
        logger.info(
            "Skipped delivery for stale claim before external send",
            extra={"extra_data": f"reminder_id={reminder.id}"},
        )
        return False
    occurrence_id = await prepare_delivery_occurrence(
        reminder.id,
        lease_token,
        now_utc=utc_now(),
    )
    if occurrence_id is None:
        logger.info(
            "Skipped delivery because occurrence ownership is stale",
            extra={"extra_data": f"reminder_id={reminder.id}"},
        )
        return False
    if stop_event is not None and stop_event.is_set():
        return False

    attempt_number = reminder.attempt_count
    try:
        sent = await asyncio.wait_for(
            bot.send_message(
                chat_id=reminder.chat_id,
                text=f"⏰ Напоминание\n\n{escape(reminder.text)}",
                reply_markup=reminder_actions_kb(
                    reminder.id,
                    occurrence_id=occurrence_id,
                    revision=reminder.action_revision,
                    state=ReminderState.DELIVERED.value,
                    recurrence_type=reminder.recurrence_type,
                    include_snooze=True,
                ),
            ),
            timeout=settings.worker_send_timeout_seconds,
        )
    except Exception as exc:
        failure = classify_delivery_error(exc)
        finalized = await _finalize_send_failure(reminder, failure)
        if finalized:
            logger.warning(
                "Reminder delivery failed",
                extra={
                    "extra_data": (
                        f"reminder_id={reminder.id} attempt={attempt_number} "
                        f"error_type={failure.error_type} error_kind={failure.kind.value}"
                    )
                },
            )
        return False

    try:
        recorded = await set_last_message_id(
            reminder.id,
            sent.message_id,
            lease_token=lease_token,
            occurrence_at_utc=reminder.remind_at_utc,
            now_utc=utc_now(),
        )
        if not recorded:
            logger.info(
                "Ignored stale delivery message metadata",
                extra={"extra_data": f"reminder_id={reminder.id}"},
            )
            return False

        finalized = await finalize_delivery_success(reminder.id, lease_token)
        if not finalized:
            logger.info(
                "Ignored stale delivery success finalization",
                extra={"extra_data": f"reminder_id={reminder.id}"},
            )
            return False
    except Exception as exc:
        # A successful external send followed by a DB failure intentionally
        # leaves the lease for expiry/recovery. This is the documented
        # at-least-once boundary, not an unsafe token-less finalization.
        logger.error(
            "Delivery sent but finalization was not completed; lease remains recoverable",
            extra={
                "extra_data": (
                    f"reminder_id={reminder.id} finalization_error_type={type(exc).__name__[:80]}"
                )
            },
        )
        return False

    worker_metrics.delivered += 1
    logger.info(
        "Reminder delivered",
        extra={"extra_data": f"reminder_id={reminder.id} attempt={attempt_number}"},
    )
    return True


async def process_due_reminders(bot: Bot, *, stop_event: asyncio.Event | None = None) -> int:
    if stop_event is not None and stop_event.is_set():
        return 0

    reminders = await claim_due_reminders(limit=settings.worker_batch_size)
    if not reminders:
        return 0

    processed_count = 0
    for index, reminder in enumerate(reminders):
        if stop_event is not None and stop_event.is_set():
            logger.info(
                "Worker stop requested; leaving remaining claims for lease recovery",
                extra={"extra_data": f"remaining_claims={len(reminders) - index}"},
            )
            break
        if await process_claimed_reminder(bot, reminder, stop_event=stop_event):
            processed_count += 1

    return processed_count


async def _wait_for_stop(
    stop_event: asyncio.Event | None,
    timeout_seconds: float,
) -> bool:
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


async def reminder_loop(bot: Bot, stop_event: asyncio.Event | None = None) -> None:
    logger.info("Reminder worker started")
    while stop_event is None or not stop_event.is_set():
        try:
            processed_count = await process_due_reminders(bot, stop_event=stop_event)
            if processed_count:
                logger.info(
                    "Processed reminders",
                    extra={"extra_data": f"count={processed_count}"},
                )
        except asyncio.CancelledError:
            logger.info("Reminder worker task cancelled; active claims remain lease-recoverable")
            raise
        except Exception as exc:
            logger.error(
                "Unexpected error in reminder worker loop",
                extra={"extra_data": f"error_type={type(exc).__name__[:80]}"},
            )
        if await _wait_for_stop(stop_event, settings.worker_poll_interval_seconds):
            break
    logger.info("Reminder worker stopped")
