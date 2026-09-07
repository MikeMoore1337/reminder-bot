from __future__ import annotations

import calendar
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from html import escape
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult

from app.db.models import (
    ActionDraft,
    OccurrenceState,
    RecurrenceType,
    Reminder,
    ReminderOccurrence,
    ReminderState,
    User,
)
from app.db.session import SessionLocal
from app.services.reminder_parser import parse_reminder_input
from app.utils.datetime_utils import (
    DatetimeSemantics,
    from_utc_to_user,
    resolve_schedule_datetime,
    to_utc,
    utc_now,
)

logger = logging.getLogger(__name__)

ACTIVE_STATES = (
    ReminderState.SCHEDULED.value,
    ReminderState.DELIVERED.value,
    ReminderState.SNOOZED.value,
    ReminderState.PAUSED.value,
)
FLOW_TTL = timedelta(minutes=15)
MAX_REMINDER_TEXT_LENGTH = 4096


@dataclass
class ActionMetrics:
    action_success: int = 0
    stale_callback: int = 0
    malformed_callback: int = 0
    unauthorized_callback: int = 0
    completed: int = 0
    snoozed: int = 0
    paused: int = 0
    resumed: int = 0
    cancelled: int = 0
    edit_success: int = 0
    edit_failure: int = 0
    expired_draft: int = 0

    def record(self, name: str) -> None:
        if hasattr(self, name):
            setattr(self, name, getattr(self, name) + 1)

    def snapshot(self) -> dict[str, int]:
        return {
            "action_success": self.action_success,
            "stale_callback": self.stale_callback,
            "malformed_callback": self.malformed_callback,
            "unauthorized_callback": self.unauthorized_callback,
            "completed": self.completed,
            "snoozed": self.snoozed,
            "paused": self.paused,
            "resumed": self.resumed,
            "cancelled": self.cancelled,
            "edit_success": self.edit_success,
            "edit_failure": self.edit_failure,
            "expired_draft": self.expired_draft,
        }


action_metrics = ActionMetrics()


@dataclass(frozen=True, slots=True)
class ParsedEditSchedule:
    local_dt: datetime
    recurrence_type: str = RecurrenceType.NONE.value
    recurrence_interval: int = 1
    datetime_semantics: DatetimeSemantics = "wall_clock"


@dataclass(frozen=True, slots=True)
class ParsedCustomDatetime:
    local_dt: datetime
    datetime_semantics: DatetimeSemantics = "wall_clock"


def get_action_metrics() -> dict[str, int]:
    return action_metrics.snapshot()


def _record_action(
    metric: str,
    *,
    action: str,
    reminder_id: int | None = None,
    revision: int | None = None,
    reason: str | None = None,
) -> None:
    action_metrics.record(metric)
    fields = [f"action={action}"]
    if reminder_id is not None:
        fields.append(f"reminder_id={reminder_id}")
    if revision is not None:
        fields.append(f"revision={revision}")
    if reason is not None:
        fields.append(f"reason={reason}")
    logger.info("Reminder action", extra={"extra_data": " ".join(fields)})


def record_malformed_callback() -> None:
    _record_action("malformed_callback", action="callback", reason="malformed")


def record_unauthorized_callback() -> None:
    _record_action("unauthorized_callback", action="callback", reason="target")


