from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Any, cast
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    DigestDeliveryState,
    DigestPeriod,
    RecurrenceType,
    Reminder,
    ReminderDigestDelivery,
    ReminderKind,
    ReminderSnoozeEvent,
    ReminderState,
    ReminderSuggestion,
    SuggestionState,
    User,
)
from app.db.session import SessionLocal
from app.services.persistent_policy import is_persistent_mode, is_quiet_hours, parse_clock
from app.services.recurrence import decode_rule, encode_rule, legacy_rule, next_occurrence
from app.utils.datetime_utils import from_utc_to_user, to_utc, utc_now

logger = logging.getLogger(__name__)
settings = get_settings()

SUGGESTION_KIND_SCHEDULE_TIME = "schedule_time"
SNOOZE_HISTORY_RETENTION = timedelta(days=30)
SUGGESTION_THRESHOLD = 3
SUGGESTION_TARGET_TOLERANCE_MINUTES = 20
SUGGESTION_MIN_SCHEDULE_SHIFT_MINUTES = 30
MAX_SNOOZE_EVENTS_PER_REMINDER = 32
SUGGESTION_RETENTION = timedelta(days=90)
DIGEST_DELIVERY_RETENTION = timedelta(days=90)
DEFAULT_DIGEST_MORNING_TIME = "09:00"
DEFAULT_DIGEST_EVENING_TIME = "20:00"
DEFAULT_DIGEST_QUIET_HOURS_START = "22:00"
DEFAULT_DIGEST_QUIET_HOURS_END = "08:00"
DEFAULT_DIGEST_MAX_ITEMS = 20
DEFAULT_DIGEST_MAX_DELAY_MINUTES = 360
DEFAULT_DIGEST_LEASE_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 3
DIGEST_MESSAGE_LIMIT = 4096
DIGEST_TRUNCATION_MARKER = "\nСписок ограничен безопасным размером сообщения."
TERMINAL_DIGEST_ERROR_TYPES = frozenset(
    {
        "TelegramBadRequest",
        "TelegramForbiddenError",
        "TelegramNotFound",
        "TelegramUnauthorizedError",
    }
)
REACTIVATABLE_DIGEST_SUPPRESSION_REASONS = frozenset(
    {"opt_out", "terminal_error", "profile_changed", "chat_changed"}
)
ACTIVE_DIGEST_REMINDER_STATES = (
    ReminderState.SCHEDULED.value,
    ReminderState.DELIVERED.value,
    ReminderState.SNOOZED.value,
    ReminderState.PAUSED.value,
)


@dataclass(frozen=True, slots=True)
class AdaptivePreferences:
    suggestions_enabled: bool
    digests_enabled: bool
    digest_morning_time: str
    digest_evening_time: str
    digest_quiet_hours_start: str
    digest_quiet_hours_end: str


@dataclass(frozen=True, slots=True)
class SuggestionResolution:
    status: str
    changed: bool
    already_resolved: bool = False
    suggestion_id: int | None = None
    reminder_id: int | None = None
    proposed_local_time: str | None = None


@dataclass
class AdaptiveMetrics:
    snooze_events_recorded: int = 0
    suggestions_eligible: int = 0
    suggestions_created: int = 0
    suggestions_accepted: int = 0
    suggestions_rejected: int = 0
    suggestions_dismissed: int = 0
    digests_claimed: int = 0
    digests_sent: int = 0
    digests_suppressed: int = 0
    digests_failed: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "snooze_events_recorded": self.snooze_events_recorded,
            "suggestions_eligible": self.suggestions_eligible,
            "suggestions_created": self.suggestions_created,
            "suggestions_accepted": self.suggestions_accepted,
            "suggestions_rejected": self.suggestions_rejected,
            "suggestions_dismissed": self.suggestions_dismissed,
            "digests_claimed": self.digests_claimed,
            "digests_sent": self.digests_sent,
            "digests_suppressed": self.digests_suppressed,
            "digests_failed": self.digests_failed,
        }


adaptive_metrics = AdaptiveMetrics()


def get_adaptive_metrics() -> dict[str, int]:
    return adaptive_metrics.snapshot()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _setting_str(name: str, default: str) -> str:
    value = getattr(settings, name, default)
    return value if isinstance(value, str) and value else default


def _setting_int(name: str, default: int, *, minimum: int = 0) -> int:
    value = getattr(settings, name, default)
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _default_preferences() -> AdaptivePreferences:
    return AdaptivePreferences(
        suggestions_enabled=False,
        digests_enabled=False,
        digest_morning_time=_setting_str("digest_morning_time", DEFAULT_DIGEST_MORNING_TIME),
        digest_evening_time=_setting_str("digest_evening_time", DEFAULT_DIGEST_EVENING_TIME),
        digest_quiet_hours_start=_setting_str(
            "digest_quiet_hours_start", DEFAULT_DIGEST_QUIET_HOURS_START
        ),
        digest_quiet_hours_end=_setting_str(
            "digest_quiet_hours_end", DEFAULT_DIGEST_QUIET_HOURS_END
        ),
    )


def _preferences_from_user(user: User) -> AdaptivePreferences:
    defaults = _default_preferences()
    return AdaptivePreferences(
        suggestions_enabled=bool(getattr(user, "suggestions_enabled", False)),
        digests_enabled=bool(getattr(user, "digests_enabled", False)),
        digest_morning_time=str(
            getattr(user, "digest_morning_time", None) or defaults.digest_morning_time
        ),
        digest_evening_time=str(
            getattr(user, "digest_evening_time", None) or defaults.digest_evening_time
        ),
        digest_quiet_hours_start=str(
            getattr(user, "digest_quiet_hours_start", None) or defaults.digest_quiet_hours_start
        ),
        digest_quiet_hours_end=str(
            getattr(user, "digest_quiet_hours_end", None) or defaults.digest_quiet_hours_end
        ),
    )


async def get_adaptive_preferences(user: User) -> AdaptivePreferences | None:
    async with SessionLocal() as session:
        owner = await session.scalar(
            select(User).where(User.id == user.id, User.chat_id == user.chat_id)
        )
        return _preferences_from_user(owner) if owner is not None else None


async def set_adaptive_preferences(
    user: User,
    *,
    suggestions_enabled: bool | None = None,
    digests_enabled: bool | None = None,
) -> AdaptivePreferences | None:
    async with SessionLocal() as session, session.begin():
        owner = await session.scalar(
            select(User).where(User.id == user.id, User.chat_id == user.chat_id).with_for_update()
        )
        if owner is None:
            return None
        now_utc: datetime | None = None
        if suggestions_enabled is not None:
            owner.suggestions_enabled = suggestions_enabled
            if not suggestions_enabled:
                now_utc = _as_utc(utc_now())
                await session.execute(
                    update(ReminderSuggestion)
                    .where(
                        ReminderSuggestion.user_id == owner.id,
                        ReminderSuggestion.chat_id == owner.chat_id,
                        ReminderSuggestion.status == SuggestionState.PENDING.value,
                    )
                    .values(
                        status=SuggestionState.DISMISSED.value,
                        resolution="opt_out",
                        resolved_at=now_utc,
                        revision=ReminderSuggestion.revision + 1,
                    )
                )
        if digests_enabled is not None:
            now_utc = now_utc or _as_utc(utc_now())
            owner.digests_enabled = digests_enabled
            if digests_enabled:
                await _ensure_digest_schedule(session, owner, now_utc=now_utc)
            else:
                owner.digest_schedule_seeded = False
                await session.execute(
                    update(ReminderDigestDelivery)
                    .where(
                        ReminderDigestDelivery.user_id == owner.id,
                        ReminderDigestDelivery.chat_id == owner.chat_id,
                        ReminderDigestDelivery.state == DigestDeliveryState.PENDING.value,
                    )
                    .values(
                        state=DigestDeliveryState.SUPPRESSED.value,
                        suppressed_at=now_utc,
                        suppression_reason="opt_out",
                        next_retry_at=None,
                        error_text=None,
                    )
                )
        await session.flush()
        return _preferences_from_user(owner)


