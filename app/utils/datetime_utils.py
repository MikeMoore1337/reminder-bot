from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DatetimeSemantics = Literal["wall_clock", "instant"]


def validate_timezone(timezone_name: str) -> str:
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Неизвестный часовой пояс: {timezone_name}") from exc
    return timezone_name


def _valid_local_candidates(local_dt: datetime, tz: ZoneInfo) -> list[datetime]:
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = local_dt.replace(tzinfo=tz, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(tz)
        if round_trip.replace(tzinfo=None) == local_dt and all(
            existing.astimezone(UTC) != candidate.astimezone(UTC) for existing in candidates
        ):
            candidates.append(candidate)
    return candidates


def localize_in_timezone(local_dt: datetime, timezone_name: str) -> datetime:
    """Convert a wall-clock datetime using deterministic DST policies.

    Ambiguous local times use the earlier occurrence (fold=0). A nonexistent local
    time is shifted forward to the first valid local time, preserving the intended
    calendar direction across a spring-forward gap.
    """

    tz = ZoneInfo(timezone_name)
    naive_local_dt = local_dt.replace(tzinfo=None)
    candidates = _valid_local_candidates(naive_local_dt, tz)
    if candidates:
        return candidates[0]

    fold_zero = naive_local_dt.replace(tzinfo=tz, fold=0)
    fold_one = naive_local_dt.replace(tzinfo=tz, fold=1)
    offset_zero = fold_zero.utcoffset()
    offset_one = fold_one.utcoffset()
    if offset_zero is not None and offset_one is not None:
        forward_gap = offset_one - offset_zero
        if forward_gap > timedelta(0):
            shifted = naive_local_dt + forward_gap
            shifted_candidates = _valid_local_candidates(shifted, tz)
            if shifted_candidates:
                return shifted_candidates[0]

    # ZoneInfo transitions are normally minute-aligned, but keep a bounded
    # fallback for unusual historical transitions and retain the same policy.
    for minutes in range(1, 24 * 60 + 1):
        shifted = naive_local_dt + timedelta(minutes=minutes)
        shifted_candidates = _valid_local_candidates(shifted, tz)
        if shifted_candidates:
            return shifted_candidates[0]

    raise ValueError(f"Не удалось разрешить локальное время: {local_dt!s} {timezone_name}")


def to_utc(local_dt: datetime, timezone_name: str) -> datetime:
    return localize_in_timezone(local_dt, timezone_name).astimezone(UTC)


def to_utc_instant(instant_dt: datetime) -> datetime:
    if instant_dt.tzinfo is None:
        raise ValueError("Elapsed datetime must be timezone-aware")
    return instant_dt.astimezone(UTC)


def resolve_schedule_datetime(
    local_dt: datetime,
    timezone_name: str,
    semantics: DatetimeSemantics = "wall_clock",
) -> datetime:
    if semantics == "wall_clock":
        return to_utc(local_dt, timezone_name)
    if semantics == "instant":
        return to_utc_instant(local_dt)
    raise ValueError(f"Unsupported datetime semantics: {semantics}")


def from_utc_to_user(dt_utc: datetime, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=UTC)
    return dt_utc.astimezone(tz)


def utc_now() -> datetime:
    return datetime.now(UTC)


def now_in_timezone(timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    return datetime.now(tz)