def _invalid_action(
    action: str,
    *,
    reminder_id: int | None = None,
    revision: int | None = None,
    reason: str,
) -> None:
    metric = "unauthorized_callback" if reason in {"owner", "chat"} else "stale_callback"
    _record_action(
        metric,
        action=action,
        reminder_id=reminder_id,
        revision=revision,
        reason=reason,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _next_month(dt: datetime, day_of_month: int | None = None) -> datetime:
    year = dt.year + (1 if dt.month == 12 else 0)
    month = 1 if dt.month == 12 else dt.month + 1
    day = min(day_of_month or dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def validate_recurrence(recurrence_type: str, recurrence_interval: int) -> None:
    if recurrence_interval < 1:
        raise ValueError("Интервал повторения должен быть больше 0")

    if recurrence_type == RecurrenceType.MINUTES.value and recurrence_interval < 5:
        raise ValueError("Минимальный интервал для повторения в минутах - 5 минут")

    if (
        recurrence_type
        in {
            RecurrenceType.HOURLY.value,
            RecurrenceType.DAILY.value,
            RecurrenceType.WEEKLY.value,
            RecurrenceType.MONTHLY.value,
        }
        and recurrence_interval < 1
    ):
        raise ValueError("Интервал повторения должен быть не меньше 1")


def calculate_next_occurrence(
    remind_at_utc: datetime,
    recurrence_type: str,
    recurrence_interval: int,
    timezone_name: str = "UTC",
    recurrence_day_of_month: int | None = None,
) -> datetime | None:
    if recurrence_type == RecurrenceType.NONE.value:
        return None

    if recurrence_type == RecurrenceType.MINUTES.value:
        return remind_at_utc + timedelta(minutes=recurrence_interval)

    if recurrence_type == RecurrenceType.HOURLY.value:
        return remind_at_utc + timedelta(hours=recurrence_interval)

    local_dt = from_utc_to_user(remind_at_utc, timezone_name).replace(tzinfo=None)

    if recurrence_type == RecurrenceType.DAILY.value:
        next_local_dt = local_dt + timedelta(days=recurrence_interval)
        return to_utc(next_local_dt, timezone_name)

    if recurrence_type == RecurrenceType.WEEKLY.value:
        next_local_dt = local_dt + timedelta(weeks=recurrence_interval)
        return to_utc(next_local_dt, timezone_name)

    if recurrence_type == RecurrenceType.MONTHLY.value:
        next_dt = local_dt
        for _ in range(recurrence_interval):
            next_dt = _next_month(next_dt, recurrence_day_of_month)
        return to_utc(next_dt, timezone_name)

    raise ValueError(f"Unsupported recurrence_type: {recurrence_type}")


def advance_occurrence_until_future(
    remind_at_utc: datetime,
    recurrence_type: str,
    recurrence_interval: int,
    timezone_name: str,
    recurrence_day_of_month: int | None,
    now_utc: datetime,
) -> datetime | None:
    next_occurrence = calculate_next_occurrence(
        remind_at_utc,
        recurrence_type,
        recurrence_interval,
        timezone_name=timezone_name,
        recurrence_day_of_month=recurrence_day_of_month,
    )
    while next_occurrence is not None and next_occurrence <= now_utc:
        next_occurrence = calculate_next_occurrence(
            next_occurrence,
            recurrence_type,
            recurrence_interval,
            timezone_name=timezone_name,
            recurrence_day_of_month=recurrence_day_of_month,
        )
    return next_occurrence


def build_snooze_state(
    canonical_at_utc: datetime,
    now_utc: datetime,
    minutes: int,
) -> tuple[datetime, datetime]:
    if minutes < 1:
        raise ValueError("Время откладывания должно быть больше 0 минут")
    return canonical_at_utc, now_utc + timedelta(minutes=minutes)


def delivery_at_utc(reminder: Reminder) -> datetime:
    return reminder.delivery_at_utc or reminder.remind_at_utc


def _next_local_clock_target(now_utc: datetime, timezone_name: str, target_time: time) -> datetime:
    local_now = from_utc_to_user(now_utc, timezone_name)
    target_local = datetime.combine(local_now.date(), target_time)
    if target_local <= local_now.replace(tzinfo=None):
        target_local += timedelta(days=1)
    return to_utc(target_local, timezone_name)


def calculate_snooze_target(
    preset: str,
    *,
    now_utc: datetime,
    timezone_name: str,
) -> datetime:
    normalized = preset.strip().lower()
    if normalized in {"10m", "10", "10 минут", "10минут"}:
        return _as_utc(now_utc) + timedelta(minutes=10)
    if normalized in {"1h", "1 час", "час"}:
        return _as_utc(now_utc) + timedelta(hours=1)
    if normalized in {"evening", "вечером", "вечер"}:
        return _next_local_clock_target(_as_utc(now_utc), timezone_name, time(20, 0))
    if normalized in {"tomorrow", "завтра"}:
        local_now = from_utc_to_user(_as_utc(now_utc), timezone_name)
        tomorrow = local_now.date() + timedelta(days=1)
        return to_utc(datetime.combine(tomorrow, time(9, 0)), timezone_name)
    raise ValueError("Неизвестный вариант откладывания")


def parse_custom_datetime(
    raw_value: str,
    *,
    now_local: datetime,
) -> ParsedCustomDatetime | None:
    value = raw_value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M"):
        try:
            return ParsedCustomDatetime(local_dt=datetime.strptime(value, fmt))
        except ValueError:
            continue

    parsed = parse_reminder_input(f"напомни {value} custom", now_local=now_local)
    if parsed is not None and parsed.text == "custom":
        return ParsedCustomDatetime(
            local_dt=parsed.local_dt,
            datetime_semantics=parsed.datetime_semantics,
        )
    return None


def parse_edit_schedule(
    raw_value: str,
    *,
    now_local: datetime,
) -> ParsedEditSchedule | None:
    value = raw_value.strip()
    if value.lower() in {"", "оставить", "без изменений", "без изменения"}:
        return None

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M"):
        try:
            parsed_dt = datetime.strptime(value, fmt)
            return ParsedEditSchedule(local_dt=parsed_dt)
        except ValueError:
            continue

    parsed = parse_reminder_input(f"напомни {value} edited", now_local=now_local)
    if parsed is None or parsed.text != "edited":
        return None
    return ParsedEditSchedule(
        local_dt=parsed.local_dt,
        recurrence_type=parsed.recurrence_type,
        recurrence_interval=parsed.recurrence_interval,
        datetime_semantics=parsed.datetime_semantics,
    )


async def create_reminder(
    user: User,
    local_dt: datetime,
    text: str,
    recurrence_type: str = "none",
    recurrence_interval: int = 1,
    datetime_semantics: DatetimeSemantics = "wall_clock",
) -> Reminder:
    validate_recurrence(recurrence_type, recurrence_interval)
    if not text.strip() or len(text) > MAX_REMINDER_TEXT_LENGTH:
        raise ValueError("Текст напоминания должен содержать от 1 до 4096 символов")

    remind_at_utc = resolve_schedule_datetime(
        local_dt,
        user.timezone,
        semantics=datetime_semantics,
    )
    now_utc = utc_now()
    recurrence_day_of_month = (
        local_dt.day if recurrence_type == RecurrenceType.MONTHLY.value else None
    )

    if recurrence_type == RecurrenceType.NONE.value and remind_at_utc <= now_utc:
        raise ValueError("Время напоминания уже прошло")

    if recurrence_type != RecurrenceType.NONE.value:
        while remind_at_utc <= now_utc:
            next_dt = calculate_next_occurrence(
                remind_at_utc,
                recurrence_type,
                recurrence_interval,
                timezone_name=user.timezone,
                recurrence_day_of_month=recurrence_day_of_month,
            )
            if next_dt is None:
                break
            remind_at_utc = next_dt

    async with SessionLocal() as session:
        reminder = Reminder(
            user_id=user.id,
            chat_id=user.chat_id,
            text=text.strip(),
            remind_at_utc=remind_at_utc,
            status="pending",
            state=ReminderState.SCHEDULED.value,
            action_revision=0,
            schedule_timezone=user.timezone,
            delivery_at_utc=remind_at_utc,
            recurrence_type=recurrence_type,
            recurrence_interval=recurrence_interval,
            recurrence_day_of_month=recurrence_day_of_month,
        )
        session.add(reminder)
        await session.commit()
        await session.refresh(reminder)

        logger.info(
            "Created reminder",
            extra={
                "extra_data": (
                    f"reminder_id={reminder.id} user_id={user.id} "
                    f"remind_at_utc={reminder.remind_at_utc.isoformat()} "
                    f"recurrence_type={reminder.recurrence_type} "
                    f"recurrence_interval={reminder.recurrence_interval}"
                )
            },
        )
        return reminder


async def list_active_reminders(user: User) -> list[Reminder]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Reminder)
            .where(
                Reminder.user_id == user.id,
                Reminder.chat_id == user.chat_id,
                Reminder.state.in_(ACTIVE_STATES),
                Reminder.status != "processing",
            )
            .order_by(func.coalesce(Reminder.delivery_at_utc, Reminder.remind_at_utc).asc())
        )
        return list(result.scalars().all())


async def list_pending_reminders(user: User) -> list[Reminder]:
    """Compatibility name retained for the actionable active reminder list."""

    return await list_active_reminders(user)