def _clock_minutes(value: datetime, timezone_name: str) -> int:
    local = from_utc_to_user(_as_utc(value), timezone_name)
    return local.hour * 60 + local.minute


def _format_clock(minutes: int) -> str:
    normalized = minutes % (24 * 60)
    return f"{normalized // 60:02d}:{normalized % 60:02d}"


def _clock_distance(left: int, right: int) -> int:
    difference = abs((left % 1440) - (right % 1440))
    return min(difference, 1440 - difference)


def _snooze_history_retention() -> timedelta:
    return timedelta(
        days=_setting_int(
            "suggestion_window_days",
            SNOOZE_HISTORY_RETENTION.days,
            minimum=1,
        )
    )


def _suggestion_expiry_cutoffs(now_utc: datetime) -> tuple[datetime, datetime]:
    current_time = _as_utc(now_utc)
    return (
        current_time - SUGGESTION_RETENTION,
        current_time - _snooze_history_retention(),
    )


def _is_suggestion_expired(
    suggestion: ReminderSuggestion,
    *,
    now_utc: datetime,
) -> bool:
    created_cutoff, evidence_cutoff = _suggestion_expiry_cutoffs(now_utc)
    created_at = getattr(suggestion, "created_at", None)
    if created_at is not None and _as_utc(created_at) < created_cutoff:
        return True
    evidence_window_end = getattr(suggestion, "evidence_window_end_utc", None)
    return evidence_window_end is not None and _as_utc(evidence_window_end) < evidence_cutoff


def _mark_suggestion_expired(
    suggestion: ReminderSuggestion,
    *,
    now_utc: datetime,
    resolution: str = "evidence_expired",
) -> None:
    suggestion.status = SuggestionState.EXPIRED.value
    suggestion.resolution = resolution[:32]
    suggestion.resolved_at = _as_utc(now_utc)
    suggestion.revision += 1


async def _expire_stale_pending_suggestions(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    chat_id: int | None = None,
    now_utc: datetime,
) -> int:
    created_cutoff, evidence_cutoff = _suggestion_expiry_cutoffs(now_utc)
    filters = [
        ReminderSuggestion.status == SuggestionState.PENDING.value,
        or_(
            ReminderSuggestion.created_at < created_cutoff,
            ReminderSuggestion.evidence_window_end_utc < evidence_cutoff,
        ),
    ]
    if user_id is not None:
        filters.append(ReminderSuggestion.user_id == user_id)
    if chat_id is not None:
        filters.append(ReminderSuggestion.chat_id == chat_id)
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(ReminderSuggestion)
            .where(*filters)
            .values(
                status=SuggestionState.EXPIRED.value,
                resolution="evidence_expired",
                resolved_at=_as_utc(now_utc),
                revision=ReminderSuggestion.revision + 1,
            )
        ),
    )
    return int(result.rowcount or 0)


def _reminder_rule(reminder: Reminder) -> dict[str, Any]:
    if reminder.recurrence_rule:
        decoded = decode_rule(reminder.recurrence_rule)
        if decoded is None:
            raise ValueError("У напоминания отсутствует recurrence rule")
        return decoded
    if reminder.recurrence_type == RecurrenceType.ADVANCED.value:
        raise ValueError("У advanced reminder отсутствует recurrence rule")
    return legacy_rule(
        str(reminder.recurrence_type),
        int(reminder.recurrence_interval),
        reminder.recurrence_day_of_month,
    )


def _suggestion_eligible(reminder: Reminder) -> bool:
    if (
        reminder.kind == ReminderKind.DEADLINE.value
        or reminder.parent_reminder_id is not None
        or reminder.state
        not in {
            ReminderState.SCHEDULED.value,
            ReminderState.DELIVERED.value,
            ReminderState.SNOOZED.value,
        }
    ):
        return False
    if reminder.recurrence_type not in {
        RecurrenceType.DAILY.value,
        RecurrenceType.WEEKLY.value,
        RecurrenceType.MONTHLY.value,
        RecurrenceType.ADVANCED.value,
    }:
        return False
    try:
        return _reminder_rule(reminder).get("kind") != "completion_relative"
    except ValueError:
        return False


