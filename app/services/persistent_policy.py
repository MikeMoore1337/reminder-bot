from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from app.utils.datetime_utils import from_utc_to_user, to_utc

DEFAULT_PERSISTENT_INTERVAL_MINUTES = 60
DEFAULT_PERSISTENT_MAX_DELIVERIES = 6
DEFAULT_PERSISTENT_MAX_ESCALATIONS = 5
DEFAULT_PERSISTENT_QUIET_HOURS_START = "22:00"
DEFAULT_PERSISTENT_QUIET_HOURS_END = "08:00"
DEFAULT_PERSISTENT_USER_COOLDOWN_MINUTES = 1

MIN_PERSISTENT_INTERVAL_MINUTES = 5
MAX_PERSISTENT_INTERVAL_MINUTES = 24 * 60
MAX_PERSISTENT_DELIVERIES = 100
MAX_PERSISTENT_ESCALATIONS = 99
MAX_PERSISTENT_CLOCK_LENGTH = 5

_CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True, slots=True)
class PersistentPolicy:
    """Bounded persisted policy for one persistent reminder.

    ``max_escalations`` counts repeat deliveries after the initial delivery.
    Therefore the effective delivery bound is the smaller of
    ``max_deliveries`` and ``1 + max_escalations``.
    """

    interval_minutes: int = DEFAULT_PERSISTENT_INTERVAL_MINUTES
    max_deliveries: int = DEFAULT_PERSISTENT_MAX_DELIVERIES
    max_escalations: int = DEFAULT_PERSISTENT_MAX_ESCALATIONS
    quiet_hours_start: str = DEFAULT_PERSISTENT_QUIET_HOURS_START
    quiet_hours_end: str = DEFAULT_PERSISTENT_QUIET_HOURS_END

    def __post_init__(self) -> None:
        if (
            not MIN_PERSISTENT_INTERVAL_MINUTES
            <= self.interval_minutes
            <= (MAX_PERSISTENT_INTERVAL_MINUTES)
        ):
            raise ValueError("Интервал важного напоминания должен быть от 5 минут до 24 часов")
        if not 1 <= self.max_deliveries <= MAX_PERSISTENT_DELIVERIES:
            raise ValueError("Лимит доставок должен быть от 1 до 100")
        if not 0 <= self.max_escalations <= MAX_PERSISTENT_ESCALATIONS:
            raise ValueError("Лимит повторов должен быть от 0 до 99")
        parse_clock(self.quiet_hours_start, field_name="quiet_hours_start")
        parse_clock(self.quiet_hours_end, field_name="quiet_hours_end")

    @property
    def max_effective_deliveries(self) -> int:
        return min(self.max_deliveries, self.max_escalations + 1)

    @property
    def repeat_interval(self) -> timedelta:
        return timedelta(minutes=self.interval_minutes)


def parse_clock(value: str, *, field_name: str = "quiet_hours") -> time:
    if not isinstance(value, str) or len(value) != MAX_PERSISTENT_CLOCK_LENGTH:
        raise ValueError(f"{field_name} должен быть в формате HH:MM")
    if _CLOCK_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} должен быть в формате HH:MM")
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour, minute)


def normalize_reminder_mode(mode: str | object | None) -> str:
    """Return the only persisted mode values accepted by the domain."""

    value = str(mode or "normal").strip().lower().replace("ё", "е")
    if value in {"normal", "обычное", "обычный", "обычная"}:
        return "normal"
    if value in {
        "persistent",
        "important",
        "важное",
        "важный",
        "важная",
        "постоянное",
        "постоянный",
        "постоянная",
    }:
        return "persistent"
    raise ValueError("Неизвестный режим напоминания")


def is_persistent_mode(mode: str | object | None) -> bool:
    try:
        return normalize_reminder_mode(mode) == "persistent"
    except ValueError:
        return False


def is_quiet_hours(
    instant_utc: datetime,
    timezone_name: str,
    *,
    quiet_hours_start: str,
    quiet_hours_end: str,
) -> bool:
    start = parse_clock(quiet_hours_start, field_name="quiet_hours_start")
    end = parse_clock(quiet_hours_end, field_name="quiet_hours_end")
    if start == end:
        return False

    local_time = from_utc_to_user(_as_utc(instant_utc), timezone_name).time().replace(tzinfo=None)
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def next_allowed_delivery(
    candidate_utc: datetime,
    timezone_name: str,
    *,
    quiet_hours_start: str,
    quiet_hours_end: str,
) -> datetime:
    """Move a candidate out of local quiet hours using the DST-safe policy."""

    candidate = _as_utc(candidate_utc)
    start = parse_clock(quiet_hours_start, field_name="quiet_hours_start")
    end = parse_clock(quiet_hours_end, field_name="quiet_hours_end")
    if start == end or not is_quiet_hours(
        candidate,
        timezone_name,
        quiet_hours_start=quiet_hours_start,
        quiet_hours_end=quiet_hours_end,
    ):
        return candidate

    local_candidate = from_utc_to_user(candidate, timezone_name)
    target_date = local_candidate.date()
    if start > end and local_candidate.time().replace(tzinfo=None) >= start:
        target_date += timedelta(days=1)
    return to_utc(datetime.combine(target_date, end), timezone_name)


def next_persistent_delivery(
    delivered_at_utc: datetime,
    timezone_name: str,
    policy: PersistentPolicy,
) -> datetime:
    return next_allowed_delivery(
        _as_utc(delivered_at_utc) + policy.repeat_interval,
        timezone_name,
        quiet_hours_start=policy.quiet_hours_start,
        quiet_hours_end=policy.quiet_hours_end,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