async def get_owned_reminder(user: User, reminder_id: int) -> Reminder | None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Reminder)
            .where(
                Reminder.id == reminder_id,
                Reminder.user_id == user.id,
                Reminder.chat_id == user.chat_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()


async def get_latest_occurrence(user: User, reminder_id: int) -> ReminderOccurrence | None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(ReminderOccurrence)
            .join(Reminder, Reminder.id == ReminderOccurrence.reminder_id)
            .where(
                ReminderOccurrence.reminder_id == reminder_id,
                Reminder.user_id == user.id,
                Reminder.chat_id == user.chat_id,
            )
            .order_by(ReminderOccurrence.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def get_owned_occurrence_target(
    user: User,
    occurrence_id: int,
) -> tuple[int, datetime, int | None] | None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(
                ReminderOccurrence.reminder_id,
                ReminderOccurrence.occurrence_at_utc,
                ReminderOccurrence.message_id,
            )
            .join(Reminder, Reminder.id == ReminderOccurrence.reminder_id)
            .where(
                ReminderOccurrence.id == occurrence_id,
                Reminder.user_id == user.id,
                Reminder.chat_id == user.chat_id,
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        return int(row[0]), row[1], row[2]


async def get_processing_occurrence_id(reminder_id: int, lease_token: str) -> int | None:
    if not lease_token:
        return None
    async with SessionLocal() as session:
        result = await session.execute(
            select(ReminderOccurrence.id)
            .join(Reminder, Reminder.id == ReminderOccurrence.reminder_id)
            .where(
                Reminder.id == reminder_id,
                Reminder.status == "processing",
                Reminder.state.in_((ReminderState.SCHEDULED.value, ReminderState.SNOOZED.value)),
                Reminder.lease_token == lease_token,
                ReminderOccurrence.status == OccurrenceState.PROCESSING.value,
                ReminderOccurrence.action_revision == Reminder.action_revision,
            )
            .order_by(ReminderOccurrence.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def prepare_delivery_occurrence(
    reminder_id: int,
    lease_token: str,
    *,
    now_utc: datetime | None = None,
) -> int | None:
    """Create or reopen the occurrence owned by a valid worker lease."""

    if not lease_token:
        return None
    current_time = _as_utc(now_utc or utc_now())
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
            return None

        result = await session.execute(
            select(ReminderOccurrence)
            .where(
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.occurrence_at_utc == reminder.remind_at_utc,
            )
            .with_for_update()
        )
        occurrence: ReminderOccurrence | None = cast(
            ReminderOccurrence | None, result.scalar_one_or_none()
        )
        if occurrence is None:
            occurrence = ReminderOccurrence(
                reminder_id=reminder.id,
                occurrence_at_utc=reminder.remind_at_utc,
                delivery_at_utc=delivery_at_utc(reminder),
                status=OccurrenceState.PROCESSING.value,
                action_revision=reminder.action_revision,
            )
            session.add(occurrence)
        else:
            occurrence.delivery_at_utc = delivery_at_utc(reminder)
            occurrence.status = OccurrenceState.PROCESSING.value
            occurrence.action_revision = reminder.action_revision
            occurrence.message_id = None
            occurrence.delivered_at = None
            occurrence.snoozed_until_utc = reminder.snoozed_until_utc
        await session.flush()
        return occurrence.id


async def set_last_message_id(
    reminder_id: int,
    message_id: int | None,
    *,
    lease_token: str,
    occurrence_at_utc: datetime,
    now_utc: datetime | None = None,
) -> bool:
    """Persist message and occurrence identity under the exact worker lease."""

    if not lease_token:
        return False
    current_time = _as_utc(now_utc or utc_now())
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

        result = await session.execute(
            select(ReminderOccurrence)
            .where(
                ReminderOccurrence.reminder_id == reminder_id,
                ReminderOccurrence.occurrence_at_utc == occurrence_at_utc,
            )
            .with_for_update()
        )
        occurrence: ReminderOccurrence | None = cast(
            ReminderOccurrence | None, result.scalar_one_or_none()
        )
        if occurrence is None:
            occurrence = ReminderOccurrence(
                reminder_id=reminder_id,
                occurrence_at_utc=occurrence_at_utc,
                delivery_at_utc=delivery_at_utc(reminder),
                status=OccurrenceState.DELIVERED.value,
                action_revision=reminder.action_revision,
            )
            session.add(occurrence)
        occurrence.status = OccurrenceState.DELIVERED.value
        occurrence.action_revision = reminder.action_revision
        occurrence.message_id = message_id
        occurrence.delivered_at = current_time
        occurrence.snoozed_until_utc = None
        reminder.last_message_id = message_id
        reminder.last_delivery_occurrence_utc = occurrence_at_utc
        await session.flush()
        return True


async def _load_owned_reminder(session: Any, user: User, reminder_id: int) -> Reminder | None:
    result = await session.execute(
        select(Reminder)
        .where(
            Reminder.id == reminder_id,
            Reminder.user_id == user.id,
            Reminder.chat_id == user.chat_id,
        )
        .with_for_update()
    )
    return cast(Reminder | None, result.scalar_one_or_none())


async def _load_occurrence(
    session: Any,
    reminder_id: int,
    occurrence_id: int,
) -> ReminderOccurrence | None:
    result = await session.execute(
        select(ReminderOccurrence)
        .where(
            ReminderOccurrence.id == occurrence_id,
            ReminderOccurrence.reminder_id == reminder_id,
        )
        .with_for_update()
    )
    return cast(ReminderOccurrence | None, result.scalar_one_or_none())


async def _legacy_occurrence(
    session: Any,
    reminder: Reminder,
    expected_message_id: int,
) -> ReminderOccurrence | None:
    result = await session.execute(
        select(ReminderOccurrence)
        .where(
            ReminderOccurrence.reminder_id == reminder.id,
            ReminderOccurrence.message_id == expected_message_id,
        )
        .with_for_update()
    )
    occurrence: ReminderOccurrence | None = cast(
        ReminderOccurrence | None, result.scalar_one_or_none()
    )
    if occurrence is not None:
        return occurrence
    if (
        reminder.last_message_id != expected_message_id
        or reminder.last_delivery_occurrence_utc is None
    ):
        return None
    result = await session.execute(
        select(ReminderOccurrence)
        .where(
            ReminderOccurrence.reminder_id == reminder.id,
            ReminderOccurrence.occurrence_at_utc == reminder.last_delivery_occurrence_utc,
        )
        .with_for_update()
    )
    occurrence = cast(ReminderOccurrence | None, result.scalar_one_or_none())
    if occurrence is not None:
        occurrence.message_id = expected_message_id
        occurrence.action_revision = reminder.action_revision
        occurrence.status = OccurrenceState.DELIVERED.value
        return occurrence
    occurrence = ReminderOccurrence(
        reminder_id=reminder.id,
        occurrence_at_utc=reminder.last_delivery_occurrence_utc,
        delivery_at_utc=reminder.last_delivery_occurrence_utc,
        status=OccurrenceState.DELIVERED.value,
        action_revision=reminder.action_revision,
        message_id=expected_message_id,
        delivered_at=reminder.sent_at,
    )
    session.add(occurrence)
    await session.flush()
    return occurrence


def _occurrence_is_current(
    reminder: Reminder,
    occurrence: ReminderOccurrence,
    *,
    expected_revision: int,
    expected_message_id: int | None,
) -> bool:
    return (
        occurrence.action_revision == expected_revision
        and occurrence.status == OccurrenceState.DELIVERED.value
        and (expected_message_id is None or occurrence.message_id == expected_message_id)
        and (expected_message_id is None or reminder.last_message_id == expected_message_id)
        and (
            reminder.last_delivery_occurrence_utc is not None
            and _as_utc(reminder.last_delivery_occurrence_utc)
            == _as_utc(occurrence.occurrence_at_utc)
        )
        and reminder.state
        not in {
            ReminderState.COMPLETED.value,
            ReminderState.CANCELLED.value,
            ReminderState.FAILED.value,
        }
        and reminder.status != "processing"
    )


async def validate_action_target(
    user: User,
    reminder_id: int,
    *,
    action: str,
    expected_revision: int,
    expected_occurrence_id: int | None = None,
    expected_occurrence_at_utc: datetime | None = None,
    expected_message_id: int | None = None,
) -> bool:
    """Validate a non-mutating action step under the same row locks as writes."""

    async with SessionLocal() as session, session.begin():
        reminder = await _load_owned_reminder(session, user, reminder_id)
        if reminder is None:
            _invalid_action(action, reminder_id=reminder_id, reason="owner")
            return False
        if expected_occurrence_id is not None:
            occurrence = await _load_occurrence(session, reminder.id, expected_occurrence_id)
            if occurrence is None or not _occurrence_is_current(
                reminder,
                occurrence,
                expected_revision=expected_revision,
                expected_message_id=expected_message_id,
            ):
                _invalid_action(
                    action, reminder_id=reminder_id, revision=expected_revision, reason="stale"
                )
                return False
            if expected_occurrence_at_utc is not None and _as_utc(
                occurrence.occurrence_at_utc
            ) != _as_utc(expected_occurrence_at_utc):
                _invalid_action(
                    action, reminder_id=reminder_id, revision=expected_revision, reason="stale"
                )
                return False
            return True
        if reminder.action_revision != expected_revision:
            _invalid_action(
                action, reminder_id=reminder_id, revision=expected_revision, reason="stale"
            )
            return False
        if (
            reminder.state
            not in {
                ReminderState.SCHEDULED.value,
                ReminderState.SNOOZED.value,
            }
            or reminder.status == "processing"
        ):
            _invalid_action(
                action, reminder_id=reminder_id, revision=expected_revision, reason="state"
            )
            return False
        return True


def _clear_delivery_identity(reminder: Reminder) -> None:
    reminder.last_message_id = None
    reminder.last_delivery_occurrence_utc = None


def _reset_delivery_retry(reminder: Reminder) -> None:
    reminder.retry_count = 0
    reminder.attempt_count = 0
    reminder.next_retry_at = None
    reminder.error_text = None


async def _cancel_children(session: Any, parent_id: int, now_utc: datetime) -> None:
    result = await session.execute(
        select(Reminder)
        .where(
            Reminder.parent_reminder_id == parent_id,
            Reminder.state.not_in(
                (
                    ReminderState.COMPLETED.value,
                    ReminderState.CANCELLED.value,
                )
            ),
        )
        .with_for_update()
    )
    for child in result.scalars():
        child.state = ReminderState.CANCELLED.value
        child.cancelled_at = now_utc
        child.action_revision += 1
        if child.status != "processing":
            child.status = "sent"
            child.delivery_at_utc = None
            child.snoozed_until_utc = None


async def _cancel_snoozed_sources(session: Any, parent_id: int, now_utc: datetime) -> None:
    result = await session.execute(
        select(ReminderOccurrence)
        .where(
            ReminderOccurrence.reminder_id == parent_id,
            ReminderOccurrence.status == OccurrenceState.SNOOZED.value,
        )
        .with_for_update()
    )
    for occurrence in result.scalars():
        occurrence.status = OccurrenceState.CANCELLED.value
        occurrence.cancelled_at = now_utc
        occurrence.action_revision += 1


async def delete_reminder_any_status(
    user: User,
    reminder_id: int,
    *,
    expected_message_id: int | None = None,
) -> bool:
    """Deprecated compatibility entry point; Issue #4 uses soft cancellation."""

    return await cancel_reminder(
        user=user,
        reminder_id=reminder_id,
        expected_message_id=expected_message_id,
    )


async def cancel_reminder(
    user: User,
    reminder_id: int,
    *,
    expected_message_id: int | None = None,
    expected_revision: int | None = None,
    expected_occurrence_id: int | None = None,
    expected_occurrence_at_utc: datetime | None = None,
) -> bool:
    async with SessionLocal() as session, session.begin():
        reminder = await _load_owned_reminder(session, user, reminder_id)
        if reminder is None:
            _invalid_action("delete", reminder_id=reminder_id, reason="owner")
            return False

        occurrence: ReminderOccurrence | None = None
        if expected_occurrence_id is not None:
            occurrence = await _load_occurrence(session, reminder.id, expected_occurrence_id)
            if occurrence is None or expected_revision is None:
                _invalid_action("delete", reminder_id=reminder_id, reason="stale")
                return False
            if not _occurrence_is_current(
                reminder,
                occurrence,
                expected_revision=expected_revision,
                expected_message_id=expected_message_id,
            ):
                _invalid_action(
                    "delete", reminder_id=reminder_id, revision=expected_revision, reason="stale"
                )
                return False
            if expected_occurrence_at_utc is not None and _as_utc(
                occurrence.occurrence_at_utc
            ) != _as_utc(expected_occurrence_at_utc):
                _invalid_action(
                    "delete", reminder_id=reminder_id, revision=expected_revision, reason="stale"
                )
                return False
        elif expected_message_id is not None:
            occurrence = await _legacy_occurrence(session, reminder, expected_message_id)
            if occurrence is None or not _occurrence_is_current(
                reminder,
                occurrence,
                expected_revision=occurrence.action_revision,
                expected_message_id=expected_message_id,
            ):
                _invalid_action("delete", reminder_id=reminder_id, reason="stale")
                return False
        elif expected_revision is not None and reminder.action_revision != expected_revision:
            _invalid_action(
                "delete", reminder_id=reminder_id, revision=expected_revision, reason="stale"
            )
            return False

        if occurrence is None and reminder.last_delivery_occurrence_utc is not None:
            occurrence = await session.scalar(
                select(ReminderOccurrence)
                .where(
                    ReminderOccurrence.reminder_id == reminder.id,
                    ReminderOccurrence.occurrence_at_utc == reminder.last_delivery_occurrence_utc,
                )
                .with_for_update()
            )
            if occurrence is None:
                occurrence = ReminderOccurrence(
                    reminder_id=reminder.id,
                    occurrence_at_utc=reminder.last_delivery_occurrence_utc,
                    delivery_at_utc=reminder.last_delivery_occurrence_utc,
                    status=OccurrenceState.DELIVERED.value,
                    action_revision=reminder.action_revision,
                    message_id=reminder.last_message_id,
                    delivered_at=reminder.sent_at,
                )
                session.add(occurrence)
                await session.flush()
            if occurrence.status != OccurrenceState.DELIVERED.value:
                occurrence = None

        if reminder.state in {
            ReminderState.COMPLETED.value,
            ReminderState.CANCELLED.value,
        }:
            _invalid_action("delete", reminder_id=reminder_id, reason="state")
            return False

        now_utc = utc_now()
        reminder.state = ReminderState.CANCELLED.value
        reminder.cancelled_at = now_utc
        reminder.action_revision += 1
        if occurrence is not None:
            occurrence.status = OccurrenceState.CANCELLED.value
            occurrence.cancelled_at = now_utc
            occurrence.action_revision += 1

        if reminder.status != "processing":
            reminder.status = "sent"
            reminder.delivery_at_utc = None
            reminder.snoozed_until_utc = None
            _reset_delivery_retry(reminder)

        if reminder.parent_reminder_id is None:
            await _cancel_children(session, reminder.id, now_utc)
            await _cancel_snoozed_sources(session, reminder.id, now_utc)
        elif reminder.source_occurrence_at_utc is not None:
            parent = await _load_owned_reminder(session, user, reminder.parent_reminder_id)
            if parent is not None:
                source = await session.scalar(
                    select(ReminderOccurrence)
                    .where(
                        ReminderOccurrence.reminder_id == parent.id,
                        ReminderOccurrence.occurrence_at_utc == reminder.source_occurrence_at_utc,
                    )
                    .with_for_update()
                )
                if source is not None and source.status == OccurrenceState.SNOOZED.value:
                    source.status = OccurrenceState.CANCELLED.value
                    source.cancelled_at = now_utc
                    source.action_revision += 1

        _record_action("cancelled", action="delete", reminder_id=reminder.id)
        _record_action("action_success", action="delete", reminder_id=reminder.id)
        return True


async def snooze_reminder(
    user: User,
    reminder_id: int,
    minutes: int = 10,
    *,
    expected_message_id: int | None = None,
    expected_revision: int | None = None,
    expected_occurrence_id: int | None = None,
    expected_occurrence_at_utc: datetime | None = None,
    target_at_utc: datetime | None = None,
) -> Reminder | None:
    if minutes < 1:
        raise ValueError("Время откладывания должно быть больше 0 минут")

    now_utc = utc_now()
    snoozed_until_utc = (
        _as_utc(target_at_utc)
        if target_at_utc is not None
        else now_utc + timedelta(minutes=minutes)
    )
    if snoozed_until_utc <= now_utc:
        raise ValueError("Время откладывания должно быть в будущем")

    async with SessionLocal() as session, session.begin():
        reminder = await _load_owned_reminder(session, user, reminder_id)
        if reminder is None:
            _invalid_action("snooze", reminder_id=reminder_id, reason="owner")
            return None

        occurrence: ReminderOccurrence | None = None
        if expected_occurrence_id is not None:
            if expected_revision is None:
                return None
            occurrence = await _load_occurrence(session, reminder.id, expected_occurrence_id)
            if occurrence is None or not _occurrence_is_current(
                reminder,
                occurrence,
                expected_revision=expected_revision,
                expected_message_id=expected_message_id,
            ):
                _invalid_action(
                    "snooze", reminder_id=reminder_id, revision=expected_revision, reason="stale"
                )
                return None
            if expected_occurrence_at_utc is not None and _as_utc(
                occurrence.occurrence_at_utc
            ) != _as_utc(expected_occurrence_at_utc):
                _invalid_action(
                    "snooze", reminder_id=reminder_id, revision=expected_revision, reason="stale"
                )
                return None
        elif expected_message_id is not None:
            occurrence = await _legacy_occurrence(session, reminder, expected_message_id)
            if occurrence is None or not _occurrence_is_current(
                reminder,
                occurrence,
                expected_revision=occurrence.action_revision,
                expected_message_id=expected_message_id,
            ):
                _invalid_action("snooze", reminder_id=reminder_id, reason="stale")
                return None
        else:
            if expected_revision is not None and reminder.action_revision != expected_revision:
                _invalid_action(
                    "snooze", reminder_id=reminder_id, revision=expected_revision, reason="stale"
                )
                return None
            if (
                reminder.state
                not in {
                    ReminderState.SCHEDULED.value,
                    ReminderState.SNOOZED.value,
                }
                or reminder.status == "processing"
            ):
                _invalid_action("snooze", reminder_id=reminder_id, reason="state")
                return None

        if occurrence is not None:
            occurrence.status = OccurrenceState.SNOOZED.value
            occurrence.snoozed_until_utc = snoozed_until_utc
            occurrence.action_revision += 1

        if reminder.parent_reminder_id is not None:
            reminder.state = ReminderState.SNOOZED.value
            reminder.status = "pending"
            reminder.delivery_at_utc = snoozed_until_utc
            reminder.snoozed_until_utc = snoozed_until_utc
            reminder.action_revision += 1
            _reset_delivery_retry(reminder)
            _clear_delivery_identity(reminder)
            _record_action("snoozed", action="snooze", reminder_id=reminder.id)
            _record_action("action_success", action="snooze", reminder_id=reminder.id)
            return reminder

        if occurrence is not None and reminder.recurrence_type != RecurrenceType.NONE.value:
            existing = await session.scalar(
                select(Reminder)
                .where(
                    Reminder.parent_reminder_id == reminder.id,
                    Reminder.source_occurrence_at_utc == occurrence.occurrence_at_utc,
                )
                .with_for_update()
            )
            if existing is None:
                existing = Reminder(
                    user_id=reminder.user_id,
                    chat_id=reminder.chat_id,
                    text=reminder.text,
                    remind_at_utc=snoozed_until_utc,
                    schedule_timezone=reminder.schedule_timezone,
                    delivery_at_utc=snoozed_until_utc,
                    snoozed_until_utc=snoozed_until_utc,
                    status="pending",
                    state=ReminderState.SNOOZED.value,
                    action_revision=0,
                    recurrence_type=RecurrenceType.NONE.value,
                    recurrence_interval=1,
                    parent_reminder_id=reminder.id,
                    source_occurrence_at_utc=occurrence.occurrence_at_utc,
                )
                session.add(existing)
            else:
                existing.text = reminder.text
                existing.remind_at_utc = snoozed_until_utc
                existing.delivery_at_utc = snoozed_until_utc
                existing.snoozed_until_utc = snoozed_until_utc
                existing.status = "pending"
                existing.state = ReminderState.SNOOZED.value
                existing.action_revision += 1
                _reset_delivery_retry(existing)
                _clear_delivery_identity(existing)
            reminder.action_revision += 1
            _clear_delivery_identity(reminder)
            await session.flush()
            _record_action("snoozed", action="snooze", reminder_id=reminder.id)
            _record_action("action_success", action="snooze", reminder_id=reminder.id)
            return existing

        reminder.state = ReminderState.SNOOZED.value
        reminder.status = "pending"
        reminder.delivery_at_utc = snoozed_until_utc
        reminder.snoozed_until_utc = snoozed_until_utc
        reminder.action_revision += 1
        _reset_delivery_retry(reminder)
        _clear_delivery_identity(reminder)
        _record_action("snoozed", action="snooze", reminder_id=reminder.id)
        _record_action("action_success", action="snooze", reminder_id=reminder.id)
        return reminder


async def complete_reminder(
    user: User,
    reminder_id: int,
    *,
    expected_revision: int,
    expected_occurrence_id: int,
    expected_message_id: int | None = None,
) -> bool:
    async with SessionLocal() as session, session.begin():
        reminder = await _load_owned_reminder(session, user, reminder_id)
        occurrence = (
            await _load_occurrence(session, reminder_id, expected_occurrence_id)
            if reminder is not None
            else None
        )
        if reminder is None or occurrence is None:
            _invalid_action(
                "done", reminder_id=reminder_id, revision=expected_revision, reason="owner"
            )
            return False
        if not _occurrence_is_current(
            reminder,
            occurrence,
            expected_revision=expected_revision,
            expected_message_id=expected_message_id,
        ):
            _invalid_action(
                "done", reminder_id=reminder_id, revision=expected_revision, reason="stale"
            )
            return False

        now_utc = utc_now()
        occurrence.status = OccurrenceState.COMPLETED.value
        occurrence.completed_at = now_utc
        occurrence.action_revision += 1
        reminder.action_revision += 1

        if reminder.parent_reminder_id is None:
            if reminder.recurrence_type == RecurrenceType.NONE.value:
                reminder.state = ReminderState.COMPLETED.value
                reminder.completed_at = now_utc
            else:
                reminder.state = ReminderState.SCHEDULED.value
        else:
            reminder.state = ReminderState.COMPLETED.value
            reminder.completed_at = now_utc
            reminder.status = "sent"
            reminder.delivery_at_utc = None
            reminder.snoozed_until_utc = None
            parent = await _load_owned_reminder(session, user, reminder.parent_reminder_id)
            if parent is not None and reminder.source_occurrence_at_utc is not None:
                source = await session.scalar(
                    select(ReminderOccurrence)
                    .where(
                        ReminderOccurrence.reminder_id == parent.id,
                        ReminderOccurrence.occurrence_at_utc == reminder.source_occurrence_at_utc,
                    )
                    .with_for_update()
                )
                if source is not None and source.status != OccurrenceState.COMPLETED.value:
                    source.status = OccurrenceState.COMPLETED.value
                    source.completed_at = now_utc
                    source.action_revision += 1
                parent.action_revision += 1

        _record_action("completed", action="done", reminder_id=reminder.id)
        _record_action("action_success", action="done", reminder_id=reminder.id)
        return True


async def pause_reminder(
    user: User,
    reminder_id: int,
    *,
    expected_revision: int,
    expected_occurrence_id: int | None = None,
    expected_occurrence_at_utc: datetime | None = None,
    expected_message_id: int | None = None,
) -> bool:
    async with SessionLocal() as session, session.begin():
        reminder = await _load_owned_reminder(session, user, reminder_id)
        if reminder is None:
            _invalid_action("pause", reminder_id=reminder_id, reason="owner")
            return False

        occurrence = None
        if expected_occurrence_id is not None:
            occurrence = await _load_occurrence(session, reminder.id, expected_occurrence_id)
            if occurrence is None or not _occurrence_is_current(
                reminder,
                occurrence,
                expected_revision=expected_revision,
                expected_message_id=expected_message_id,
            ):
                _invalid_action("pause", reminder_id=reminder_id, reason="stale")
                return False
            if expected_occurrence_at_utc is not None and _as_utc(
                occurrence.occurrence_at_utc
            ) != _as_utc(expected_occurrence_at_utc):
                _invalid_action("pause", reminder_id=reminder_id, reason="stale")
                return False
            occurrence.action_revision += 1
        elif reminder.action_revision != expected_revision:
            _invalid_action("pause", reminder_id=reminder_id, reason="stale")
            return False

        if (
            reminder.recurrence_type == RecurrenceType.NONE.value
            or reminder.state in {ReminderState.CANCELLED.value, ReminderState.COMPLETED.value}
            or reminder.status == "processing"
        ):
            _invalid_action("pause", reminder_id=reminder_id, reason="state")
            return False

        reminder.state = ReminderState.PAUSED.value
        reminder.paused_at = utc_now()
        reminder.action_revision += 1
        reminder.delivery_at_utc = reminder.remind_at_utc
        reminder.snoozed_until_utc = None
        _record_action("paused", action="pause", reminder_id=reminder.id)
        _record_action("action_success", action="pause", reminder_id=reminder.id)
        return True


async def resume_reminder(user: User, reminder_id: int, *, expected_revision: int) -> bool:
    async with SessionLocal() as session, session.begin():
        reminder = await _load_owned_reminder(session, user, reminder_id)
        if reminder is None:
            _invalid_action("resume", reminder_id=reminder_id, reason="owner")
            return False
        if reminder.action_revision != expected_revision:
            _invalid_action("resume", reminder_id=reminder_id, reason="stale")
            return False
        if (
            reminder.recurrence_type == RecurrenceType.NONE.value
            or reminder.state != ReminderState.PAUSED.value
            or reminder.status == "processing"
        ):
            _invalid_action("resume", reminder_id=reminder_id, reason="state")
            return False

        now_utc = utc_now()
        next_occurrence = advance_occurrence_until_future(
            reminder.remind_at_utc,
            reminder.recurrence_type,
            reminder.recurrence_interval,
            reminder.schedule_timezone,
            reminder.recurrence_day_of_month,
            now_utc,
        )
        if next_occurrence is None:
            _invalid_action("resume", reminder_id=reminder_id, reason="schedule")
            return False
        reminder.remind_at_utc = next_occurrence
        reminder.delivery_at_utc = next_occurrence
        reminder.snoozed_until_utc = None
        reminder.state = ReminderState.SCHEDULED.value
        reminder.status = "pending"
        reminder.paused_at = None
        reminder.action_revision += 1
        _reset_delivery_retry(reminder)
        _clear_delivery_identity(reminder)
        _record_action("resumed", action="resume", reminder_id=reminder.id)
        _record_action("action_success", action="resume", reminder_id=reminder.id)
        return True


async def edit_reminder(
    user: User,
    reminder_id: int,
    *,
    expected_revision: int,
    text: str | None = None,
    local_dt: datetime | None = None,
    recurrence_type: str | None = None,
    recurrence_interval: int | None = None,
    datetime_semantics: DatetimeSemantics = "wall_clock",
    expected_occurrence_id: int | None = None,
    expected_occurrence_at_utc: datetime | None = None,
    expected_message_id: int | None = None,
) -> Reminder | None:
    if text is not None and (not text.strip() or len(text) > MAX_REMINDER_TEXT_LENGTH):
        raise ValueError("Текст напоминания должен содержать от 1 до 4096 символов")

    async with SessionLocal() as session, session.begin():
        reminder = await _load_owned_reminder(session, user, reminder_id)
        if reminder is None:
            _record_action("edit_failure", action="edit", reminder_id=reminder_id, reason="owner")
            return None
        if reminder.action_revision != expected_revision:
            _record_action("edit_failure", action="edit", reminder_id=reminder_id, reason="stale")
            return None
        occurrence: ReminderOccurrence | None = None
        if expected_occurrence_id is not None:
            occurrence = await _load_occurrence(session, reminder.id, expected_occurrence_id)
            if occurrence is None or not _occurrence_is_current(
                reminder,
                occurrence,
                expected_revision=expected_revision,
                expected_message_id=expected_message_id,
            ):
                _record_action(
                    "edit_failure", action="edit", reminder_id=reminder_id, reason="stale"
                )
                return None
            if expected_occurrence_at_utc is not None and _as_utc(
                occurrence.occurrence_at_utc
            ) != _as_utc(expected_occurrence_at_utc):
                _record_action(
                    "edit_failure", action="edit", reminder_id=reminder_id, reason="stale"
                )
                return None
        if (
            reminder.state
            in {
                ReminderState.COMPLETED.value,
                ReminderState.CANCELLED.value,
                ReminderState.FAILED.value,
            }
            or reminder.status == "processing"
        ):
            _record_action("edit_failure", action="edit", reminder_id=reminder_id, reason="state")
            return None

        previous_state = reminder.state
        if text is not None:
            reminder.text = text.strip()

        schedule_changed = local_dt is not None or recurrence_type is not None
        if schedule_changed:
            new_recurrence = recurrence_type or RecurrenceType.NONE.value
            new_interval = recurrence_interval or 1
            validate_recurrence(new_recurrence, new_interval)
            if local_dt is None:
                raise ValueError("Для изменения расписания укажи дату и время")
            schedule_timezone = user.timezone
            remind_at_utc = resolve_schedule_datetime(
                local_dt,
                schedule_timezone,
                semantics=datetime_semantics,
            )
            now_utc = utc_now()
            day_of_month = local_dt.day if new_recurrence == RecurrenceType.MONTHLY.value else None
            if new_recurrence == RecurrenceType.NONE.value:
                if remind_at_utc <= now_utc:
                    raise ValueError("Время напоминания уже прошло")
            else:
                while remind_at_utc <= now_utc:
                    next_dt = calculate_next_occurrence(
                        remind_at_utc,
                        new_recurrence,
                        new_interval,
                        timezone_name=schedule_timezone,
                        recurrence_day_of_month=day_of_month,
                    )
                    if next_dt is None:
                        break
                    remind_at_utc = next_dt
            reminder.schedule_timezone = schedule_timezone
            reminder.remind_at_utc = remind_at_utc
            reminder.delivery_at_utc = remind_at_utc
            reminder.recurrence_type = new_recurrence
            reminder.recurrence_interval = new_interval
            reminder.recurrence_day_of_month = day_of_month

        if schedule_changed:
            reminder.state = ReminderState.SCHEDULED.value
            reminder.status = "pending"
            reminder.snoozed_until_utc = None
            reminder.paused_at = None
        elif previous_state == ReminderState.DELIVERED.value:
            reminder.status = "sent"
        else:
            reminder.status = "pending"
        reminder.action_revision += 1
        reminder.completed_at = None
        _reset_delivery_retry(reminder)
        if schedule_changed:
            _clear_delivery_identity(reminder)
        elif occurrence is not None:
            occurrence.action_revision = reminder.action_revision
        _record_action("edit_success", action="edit", reminder_id=reminder.id)
        _record_action("action_success", action="edit", reminder_id=reminder.id)
        return reminder


def _payload_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _payload_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def create_action_draft(
    user: User,
    reminder_id: int,
    *,
    action_type: str,
    expected_action_revision: int,
    expected_occurrence_at_utc: datetime | None = None,
    expected_message_id: int | None = None,
    expected_occurrence_id: int | None = None,
    current_step: str,
    payload: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
    expires_at: datetime | None = None,
) -> ActionDraft | None:
    current_time = _as_utc(now_utc or utc_now())
    expiry = _as_utc(expires_at or (current_time + FLOW_TTL))
    async with SessionLocal() as session, session.begin():
        owner = await session.scalar(
            select(User).where(User.id == user.id, User.chat_id == user.chat_id).with_for_update()
        )
        if owner is None:
            _invalid_action("draft", reminder_id=reminder_id, reason="owner")
            return None
        reminder = await _load_owned_reminder(session, user, reminder_id)
        if reminder is None or reminder.action_revision != expected_action_revision:
            _invalid_action("draft", reminder_id=reminder_id, reason="stale")
            return None
        if expected_occurrence_id is not None:
            occurrence = await _load_occurrence(session, reminder.id, expected_occurrence_id)
            if occurrence is None or not _occurrence_is_current(
                reminder,
                occurrence,
                expected_revision=expected_action_revision,
                expected_message_id=expected_message_id,
            ):
                _invalid_action(
                    "draft",
                    reminder_id=reminder_id,
                    revision=expected_action_revision,
                    reason="stale",
                )
                return None
            if expected_occurrence_at_utc is not None and _as_utc(
                occurrence.occurrence_at_utc
            ) != _as_utc(expected_occurrence_at_utc):
                _invalid_action(
                    "draft",
                    reminder_id=reminder_id,
                    revision=expected_action_revision,
                    reason="stale",
                )
                return None
        draft_payload = dict(payload or {})
        if expected_occurrence_id is not None:
            draft_payload.setdefault("occurrence_id", expected_occurrence_id)
        await session.execute(
            delete(ActionDraft).where(
                ActionDraft.user_id == user.id,
                ActionDraft.chat_id == user.chat_id,
            )
        )
        draft = ActionDraft(
            user_id=user.id,
            chat_id=user.chat_id,
            action_type=action_type,
            reminder_id=reminder_id,
            expected_action_revision=expected_action_revision,
            expected_occurrence_id=expected_occurrence_id,
            expected_occurrence_at_utc=expected_occurrence_at_utc,
            expected_message_id=expected_message_id,
            current_step=current_step,
            payload=_payload_text(draft_payload),
            expires_at=expiry,
        )
        session.add(draft)
        await session.flush()
        return draft


async def get_active_action_draft(
    user: User,
    *,
    action_type: str | None = None,
    now_utc: datetime | None = None,
) -> ActionDraft | None:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        filters = [ActionDraft.user_id == user.id, ActionDraft.chat_id == user.chat_id]
        if action_type is not None:
            filters.append(ActionDraft.action_type == action_type)
        result = await session.execute(
            select(ActionDraft)
            .where(*filters)
            .order_by(ActionDraft.updated_at.desc(), ActionDraft.id.desc())
            .with_for_update()
        )
        drafts = list(result.scalars())
        for draft in drafts:
            if _as_utc(draft.expires_at) <= current_time:
                await session.delete(draft)
                _record_action(
                    "expired_draft", action=draft.action_type, reminder_id=draft.reminder_id
                )
                continue
            return draft
        return None


async def update_action_draft(
    user: User,
    draft_id: int,
    *,
    current_step: str | None = None,
    payload: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
) -> ActionDraft | None:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        result = await session.execute(
            select(ActionDraft)
            .where(
                ActionDraft.id == draft_id,
                ActionDraft.user_id == user.id,
                ActionDraft.chat_id == user.chat_id,
            )
            .with_for_update()
        )
        draft = result.scalar_one_or_none()
        if draft is None:
            return None
        if _as_utc(draft.expires_at) <= current_time:
            await session.delete(draft)
            _record_action("expired_draft", action=draft.action_type, reminder_id=draft.reminder_id)
            return None
        if current_step is not None:
            draft.current_step = current_step
        if payload is not None:
            draft.payload = _payload_text(payload)
        draft.updated_at = current_time
        return draft


async def delete_action_draft(user: User, draft_id: int) -> bool:
    async with SessionLocal() as session, session.begin():
        result = cast(
            CursorResult[Any],
            await session.execute(
                delete(ActionDraft).where(
                    ActionDraft.id == draft_id,
                    ActionDraft.user_id == user.id,
                    ActionDraft.chat_id == user.chat_id,
                )
            ),
        )
        return bool(result.rowcount)


async def cancel_active_action_drafts(user: User) -> int:
    async with SessionLocal() as session, session.begin():
        result = cast(
            CursorResult[Any],
            await session.execute(
                delete(ActionDraft).where(
                    ActionDraft.user_id == user.id,
                    ActionDraft.chat_id == user.chat_id,
                )
            ),
        )
        return int(result.rowcount or 0)


async def apply_custom_snooze_draft(
    user: User,
    draft: ActionDraft,
    raw_value: str,
) -> Reminder | None:
    now_utc = utc_now()
    now_local = from_utc_to_user(now_utc, user.timezone)
    parsed_datetime = parse_custom_datetime(raw_value, now_local=now_local)
    if parsed_datetime is None:
        raise ValueError("Не понял дату. Используй: 2026-09-08 18:00 или «завтра в 9»")
    target = resolve_schedule_datetime(
        parsed_datetime.local_dt,
        user.timezone,
        semantics=parsed_datetime.datetime_semantics,
    )
    payload = _payload_dict(draft.payload)
    expected_occurrence_id = draft.expected_occurrence_id
    if expected_occurrence_id is None:
        payload_occurrence_id = payload.get("occurrence_id")
        if isinstance(payload_occurrence_id, int):
            expected_occurrence_id = payload_occurrence_id
    result = await snooze_reminder(
        user,
        draft.reminder_id,
        expected_message_id=draft.expected_message_id,
        expected_revision=draft.expected_action_revision,
        target_at_utc=target,
        expected_occurrence_id=expected_occurrence_id,
        expected_occurrence_at_utc=draft.expected_occurrence_at_utc,
    )
    await delete_action_draft(user, draft.id)
    return result


async def apply_edit_draft(
    user: User,
    draft: ActionDraft,
    *,
    local_dt: datetime | None = None,
    recurrence_type: str | None = None,
    recurrence_interval: int | None = None,
    datetime_semantics: DatetimeSemantics = "wall_clock",
) -> Reminder | None:
    payload = _payload_dict(draft.payload)
    new_text = payload.get("text")
    if not isinstance(new_text, str):
        new_text = None
    expected_occurrence_id = draft.expected_occurrence_id
    if expected_occurrence_id is None:
        payload_occurrence_id = payload.get("occurrence_id")
        if isinstance(payload_occurrence_id, int):
            expected_occurrence_id = payload_occurrence_id
    result = await edit_reminder(
        user,
        draft.reminder_id,
        expected_revision=draft.expected_action_revision,
        text=new_text,
        local_dt=local_dt,
        recurrence_type=recurrence_type,
        recurrence_interval=recurrence_interval,
        datetime_semantics=datetime_semantics,
        expected_occurrence_id=expected_occurrence_id,
        expected_occurrence_at_utc=draft.expected_occurrence_at_utc,
        expected_message_id=draft.expected_message_id,
    )
    await delete_action_draft(user, draft.id)
    return result


async def get_stats() -> dict[str, int]:
    async with SessionLocal() as session:
        total_users = await session.scalar(select(func.count()).select_from(User))
        total_reminders = await session.scalar(select(func.count()).select_from(Reminder))
        pending_reminders = await session.scalar(
            select(func.count())
            .select_from(Reminder)
            .where(Reminder.status == "pending", Reminder.state.in_(ACTIVE_STATES))
        )
        failed_reminders = await session.scalar(
            select(func.count())
            .select_from(Reminder)
            .where(Reminder.state == ReminderState.FAILED.value)
        )
        recurring_reminders = await session.scalar(
            select(func.count())
            .select_from(Reminder)
            .where(
                Reminder.recurrence_type != RecurrenceType.NONE.value,
                Reminder.parent_reminder_id.is_(None),
                Reminder.state.in_(ACTIVE_STATES),
            )
        )
        sent_today = await session.scalar(
            select(func.count())
            .select_from(Reminder)
            .where(
                Reminder.sent_at.is_not(None),
                Reminder.sent_at >= utc_now() - timedelta(days=1),
            )
        )

        return {
            "total_users": total_users or 0,
            "total_reminders": total_reminders or 0,
            "pending_reminders": pending_reminders or 0,
            "failed_reminders": failed_reminders or 0,
            "recurring_reminders": recurring_reminders or 0,
            "sent_last_24h": sent_today or 0,
        }


async def get_failed_reminders(limit: int = 20) -> list[Reminder]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Reminder)
            .where(Reminder.state == ReminderState.FAILED.value)
            .order_by(Reminder.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


def format_recurrence(reminder: Reminder) -> str:
    recurrence_type = str(reminder.recurrence_type)
    interval = int(reminder.recurrence_interval)
    if recurrence_type == RecurrenceType.NONE.value:
        return "нет"
    if recurrence_type == RecurrenceType.MINUTES.value:
        return "каждые 1 минуту" if interval == 1 else f"каждые {interval} минут"
    if recurrence_type == RecurrenceType.HOURLY.value:
        return "каждый час" if interval == 1 else f"каждые {interval} часов"
    if recurrence_type == RecurrenceType.DAILY.value:
        return "каждый день" if interval == 1 else f"каждые {interval} дней"
    if recurrence_type == RecurrenceType.WEEKLY.value:
        return "каждую неделю" if interval == 1 else f"каждые {interval} недель"
    if recurrence_type == RecurrenceType.MONTHLY.value:
        return "каждый месяц" if interval == 1 else f"каждые {interval} месяцев"
    return recurrence_type


def format_state(state: str) -> str:
    return {
        ReminderState.SCHEDULED.value: "запланировано",
        ReminderState.DELIVERED.value: "доставлено",
        ReminderState.COMPLETED.value: "выполнено",
        ReminderState.SNOOZED.value: "отложено",
        ReminderState.PAUSED.value: "на паузе",
        ReminderState.CANCELLED.value: "отменено",
        ReminderState.FAILED.value: "ошибка доставки",
    }.get(state, state)


def format_reminder_for_user(
    reminder: Reminder,
    timezone_name: str,
    *,
    display_state: str | None = None,
    display_at_utc: datetime | None = None,
) -> str:
    local_dt = from_utc_to_user(display_at_utc or delivery_at_utc(reminder), timezone_name)
    return (
        f"ID: {reminder.id}\n"
        f"Состояние: {format_state(display_state or reminder.state)}\n"
        f"Когда: {local_dt.strftime('%d.%m.%Y %H:%M')}\n"
        f"Повтор: {format_recurrence(reminder)}\n"
        f"Текст: {escape(reminder.text)}"
    )