def _choose_target_cluster(
    events: Sequence[ReminderSnoozeEvent],
) -> tuple[int, list[ReminderSnoozeEvent]] | None:
    tolerance = _setting_int(
        "suggestion_target_tolerance_minutes",
        SUGGESTION_TARGET_TOLERANCE_MINUTES,
        minimum=1,
    )
    best: tuple[int, datetime, int, list[ReminderSnoozeEvent]] | None = None
    best_target: int | None = None
    for anchor in events:
        cluster = [
            event
            for event in events
            if _clock_distance(event.target_local_minutes, anchor.target_local_minutes) <= tolerance
        ]
        if len(cluster) < _setting_int(
            "suggestion_snooze_threshold", SUGGESTION_THRESHOLD, minimum=2
        ):
            continue
        ordered_minutes = sorted(event.target_local_minutes for event in cluster)
        proposed = ordered_minutes[len(ordered_minutes) // 2]
        latest = max(_as_utc(event.snoozed_at_utc) for event in cluster)
        score = (len(cluster), latest, -_clock_distance(proposed, anchor.target_local_minutes))
        if best is None or score[:3] > best[:3]:
            best = (
                len(cluster),
                latest,
                -_clock_distance(proposed, anchor.target_local_minutes),
                cluster,
            )
            best_target = proposed
    if best is None or best_target is None:
        return None
    return best_target, best[3]


async def _trim_snooze_history(
    session: AsyncSession,
    *,
    reminder_id: int,
    cutoff_utc: datetime,
) -> None:
    await session.execute(
        delete(ReminderSnoozeEvent).where(
            ReminderSnoozeEvent.reminder_id == reminder_id,
            ReminderSnoozeEvent.snoozed_at_utc < cutoff_utc,
        )
    )
    result = await session.execute(
        select(ReminderSnoozeEvent.id)
        .where(ReminderSnoozeEvent.reminder_id == reminder_id)
        .order_by(ReminderSnoozeEvent.snoozed_at_utc.desc(), ReminderSnoozeEvent.id.desc())
        .limit(MAX_SNOOZE_EVENTS_PER_REMINDER + 1)
    )
    retained_candidates = list(result.scalars())
    if len(retained_candidates) <= MAX_SNOOZE_EVENTS_PER_REMINDER:
        return
    await session.execute(
        delete(ReminderSnoozeEvent).where(
            ReminderSnoozeEvent.id.in_(retained_candidates[MAX_SNOOZE_EVENTS_PER_REMINDER:])
        )
    )


async def record_snooze_event_in_session(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    reminder: Reminder,
    occurrence_id: int | None,
    occurrence_at_utc: datetime,
    snoozed_at_utc: datetime,
    target_at_utc: datetime,
) -> ReminderSuggestion | None:
    """Persist opt-in snooze evidence and, when eligible, one pending suggestion."""

    if not _suggestion_eligible(reminder):
        return None

    owner = await session.scalar(select(User).where(User.id == user_id, User.chat_id == chat_id))
    if owner is None or not bool(getattr(owner, "suggestions_enabled", False)):
        return None

    snoozed_at = _as_utc(snoozed_at_utc)
    target_at = _as_utc(target_at_utc)
    event = ReminderSnoozeEvent(
        user_id=user_id,
        chat_id=chat_id,
        reminder_id=reminder.id,
        occurrence_id=occurrence_id,
        occurrence_at_utc=_as_utc(occurrence_at_utc),
        snoozed_at_utc=snoozed_at,
        target_at_utc=target_at,
        target_local_minutes=_clock_minutes(target_at, reminder.schedule_timezone),
        schedule_timezone=reminder.schedule_timezone,
    )
    session.add(event)
    await session.flush()
    adaptive_metrics.snooze_events_recorded += 1
    await _trim_snooze_history(
        session,
        reminder_id=reminder.id,
        cutoff_utc=snoozed_at - _snooze_history_retention(),
    )
    return await evaluate_snooze_suggestion_in_session(
        session,
        owner=owner,
        reminder=reminder,
        now_utc=snoozed_at,
    )


async def evaluate_snooze_suggestion_in_session(
    session: AsyncSession,
    *,
    owner: User,
    reminder: Reminder,
    now_utc: datetime,
) -> ReminderSuggestion | None:
    if not _suggestion_eligible(reminder) or not bool(getattr(owner, "suggestions_enabled", False)):
        return None

    current_time = _as_utc(now_utc)
    cutoff = current_time - _snooze_history_retention()
    result = await session.execute(
        select(ReminderSnoozeEvent)
        .where(
            ReminderSnoozeEvent.reminder_id == reminder.id,
            ReminderSnoozeEvent.user_id == owner.id,
            ReminderSnoozeEvent.chat_id == owner.chat_id,
            ReminderSnoozeEvent.snoozed_at_utc >= cutoff,
            ReminderSnoozeEvent.snoozed_at_utc <= current_time,
        )
        .order_by(ReminderSnoozeEvent.snoozed_at_utc.desc(), ReminderSnoozeEvent.id.desc())
    )
    events = list(result.scalars())
    cluster_result = _choose_target_cluster(events)
    if cluster_result is None:
        return None
    proposed_minutes, cluster = cluster_result
    current_minutes = _clock_minutes(reminder.remind_at_utc, reminder.schedule_timezone)
    if _clock_distance(current_minutes, proposed_minutes) < _setting_int(
        "suggestion_min_schedule_shift_minutes",
        SUGGESTION_MIN_SCHEDULE_SHIFT_MINUTES,
        minimum=1,
    ):
        return None

    adaptive_metrics.suggestions_eligible += 1
    pending = await session.scalar(
        select(ReminderSuggestion)
        .where(
            ReminderSuggestion.reminder_id == reminder.id,
            ReminderSuggestion.user_id == owner.id,
            ReminderSuggestion.chat_id == owner.chat_id,
            ReminderSuggestion.status == SuggestionState.PENDING.value,
        )
        .order_by(ReminderSuggestion.id.desc())
        .with_for_update()
    )
    if pending is not None:
        if not _is_suggestion_expired(pending, now_utc=current_time):
            return pending
        _mark_suggestion_expired(pending, now_utc=current_time)

    local_date = from_utc_to_user(current_time, reminder.schedule_timezone).date()
    dedupe_key = f"time:{reminder.id}:{proposed_minutes:04d}:{local_date.isoformat()}"
    existing = await session.scalar(
        select(ReminderSuggestion).where(ReminderSuggestion.dedupe_key == dedupe_key)
    )
    if existing is not None:
        return None

    suggestion = ReminderSuggestion(
        user_id=owner.id,
        chat_id=owner.chat_id,
        reminder_id=reminder.id,
        kind=SUGGESTION_KIND_SCHEDULE_TIME,
        status=SuggestionState.PENDING.value,
        revision=1,
        expected_reminder_revision=reminder.action_revision,
        current_local_minutes=current_minutes,
        proposed_local_minutes=proposed_minutes,
        evidence_count=len(cluster),
        evidence_window_start_utc=min(_as_utc(event.snoozed_at_utc) for event in cluster),
        evidence_window_end_utc=max(_as_utc(event.snoozed_at_utc) for event in cluster),
        dedupe_key=dedupe_key,
    )
    session.add(suggestion)
    await session.flush()
    adaptive_metrics.suggestions_created += 1
    logger.info(
        "Adaptive suggestion created",
        extra={
            "extra_data": (f"kind={suggestion.kind} evidence_count={suggestion.evidence_count}")
        },
    )
    return suggestion


async def evaluate_snooze_suggestion(
    user: User,
    reminder_id: int,
    *,
    now_utc: datetime | None = None,
) -> ReminderSuggestion | None:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        owner = await session.scalar(
            select(User).where(User.id == user.id, User.chat_id == user.chat_id).with_for_update()
        )
        reminder = await session.scalar(
            select(Reminder)
            .where(
                Reminder.id == reminder_id,
                Reminder.user_id == user.id,
                Reminder.chat_id == user.chat_id,
            )
            .with_for_update()
        )
        if owner is None or reminder is None:
            return None
        return await evaluate_snooze_suggestion_in_session(
            session,
            owner=owner,
            reminder=reminder,
            now_utc=current_time,
        )


async def get_pending_suggestions(
    user: User,
    *,
    reminder_id: int | None = None,
    limit: int = 10,
) -> list[ReminderSuggestion]:
    if limit < 1:
        return []
    current_time = _as_utc(utc_now())
    async with SessionLocal() as session, session.begin():
        await _expire_stale_pending_suggestions(
            session,
            user_id=user.id,
            chat_id=user.chat_id,
            now_utc=current_time,
        )
        filters = [
            ReminderSuggestion.user_id == user.id,
            ReminderSuggestion.chat_id == user.chat_id,
            ReminderSuggestion.status == SuggestionState.PENDING.value,
        ]
        if reminder_id is not None:
            filters.append(ReminderSuggestion.reminder_id == reminder_id)
        result = await session.execute(
            select(ReminderSuggestion)
            .where(*filters)
            .order_by(ReminderSuggestion.created_at.asc(), ReminderSuggestion.id.asc())
            .limit(limit)
        )
        return list(result.scalars())


async def get_pending_suggestion(
    user: User,
    *,
    reminder_id: int | None = None,
) -> ReminderSuggestion | None:
    suggestions = await get_pending_suggestions(user, reminder_id=reminder_id, limit=1)
    return suggestions[0] if suggestions else None


def format_suggestion(
    suggestion: ReminderSuggestion,
    *,
    reminder: Reminder | None = None,
) -> str:
    reminder_text = escape((reminder.text if reminder is not None else "это напоминание")[:600])
    proposed = _format_clock(suggestion.proposed_local_minutes)
    return (
        "💡 <b>Предложение по расписанию</b>\n\n"
        f"Ты уже {suggestion.evidence_count} раза переносил(а) «{reminder_text}» "
        f"примерно на <b>{proposed}</b>.\n"
        f"Перенести повторяющееся напоминание на {proposed}?\n\n"
        "Это только предложение: без подтверждения исходное расписание не изменится."
    )


def _clear_reminder_delivery_state(reminder: Reminder) -> None:
    reminder.last_message_id = None
    reminder.last_delivery_occurrence_utc = None
    reminder.retry_count = 0
    reminder.attempt_count = 0
    reminder.next_retry_at = None
    reminder.error_text = None
    reminder.processing_started_at = None
    reminder.lease_until = None
    reminder.lease_token = None


def _apply_schedule_suggestion(
    reminder: Reminder,
    *,
    proposed_local_minutes: int,
    now_utc: datetime,
) -> bool:
    if not _suggestion_eligible(reminder):
        return False
    try:
        rule = _reminder_rule(reminder)
    except ValueError:
        return False

    proposed_time = _format_clock(proposed_local_minutes)
    updated_rule = dict(rule)
    if updated_rule.get("kind") != "legacy":
        if updated_rule.get("kind") == "completion_relative":
            return False
        updated_rule["time"] = proposed_time

    current_local = from_utc_to_user(reminder.remind_at_utc, reminder.schedule_timezone)
    naive_local = current_local.replace(tzinfo=None)
    proposed_local = naive_local.replace(
        hour=proposed_local_minutes // 60,
        minute=proposed_local_minutes % 60,
        second=0,
        microsecond=0,
    )
    candidate_utc = to_utc(proposed_local, reminder.schedule_timezone)
    if candidate_utc <= _as_utc(now_utc):
        next_at_utc = next_occurrence(
            candidate_utc,
            updated_rule,
            reminder.schedule_timezone,
        )
    else:
        next_at_utc = candidate_utc
    if next_at_utc is None:
        return False

    reminder.remind_at_utc = next_at_utc
    reminder.delivery_at_utc = next_at_utc
    reminder.snoozed_until_utc = None
    reminder.state = ReminderState.SCHEDULED.value
    reminder.status = "pending"
    reminder.action_revision += 1
    if reminder.recurrence_rule and updated_rule != rule:
        reminder.recurrence_rule = encode_rule(updated_rule)
    _clear_reminder_delivery_state(reminder)
    if is_persistent_mode(reminder.mode):
        reminder.persistent_delivery_count = 0
        reminder.persistent_escalation_count = 0
        reminder.persistent_exhausted_at = None
        reminder.persistent_stop_reason = None
    return True


def _resolution(
    suggestion: ReminderSuggestion,
    *,
    status: str,
    changed: bool,
    already_resolved: bool = False,
) -> SuggestionResolution:
    return SuggestionResolution(
        status=status,
        changed=changed,
        already_resolved=already_resolved,
        suggestion_id=suggestion.id,
        reminder_id=suggestion.reminder_id,
        proposed_local_time=_format_clock(suggestion.proposed_local_minutes),
    )


async def resolve_suggestion(
    user: User,
    suggestion_id: int,
    *,
    expected_revision: int,
    action: str,
    now_utc: datetime | None = None,
) -> SuggestionResolution:
    normalized_action = str(action).strip().lower()
    if normalized_action not in {"accept", "reject", "dismiss"}:
        raise ValueError("Неизвестное действие с предложением")
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        owner = await session.scalar(
            select(User).where(User.id == user.id, User.chat_id == user.chat_id).with_for_update()
        )
        suggestion = await session.scalar(
            select(ReminderSuggestion)
            .where(
                ReminderSuggestion.id == suggestion_id,
                ReminderSuggestion.user_id == user.id,
                ReminderSuggestion.chat_id == user.chat_id,
            )
            .with_for_update()
        )
        if owner is None or suggestion is None:
            return SuggestionResolution(status="not_found", changed=False)
        if suggestion.status != SuggestionState.PENDING.value:
            return _resolution(
                suggestion,
                status=suggestion.status,
                changed=False,
                already_resolved=True,
            )
        if _is_suggestion_expired(suggestion, now_utc=current_time):
            _mark_suggestion_expired(suggestion, now_utc=current_time)
            return _resolution(suggestion, status="expired", changed=False)
        if suggestion.revision != expected_revision:
            return _resolution(suggestion, status="stale", changed=False)

        reminder = await session.scalar(
            select(Reminder)
            .where(
                Reminder.id == suggestion.reminder_id,
                Reminder.user_id == owner.id,
                Reminder.chat_id == owner.chat_id,
            )
            .with_for_update()
        )
        if reminder is None or reminder.state in {
            ReminderState.COMPLETED.value,
            ReminderState.CANCELLED.value,
            ReminderState.FAILED.value,
        }:
            suggestion.status = SuggestionState.EXPIRED.value
            suggestion.resolution = "reminder_terminal"
            suggestion.resolved_at = current_time
            suggestion.revision += 1
            return _resolution(suggestion, status="expired", changed=False)
        if reminder.status == "processing":
            suggestion.status = SuggestionState.EXPIRED.value
            suggestion.resolution = "reminder_processing"
            suggestion.resolved_at = current_time
            suggestion.revision += 1
            return _resolution(suggestion, status="expired", changed=False)

        if normalized_action == "accept":
            if reminder.action_revision != suggestion.expected_reminder_revision:
                suggestion.status = SuggestionState.EXPIRED.value
                suggestion.resolution = "stale_reminder"
                suggestion.resolved_at = current_time
                suggestion.revision += 1
                return _resolution(suggestion, status="stale", changed=False)
            if not _apply_schedule_suggestion(
                reminder,
                proposed_local_minutes=suggestion.proposed_local_minutes,
                now_utc=current_time,
            ):
                suggestion.status = SuggestionState.EXPIRED.value
                suggestion.resolution = "unsupported_schedule"
                suggestion.resolved_at = current_time
                suggestion.revision += 1
                return _resolution(suggestion, status="expired", changed=False)
            suggestion.status = SuggestionState.ACCEPTED.value
            suggestion.resolution = "accept"
            suggestion.resolved_at = current_time
            suggestion.revision += 1
            adaptive_metrics.suggestions_accepted += 1
            return _resolution(suggestion, status="accepted", changed=True)

        final_state = (
            SuggestionState.REJECTED.value
            if normalized_action == "reject"
            else SuggestionState.DISMISSED.value
        )
        suggestion.status = final_state
        suggestion.resolution = normalized_action
        suggestion.resolved_at = current_time
        suggestion.revision += 1
        if normalized_action == "reject":
            adaptive_metrics.suggestions_rejected += 1
        else:
            adaptive_metrics.suggestions_dismissed += 1
        return _resolution(suggestion, status=final_state, changed=False)


def _digest_period(value: str | DigestPeriod) -> DigestPeriod:
    try:
        return DigestPeriod(value)
    except ValueError as exc:
        raise ValueError("Неизвестный период дайджеста") from exc


def _digest_time(owner: User, period: DigestPeriod) -> str:
    defaults = _default_preferences()
    if period == DigestPeriod.MORNING:
        return str(getattr(owner, "digest_morning_time", None) or defaults.digest_morning_time)
    return str(getattr(owner, "digest_evening_time", None) or defaults.digest_evening_time)


def _digest_quiet_hours(owner: User) -> tuple[str, str]:
    defaults = _default_preferences()
    return (
        str(getattr(owner, "digest_quiet_hours_start", None) or defaults.digest_quiet_hours_start),
        str(getattr(owner, "digest_quiet_hours_end", None) or defaults.digest_quiet_hours_end),
    )


def digest_suppression_reason(
    owner: User,
    period: str | DigestPeriod,
    *,
    scheduled_at_utc: datetime,
    now_utc: datetime,
) -> str | None:
    normalized_period = _digest_period(period)
    current_time = _as_utc(now_utc)
    scheduled = _as_utc(scheduled_at_utc)
    if current_time < scheduled:
        return "not_due"
    max_delay = timedelta(
        minutes=_setting_int(
            "digest_max_delay_minutes", DEFAULT_DIGEST_MAX_DELAY_MINUTES, minimum=0
        )
    )
    if current_time - scheduled > max_delay:
        return "stale"
    quiet_start, quiet_end = _digest_quiet_hours(owner)
    if is_quiet_hours(
        current_time,
        owner.timezone,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
    ) or is_quiet_hours(
        scheduled,
        owner.timezone,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
    ):
        return "quiet_hours"
    del normalized_period
    return None


def _digest_sort_key(reminder: Reminder) -> tuple[datetime, int]:
    scheduled = reminder.delivery_at_utc or reminder.remind_at_utc
    return _as_utc(scheduled), reminder.id


async def _load_digest_reminders(
    session: AsyncSession,
    owner: User,
) -> list[Reminder]:
    max_items = _setting_int("digest_max_items", DEFAULT_DIGEST_MAX_ITEMS, minimum=1)
    common_filters = (
        Reminder.user_id == owner.id,
        Reminder.chat_id == owner.chat_id,
        Reminder.state.in_(ACTIVE_DIGEST_REMINDER_STATES),
        Reminder.status != "processing",
    )
    important_result = await session.execute(
        select(Reminder)
        .where(*common_filters, Reminder.mode == "persistent")
        .order_by(
            func.coalesce(Reminder.delivery_at_utc, Reminder.remind_at_utc).asc(),
            Reminder.id.asc(),
        )
    )
    ordinary_result = await session.execute(
        select(Reminder)
        .where(
            *common_filters,
            or_(Reminder.mode.is_(None), Reminder.mode != "persistent"),
        )
        .order_by(
            func.coalesce(Reminder.delivery_at_utc, Reminder.remind_at_utc).asc(),
            Reminder.id.asc(),
        )
        .limit(max_items)
    )
    selected = list(important_result.scalars()) + list(ordinary_result.scalars())
    selected.sort(key=_digest_sort_key)
    return selected


def _state_label(state: str) -> str:
    return {
        ReminderState.SCHEDULED.value: "запланировано",
        ReminderState.DELIVERED.value: "доставлено",
        ReminderState.SNOOZED.value: "отложено",
        ReminderState.PAUSED.value: "на паузе",
    }.get(state, state)


def _escape_bounded(value: str, max_length: int) -> str:
    if max_length <= 0:
        return ""
    escaped = escape(value)
    if len(escaped) <= max_length:
        return escaped
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if len(escape(value[:middle])) <= max_length - 1:
            low = middle
        else:
            high = middle - 1
    return f"{escape(value[:low])}…"


def render_digest(
    period: str | DigestPeriod,
    reminders: Sequence[Reminder],
    *,
    timezone_name: str,
    local_date: date,
) -> str:
    normalized_period = _digest_period(period)
    heading = "🌅 Утренний" if normalized_period == DigestPeriod.MORNING else "🌆 Вечерний"
    result = [f"{heading} <b>дайджест</b> · {local_date.strftime('%d.%m.%Y')}\n"]
    if not reminders:
        return "".join(result) + "Незавершённых напоминаний нет."
    result.append("Незавершённые напоминания:\n")
    # Persistent reminders must get first claim on the bounded Telegram
    # message budget. Otherwise a long chronological prefix of ordinary
    # reminders can hide a later important item entirely.
    persistent_reminders = sorted(
        (reminder for reminder in reminders if is_persistent_mode(reminder.mode)),
        key=_digest_sort_key,
    )
    ordinary = sorted(
        (reminder for reminder in reminders if not is_persistent_mode(reminder.mode)),
        key=_digest_sort_key,
    )

    def render_line(index: int, reminder: Reminder, *, compact: bool = False) -> str:
        if compact:
            # The identifier is a deterministic, compact representation of an
            # important reminder. It lets every persistent item survive even
            # when its full text cannot fit in Telegram's single-message cap.
            return f"{index}.🔔#{reminder.id}\n"
        scheduled = reminder.delivery_at_utc or reminder.remind_at_utc
        local_scheduled = from_utc_to_user(_as_utc(scheduled), timezone_name)
        important = "🔔 ВАЖНОЕ · " if is_persistent_mode(reminder.mode) else ""
        return (
            f"{index}. {important}<b>#{reminder.id}</b> "
            f"{local_scheduled.strftime('%d.%m %H:%M')} · "
            f"{_state_label(reminder.state)}\n"
            f"   {_escape_bounded(reminder.text, 360)}\n"
        )

    persistent_full_lines = [
        render_line(index, reminder) for index, reminder in enumerate(persistent_reminders, start=1)
    ]
    persistent_compact_lines = [
        render_line(index, reminder, compact=True)
        for index, reminder in enumerate(persistent_reminders, start=1)
    ]
    persistent_aggregate_line = (
        f"🔔 ВАЖНЫЕ НАПОМИНАНИЯ: {len(persistent_reminders)} шт. · "
        "все учтены; полный список: /list\n"
    )
    current_length = len("".join(result))
    marker_reservation = len(DIGEST_TRUNCATION_MARKER) if ordinary else 0
    compact_length = current_length + sum(map(len, persistent_compact_lines))
    if compact_length + marker_reservation > DIGEST_MESSAGE_LIMIT:
        # Individual identifiers are still bounded per item, but the set of
        # persistent reminders is not. Keep one deterministic, actionable
        # aggregate instead of emitting an invalid payload or silently
        # dropping the tail. `/list` is the continuation surface for details.
        persistent_lines = [persistent_aggregate_line]
        current_length += len(persistent_aggregate_line)
    else:
        persistent_lines = list(persistent_compact_lines)
        current_length = compact_length
        for index, full_line in enumerate(persistent_full_lines):
            extra = len(full_line) - len(persistent_lines[index])
            if current_length + extra + marker_reservation <= DIGEST_MESSAGE_LIMIT:
                persistent_lines[index] = full_line
                current_length += extra
    result.append("".join(persistent_lines))

    for index, reminder in enumerate(ordinary, start=len(persistent_reminders) + 1):
        line = render_line(index, reminder)
        if current_length + len(line) + len(DIGEST_TRUNCATION_MARKER) > DIGEST_MESSAGE_LIMIT:
            if current_length + len(DIGEST_TRUNCATION_MARKER) <= DIGEST_MESSAGE_LIMIT:
                result.append(DIGEST_TRUNCATION_MARKER)
            break
        result.append(line)
        current_length += len(line)
    return "".join(result)


async def build_digest_text(
    user: User,
    period: str | DigestPeriod,
    *,
    local_date: date | None = None,
    now_utc: datetime | None = None,
) -> str:
    normalized_period = _digest_period(period)
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session:
        owner = await session.scalar(
            select(User).where(User.id == user.id, User.chat_id == user.chat_id)
        )
        if owner is None:
            return "Незавершённых напоминаний нет."
        digest_date = local_date or from_utc_to_user(current_time, owner.timezone).date()
        reminders = await _load_digest_reminders(session, owner)
        return render_digest(
            normalized_period,
            reminders,
            timezone_name=owner.timezone,
            local_date=digest_date,
        )


async def _create_digest_delivery(
    session: AsyncSession,
    *,
    owner: User,
    period: DigestPeriod,
    local_date: date,
    scheduled_at_utc: datetime,
    state: str,
    now_utc: datetime,
    suppression_reason: str | None = None,
) -> bool:
    delivery = ReminderDigestDelivery(
        user_id=owner.id,
        chat_id=owner.chat_id,
        period=period.value,
        local_date=local_date,
        scheduled_at_utc=_as_utc(scheduled_at_utc),
        state=state,
        suppressed_at=_as_utc(now_utc) if state == DigestDeliveryState.SUPPRESSED.value else None,
        suppression_reason=suppression_reason,
    )
    try:
        async with session.begin_nested():
            session.add(delivery)
            await session.flush()
    except IntegrityError:
        return False
    return True


def _digest_lease_seconds() -> int:
    configured_lease_seconds = _setting_int(
        "digest_lease_duration_seconds",
        _setting_int("worker_lease_duration_seconds", DEFAULT_DIGEST_LEASE_SECONDS, minimum=1),
        minimum=1,
    )
    lease_floor = _setting_int("worker_send_timeout_seconds", 30, minimum=1) + _setting_int(
        "worker_lease_safety_margin_seconds", 10, minimum=1
    )
    return max(configured_lease_seconds, lease_floor)


def _digest_slot_for_now(
    owner: User,
    period: DigestPeriod,
    *,
    now_utc: datetime,
    next_if_due: bool,
) -> tuple[date, datetime]:
    local_now = from_utc_to_user(_as_utc(now_utc), owner.timezone)
    scheduled_local_time = parse_clock(_digest_time(owner, period), field_name="digest_time")
    local_date = local_now.date()
    if next_if_due and local_now.time().replace(tzinfo=None) >= scheduled_local_time:
        local_date += timedelta(days=1)
    return local_date, to_utc(
        datetime.combine(local_date, scheduled_local_time),
        owner.timezone,
    )


async def _ensure_digest_schedule(
    session: AsyncSession,
    owner: User,
    *,
    now_utc: datetime,
    next_if_due: bool = False,
) -> None:
    """Keep one current/next slot per period so the worker only scans due rows.

    ``digest_schedule_seeded`` is a durable bootstrap marker for users that
    were enabled outside the command handler. Once seeded, normal worker polls
    use the indexed delivery queue and do not walk the whole opt-in population.
    """

    def reactivate(delivery: ReminderDigestDelivery, scheduled_at_utc: datetime) -> None:
        delivery.chat_id = owner.chat_id
        delivery.scheduled_at_utc = scheduled_at_utc
        delivery.state = DigestDeliveryState.PENDING.value
        delivery.suppressed_at = None
        delivery.suppression_reason = None
        delivery.next_retry_at = None
        delivery.error_text = None
        delivery.attempt_count = 0
        delivery.retry_count = 0

    for period in (DigestPeriod.MORNING, DigestPeriod.EVENING):
        local_date, scheduled_at_utc = _digest_slot_for_now(
            owner,
            period,
            now_utc=now_utc,
            next_if_due=next_if_due,
        )
        existing = await session.scalar(
            select(ReminderDigestDelivery)
            .where(
                ReminderDigestDelivery.user_id == owner.id,
                ReminderDigestDelivery.period == period.value,
                ReminderDigestDelivery.local_date == local_date,
            )
            .with_for_update()
        )
        if existing is None:
            await _create_digest_delivery(
                session,
                owner=owner,
                period=period,
                local_date=local_date,
                scheduled_at_utc=scheduled_at_utc,
                state=DigestDeliveryState.PENDING.value,
                now_utc=now_utc,
            )
        elif (
            existing.state == DigestDeliveryState.SUPPRESSED.value
            and existing.suppression_reason in REACTIVATABLE_DIGEST_SUPPRESSION_REASONS
        ):
            # Re-enabling the feature or changing the delivery profile should
            # make the withdrawn slot available with the current chat/timezone.
            reactivate(existing, scheduled_at_utc)
        elif (
            existing.state
            not in {
                DigestDeliveryState.PENDING.value,
                DigestDeliveryState.PROCESSING.value,
            }
            and not next_if_due
        ):
            # A historical SENT/FAILED/SUPPRESSED row is not a schedule for
            # the next occurrence. This also lets a bounded bootstrap pass
            # through users that already have today's delivery record.
            next_local_date, next_scheduled_at_utc = _digest_slot_for_now(
                owner,
                period,
                now_utc=now_utc,
                next_if_due=True,
            )
            if next_local_date != local_date:
                next_existing = await session.scalar(
                    select(ReminderDigestDelivery)
                    .where(
                        ReminderDigestDelivery.user_id == owner.id,
                        ReminderDigestDelivery.period == period.value,
                        ReminderDigestDelivery.local_date == next_local_date,
                    )
                    .with_for_update()
                )
                if next_existing is None:
                    await _create_digest_delivery(
                        session,
                        owner=owner,
                        period=period,
                        local_date=next_local_date,
                        scheduled_at_utc=next_scheduled_at_utc,
                        state=DigestDeliveryState.PENDING.value,
                        now_utc=now_utc,
                    )
                elif (
                    next_existing.state == DigestDeliveryState.SUPPRESSED.value
                    and next_existing.suppression_reason in REACTIVATABLE_DIGEST_SUPPRESSION_REASONS
                ):
                    reactivate(next_existing, next_scheduled_at_utc)
    owner.digest_schedule_seeded = True


async def claim_due_digests(
    limit: int,
    *,
    now_utc: datetime | None = None,
) -> list[ReminderDigestDelivery]:
    if limit < 1:
        return []
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        # Seeding is bounded and happens once per enabled user. Every normal
        # poll after that uses the indexed delivery schedule, so an idle
        # worker does not rescan every digest-enabled user.
        seed_page_size = max(limit * 2, limit)
        last_seeded_user_id = 0
        while True:
            users_result = await session.execute(
                select(User)
                .where(
                    User.digests_enabled.is_(True),
                    User.digest_schedule_seeded.is_(False),
                    User.id > last_seeded_user_id,
                )
                .order_by(User.id.asc())
                .with_for_update(skip_locked=True)
                .limit(seed_page_size)
            )
            owners = list(users_result.scalars())
            if not owners:
                break
            for owner in owners:
                await _ensure_digest_schedule(session, owner, now_utc=current_time)
            last_seeded_user_id = owners[-1].id

        due_filter = and_(
            ReminderDigestDelivery.state == DigestDeliveryState.PENDING.value,
            ReminderDigestDelivery.scheduled_at_utc <= current_time,
            or_(
                ReminderDigestDelivery.next_retry_at.is_(None),
                ReminderDigestDelivery.next_retry_at <= current_time,
            ),
        )
        recoverable_filter = and_(
            ReminderDigestDelivery.state == DigestDeliveryState.PROCESSING.value,
            or_(
                ReminderDigestDelivery.lease_until.is_(None),
                ReminderDigestDelivery.lease_until <= current_time,
            ),
        )
        result = await session.execute(
            select(ReminderDigestDelivery)
            .where(or_(due_filter, recoverable_filter))
            .order_by(
                ReminderDigestDelivery.scheduled_at_utc.asc(), ReminderDigestDelivery.id.asc()
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        candidates = list(result.scalars())
        claimed: list[ReminderDigestDelivery] = []
        max_attempts = _setting_int("worker_max_attempts", DEFAULT_MAX_ATTEMPTS, minimum=1)
        lease_seconds = _digest_lease_seconds()
        for delivery in candidates:
            digest_owner = await session.scalar(select(User).where(User.id == delivery.user_id))
            if digest_owner is None:
                delivery.state = DigestDeliveryState.FAILED.value
                delivery.error_text = "owner_missing"
                continue
            reason = digest_suppression_reason(
                digest_owner,
                delivery.period,
                scheduled_at_utc=delivery.scheduled_at_utc,
                now_utc=current_time,
            )
            if reason in {"stale", "quiet_hours"}:
                delivery.state = DigestDeliveryState.SUPPRESSED.value
                delivery.suppressed_at = current_time
                delivery.suppression_reason = reason
                delivery.lease_token = None
                delivery.lease_until = None
                adaptive_metrics.digests_suppressed += 1
                if digest_owner.digests_enabled:
                    await _ensure_digest_schedule(
                        session,
                        digest_owner,
                        now_utc=current_time,
                        next_if_due=True,
                    )
                continue
            if delivery.attempt_count >= max_attempts:
                delivery.state = DigestDeliveryState.FAILED.value
                delivery.error_text = "attempt_limit_exhausted"
                delivery.next_retry_at = None
                adaptive_metrics.digests_failed += 1
                if digest_owner.digests_enabled:
                    await _ensure_digest_schedule(
                        session,
                        digest_owner,
                        now_utc=current_time,
                        next_if_due=True,
                    )
                continue
            delivery.state = DigestDeliveryState.PROCESSING.value
            delivery.processing_started_at = current_time
            delivery.lease_until = current_time + timedelta(seconds=lease_seconds)
            delivery.lease_token = uuid4().hex
            delivery.next_retry_at = None
            delivery.attempt_count += 1
            claimed.append(delivery)
        adaptive_metrics.digests_claimed += len(claimed)
        return claimed


async def renew_digest_delivery_lease(
    delivery_id: int,
    lease_token: str,
    *,
    now_utc: datetime | None = None,
) -> bool:
    """Renew a still-owned digest lease immediately before Telegram send."""

    if not lease_token:
        return False
    current_time = _as_utc(now_utc or utc_now())
    renewed_until = current_time + timedelta(seconds=_digest_lease_seconds())
    async with SessionLocal() as session, session.begin():
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(ReminderDigestDelivery)
                .where(
                    ReminderDigestDelivery.id == delivery_id,
                    ReminderDigestDelivery.state == DigestDeliveryState.PROCESSING.value,
                    ReminderDigestDelivery.lease_token == lease_token,
                )
                .values(lease_until=renewed_until)
            ),
        )
        return bool(result.rowcount)


async def _load_digest_owner(delivery: ReminderDigestDelivery) -> User | None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(User).where(
                User.id == delivery.user_id,
                User.chat_id == delivery.chat_id,
            )
        )
        return result.scalar_one_or_none()


def _clear_digest_lease(delivery: ReminderDigestDelivery) -> None:
    delivery.processing_started_at = None
    delivery.lease_until = None
    delivery.lease_token = None


async def finalize_digest_success(
    delivery_id: int,
    lease_token: str,
    message_id: int,
    *,
    now_utc: datetime | None = None,
) -> bool:
    if not lease_token or message_id < 1:
        return False
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        delivery_user_id = await session.scalar(
            select(ReminderDigestDelivery.user_id).where(
                ReminderDigestDelivery.id == delivery_id,
            )
        )
        if delivery_user_id is None:
            return False
        owner = await session.scalar(
            select(User).where(User.id == delivery_user_id).with_for_update()
        )
        delivery = await session.scalar(
            select(ReminderDigestDelivery)
            .where(
                ReminderDigestDelivery.id == delivery_id,
                ReminderDigestDelivery.state == DigestDeliveryState.PROCESSING.value,
                ReminderDigestDelivery.lease_token == lease_token,
                ReminderDigestDelivery.lease_until > current_time,
            )
            .with_for_update()
        )
        if delivery is None:
            return False
        delivery.state = DigestDeliveryState.SENT.value
        delivery.message_id = message_id
        delivery.sent_at = current_time
        delivery.error_text = None
        delivery.retry_count = 0
        _clear_digest_lease(delivery)
        if owner is not None and owner.digests_enabled:
            await _ensure_digest_schedule(session, owner, now_utc=current_time, next_if_due=True)
        elif owner is not None:
            owner.digest_schedule_seeded = False
        return True


async def suppress_digest_delivery(
    delivery_id: int,
    lease_token: str,
    *,
    reason: str = "opt_out",
    now_utc: datetime | None = None,
) -> bool:
    """Suppress a claimed digest when its opt-in is withdrawn before send."""

    if not lease_token:
        return False
    current_time = _as_utc(now_utc or utc_now())
    safe_reason = str(reason)[:32] or "suppressed"
    async with SessionLocal() as session, session.begin():
        delivery = await session.scalar(
            select(ReminderDigestDelivery)
            .where(
                ReminderDigestDelivery.id == delivery_id,
                ReminderDigestDelivery.state == DigestDeliveryState.PROCESSING.value,
                ReminderDigestDelivery.lease_token == lease_token,
                ReminderDigestDelivery.lease_until > current_time,
            )
            .with_for_update()
        )
        if delivery is None:
            return False
        delivery.state = DigestDeliveryState.SUPPRESSED.value
        delivery.suppressed_at = current_time
        delivery.suppression_reason = safe_reason
        delivery.next_retry_at = None
        delivery.error_text = None
        _clear_digest_lease(delivery)
        adaptive_metrics.digests_suppressed += 1
        return True


async def finalize_digest_failure(
    delivery_id: int,
    lease_token: str,
    error_type: str,
    *,
    retry_after_seconds: int | None = None,
    terminal: bool = False,
    now_utc: datetime | None = None,
) -> bool:
    if not lease_token:
        return False
    current_time = _as_utc(now_utc or utc_now())
    safe_error = str(error_type).split(" ", 1)[0][:80] or "WorkerError"
    async with SessionLocal() as session, session.begin():
        delivery_user_id = await session.scalar(
            select(ReminderDigestDelivery.user_id).where(
                ReminderDigestDelivery.id == delivery_id,
            )
        )
        if delivery_user_id is None:
            return False
        owner = await session.scalar(
            select(User).where(User.id == delivery_user_id).with_for_update()
        )
        delivery = await session.scalar(
            select(ReminderDigestDelivery)
            .where(
                ReminderDigestDelivery.id == delivery_id,
                ReminderDigestDelivery.state == DigestDeliveryState.PROCESSING.value,
                ReminderDigestDelivery.lease_token == lease_token,
                ReminderDigestDelivery.lease_until > current_time,
            )
            .with_for_update()
        )
        if delivery is None:
            return False
        delivery.retry_count += 1
        delivery.error_text = safe_error
        _clear_digest_lease(delivery)
        max_attempts = _setting_int("worker_max_attempts", DEFAULT_MAX_ATTEMPTS, minimum=1)
        if terminal:
            delivery.state = DigestDeliveryState.FAILED.value
            delivery.next_retry_at = None
            if owner is not None:
                owner.digests_enabled = False
                owner.digest_schedule_seeded = False
                suppressed_result = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(ReminderDigestDelivery)
                        .where(
                            ReminderDigestDelivery.user_id == owner.id,
                            ReminderDigestDelivery.state == DigestDeliveryState.PENDING.value,
                        )
                        .values(
                            state=DigestDeliveryState.SUPPRESSED.value,
                            suppressed_at=current_time,
                            suppression_reason="terminal_error",
                            next_retry_at=None,
                            error_text=safe_error,
                        )
                    ),
                )
                adaptive_metrics.digests_suppressed += int(suppressed_result.rowcount or 0)
            adaptive_metrics.digests_failed += 1
            return True
        if delivery.attempt_count >= max_attempts:
            delivery.state = DigestDeliveryState.FAILED.value
            delivery.next_retry_at = None
            if owner is not None and owner.digests_enabled:
                await _ensure_digest_schedule(
                    session,
                    owner,
                    now_utc=current_time,
                    next_if_due=True,
                )
            adaptive_metrics.digests_failed += 1
            return True
        base_seconds = _setting_int("worker_retry_base_seconds", 10, minimum=1)
        max_seconds = _setting_int("worker_retry_max_seconds", 300, minimum=1)
        exponential_delay = base_seconds * (2 ** min(delivery.retry_count - 1, 30))
        if retry_after_seconds is not None:
            # Telegram's explicit rate-limit delay is provider guidance, not
            # generic exponential backoff. Never shorten it to the generic cap.
            delay = max(exponential_delay, retry_after_seconds)
        else:
            delay = min(max_seconds, exponential_delay)
        delivery.state = DigestDeliveryState.PENDING.value
        delivery.next_retry_at = current_time + timedelta(seconds=max(1, delay))
        return True


def _retry_after_seconds(exc: BaseException) -> int | None:
    if not isinstance(exc, TelegramRetryAfter) and type(exc).__name__ != "TelegramRetryAfter":
        return None
    value = getattr(exc, "retry_after", None)
    if not isinstance(value, (int, float, str)):
        return None
    try:
        numeric_value = float(value)
        if not math.isfinite(numeric_value):
            return None
        return max(0, math.ceil(numeric_value))
    except (TypeError, ValueError, OverflowError):
        return None


def _is_terminal_digest_error(exc: BaseException) -> bool:
    error_type = type(exc).__name__
    return (
        isinstance(
            exc,
            (
                TelegramBadRequest,
                TelegramForbiddenError,
                TelegramNotFound,
                TelegramUnauthorizedError,
            ),
        )
        or error_type in TERMINAL_DIGEST_ERROR_TYPES
    )


async def process_due_digests(
    bot: Bot,
    *,
    limit: int | None = None,
    stop_event: asyncio.Event | None = None,
) -> int:
    if stop_event is not None and stop_event.is_set():
        return 0
    batch_limit = limit or _setting_int("worker_batch_size", 100, minimum=1)
    deliveries = await claim_due_digests(batch_limit)
    processed = 0
    for delivery in deliveries:
        if stop_event is not None and stop_event.is_set():
            break
        lease_token = delivery.lease_token
        if not lease_token:
            continue
        try:
            owner = await _load_digest_owner(delivery)
            if owner is None:
                await finalize_digest_failure(
                    delivery.id,
                    lease_token,
                    "owner_missing",
                )
                continue
            if not owner.digests_enabled:
                await suppress_digest_delivery(delivery.id, lease_token)
                continue

            text = await build_digest_text(
                owner,
                delivery.period,
                local_date=delivery.local_date,
                now_utc=utc_now(),
            )
            # Re-read the opt-in immediately before the external provider call.
            # The claim transaction cannot protect this decision across a
            # network request, so this closes the normal /digest off race.
            owner = await _load_digest_owner(delivery)
            if owner is None:
                await finalize_digest_failure(
                    delivery.id,
                    lease_token,
                    "owner_missing",
                )
                continue
            if not owner.digests_enabled:
                await suppress_digest_delivery(delivery.id, lease_token)
                continue
            if not await renew_digest_delivery_lease(delivery.id, lease_token):
                # A different worker may have reclaimed the row, or the
                # original lease may have expired while the digest was built.
                # Do not cross that ownership boundary with an external send.
                continue
            sent = await asyncio.wait_for(
                bot.send_message(chat_id=delivery.chat_id, text=text, parse_mode="HTML"),
                timeout=_setting_int("worker_send_timeout_seconds", 30, minimum=1),
            )
            message_id = getattr(sent, "message_id", None)
            if not isinstance(message_id, int) or message_id < 1:
                raise RuntimeError("digest_message_id_missing")
            if await finalize_digest_success(delivery.id, lease_token, message_id):
                adaptive_metrics.digests_sent += 1
                processed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await finalize_digest_failure(
                delivery.id,
                lease_token,
                type(exc).__name__,
                retry_after_seconds=_retry_after_seconds(exc),
                terminal=_is_terminal_digest_error(exc),
            )
            logger.warning(
                "Digest delivery failed",
                extra={
                    "extra_data": f"period={delivery.period} error_type={type(exc).__name__[:80]}"
                },
            )
    return processed


async def cleanup_adaptive_data(*, now_utc: datetime | None = None) -> dict[str, int]:
    current_time = _as_utc(now_utc or utc_now())
    event_cutoff = current_time - _snooze_history_retention()
    suggestion_cutoff = current_time - SUGGESTION_RETENTION
    digest_cutoff = (current_time - DIGEST_DELIVERY_RETENTION).date()
    async with SessionLocal() as session, session.begin():
        await _expire_stale_pending_suggestions(
            session,
            now_utc=current_time,
        )
        event_result = cast(
            CursorResult[Any],
            await session.execute(
                delete(ReminderSnoozeEvent).where(ReminderSnoozeEvent.snoozed_at_utc < event_cutoff)
            ),
        )
        suggestion_result = cast(
            CursorResult[Any],
            await session.execute(
                delete(ReminderSuggestion).where(
                    ReminderSuggestion.status != SuggestionState.PENDING.value,
                    ReminderSuggestion.resolved_at.is_not(None),
                    ReminderSuggestion.resolved_at < suggestion_cutoff,
                )
            ),
        )
        digest_result = cast(
            CursorResult[Any],
            await session.execute(
                delete(ReminderDigestDelivery).where(
                    ReminderDigestDelivery.local_date < digest_cutoff,
                    ReminderDigestDelivery.state != DigestDeliveryState.PROCESSING.value,
                )
            ),
        )
        return {
            "snooze_events": int(event_result.rowcount or 0),
            "suggestions": int(suggestion_result.rowcount or 0),
            "digest_deliveries": int(digest_result.rowcount or 0),
        }
