from __future__ import annotations

import calendar
import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from app.utils.datetime_utils import from_utc_to_user, to_utc

RULE_VERSION = 1
MAX_RULE_BYTES = 4096
MAX_ADVANCE_STEPS = 10_000
MAX_INTERVAL = 52
MAX_COMPLETION_DELAY_DAYS = 3650

LEGACY_KINDS = {"none", "minutes", "hourly", "daily", "weekly", "monthly"}
ADVANCED_KINDS = {
    "weekly_days",
    "weekdays",
    "monthly_nth",
    "monthly_last",
    "yearly",
    "completion_relative",
}
_TIME_RE = re.compile(r"^(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)$")


class RecurrenceRuleError(ValueError):
    """Raised when a persisted or parsed recurrence rule is not safe to use."""


def _parse_iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise RecurrenceRuleError(f"{field} должен быть датой")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RecurrenceRuleError(f"{field} должен быть датой") from exc


def _optional_until(rule: Mapping[str, Any]) -> date | None:
    value = rule.get("until")
    if value is None:
        return None
    return _parse_iso_date(value, "until")


def _validate_time(value: Any) -> str:
    if not isinstance(value, str) or _TIME_RE.fullmatch(value) is None:
        raise RecurrenceRuleError("Время правила должно быть в формате HH:MM")
    return value


def _validate_interval(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_INTERVAL:
        raise RecurrenceRuleError(f"Интервал правила должен быть от 1 до {MAX_INTERVAL}")
    return value


def _validate_weekday(value: Any, field: str = "weekday") -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 6:
        raise RecurrenceRuleError(f"{field} должен быть числом от 0 до 6")
    return value


def _validate_weekdays(value: Any) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= 7:
        raise RecurrenceRuleError("weekdays должен содержать от 1 до 7 дней")
    if any(not isinstance(item, int) or isinstance(item, bool) for item in value):
        raise RecurrenceRuleError("weekdays должен содержать только числа")
    weekdays = sorted({int(item) for item in value})
    if len(weekdays) != len(value) or any(not 0 <= item <= 6 for item in weekdays):
        raise RecurrenceRuleError("weekdays содержит недопустимый день")
    return weekdays


def _validate_until(rule: Mapping[str, Any], canonical: dict[str, Any]) -> None:
    until = _optional_until(rule)
    if until is not None:
        canonical["until"] = until.isoformat()


def canonicalize_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a bounded, JSON-serializable recurrence rule."""

    if not isinstance(rule, Mapping):
        raise RecurrenceRuleError("Правило повторения должно быть объектом")
    version = rule.get("version", RULE_VERSION)
    if version != RULE_VERSION:
        raise RecurrenceRuleError("Неподдерживаемая версия правила повторения")
    kind = rule.get("kind")
    if not isinstance(kind, str):
        raise RecurrenceRuleError("У правила повторения отсутствует kind")

    canonical: dict[str, Any] = {"version": RULE_VERSION, "kind": kind}
    if kind == "none":
        pass
    elif kind == "legacy":
        recurrence_type = rule.get("recurrence_type")
        if recurrence_type not in LEGACY_KINDS:
            raise RecurrenceRuleError("Неподдерживаемый legacy recurrence_type")
        canonical["recurrence_type"] = recurrence_type
        canonical["interval"] = _validate_interval(rule.get("interval", 1))
        day_of_month = rule.get("day_of_month")
        if day_of_month is not None and (
            not isinstance(day_of_month, int)
            or isinstance(day_of_month, bool)
            or not 1 <= day_of_month <= 31
        ):
            raise RecurrenceRuleError("day_of_month должен быть от 1 до 31")
        canonical["day_of_month"] = day_of_month
        _validate_until(rule, canonical)
    elif kind in {"weekly_days", "weekdays"}:
        canonical["weekdays"] = _validate_weekdays(rule.get("weekdays"))
        canonical["interval"] = _validate_interval(rule.get("interval", 1))
        canonical["time"] = _validate_time(rule.get("time"))
        anchor_week = _parse_iso_date(rule.get("anchor_week"), "anchor_week")
        if anchor_week.weekday() != 0:
            raise RecurrenceRuleError("anchor_week должен быть понедельником")
        canonical["anchor_week"] = anchor_week.isoformat()
        _validate_until(rule, canonical)
    elif kind in {"monthly_nth", "monthly_last"}:
        canonical["weekday"] = _validate_weekday(rule.get("weekday"))
        if kind == "monthly_nth":
            ordinal = rule.get("ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 1 <= ordinal <= 5:
                raise RecurrenceRuleError("ordinal должен быть от 1 до 5")
            canonical["ordinal"] = ordinal
        canonical["interval"] = _validate_interval(rule.get("interval", 1))
        if canonical["interval"] != 1:
            raise RecurrenceRuleError("Месячное правило поддерживает только интервал 1")
        canonical["time"] = _validate_time(rule.get("time"))
        _validate_until(rule, canonical)
    elif kind == "yearly":
        month = rule.get("month")
        day = rule.get("day")
        if not isinstance(month, int) or isinstance(month, bool) or not 1 <= month <= 12:
            raise RecurrenceRuleError("month должен быть от 1 до 12")
        if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 31:
            raise RecurrenceRuleError("day должен быть от 1 до 31")
        # Reject dates which cannot exist in any year other than the special
        # leap-day case. This keeps malformed rules out of the database.
        if month == 2 and day > 29:
            raise RecurrenceRuleError("Неверный день февраля")
        if month in {4, 6, 9, 11} and day > 30:
            raise RecurrenceRuleError("Неверный день месяца")
        canonical["month"] = month
        canonical["day"] = day
        canonical["interval"] = _validate_interval(rule.get("interval", 1))
        if canonical["interval"] != 1:
            raise RecurrenceRuleError("Годовое правило поддерживает только интервал 1")
        canonical["time"] = _validate_time(rule.get("time"))
        _validate_until(rule, canonical)
    elif kind == "completion_relative":
        after_days = rule.get("after_days")
        if (
            not isinstance(after_days, int)
            or isinstance(after_days, bool)
            or not 1 <= after_days <= MAX_COMPLETION_DELAY_DAYS
        ):
            raise RecurrenceRuleError(f"after_days должен быть от 1 до {MAX_COMPLETION_DELAY_DAYS}")
        canonical["after_days"] = after_days
        _validate_until(rule, canonical)
    else:
        raise RecurrenceRuleError("Неподдерживаемый вид правила повторения")

    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_RULE_BYTES:
        raise RecurrenceRuleError("Правило повторения слишком длинное")
    return canonical


def encode_rule(rule: Mapping[str, Any]) -> str:
    canonical = canonicalize_rule(rule)
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_rule(value: str | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return canonicalize_rule(value)
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_RULE_BYTES:
        raise RecurrenceRuleError("Правило повторения повреждено")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise RecurrenceRuleError("Правило повторения повреждено") from exc
    if not isinstance(parsed, Mapping):
        raise RecurrenceRuleError("Правило повторения повреждено")
    return canonicalize_rule(parsed)


def legacy_rule(
    recurrence_type: str,
    interval: int,
    day_of_month: int | None = None,
    *,
    until: date | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": RULE_VERSION,
        "kind": "legacy",
        "recurrence_type": recurrence_type,
        "interval": interval,
        "day_of_month": day_of_month,
    }
    if until is not None:
        raw["until"] = until.isoformat()
    return canonicalize_rule(raw)


def weekly_rule(
    weekdays: list[int],
    local_time: time,
    *,
    interval: int = 1,
    anchor_week: date,
    until: date | None = None,
    kind: str = "weekly_days",
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": RULE_VERSION,
        "kind": kind,
        "weekdays": weekdays,
        "interval": interval,
        "time": local_time.strftime("%H:%M"),
        "anchor_week": anchor_week.isoformat(),
    }
    if until is not None:
        raw["until"] = until.isoformat()
    return canonicalize_rule(raw)


def monthly_nth_rule(
    weekday: int,
    ordinal: int,
    local_time: time,
    *,
    until: date | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": RULE_VERSION,
        "kind": "monthly_nth",
        "weekday": weekday,
        "ordinal": ordinal,
        "interval": 1,
        "time": local_time.strftime("%H:%M"),
    }
    if until is not None:
        raw["until"] = until.isoformat()
    return canonicalize_rule(raw)


def monthly_last_rule(
    weekday: int,
    local_time: time,
    *,
    until: date | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": RULE_VERSION,
        "kind": "monthly_last",
        "weekday": weekday,
        "interval": 1,
        "time": local_time.strftime("%H:%M"),
    }
    if until is not None:
        raw["until"] = until.isoformat()
    return canonicalize_rule(raw)


def yearly_rule(
    month: int,
    day: int,
    local_time: time,
    *,
    until: date | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": RULE_VERSION,
        "kind": "yearly",
        "month": month,
        "day": day,
        "interval": 1,
        "time": local_time.strftime("%H:%M"),
    }
    if until is not None:
        raw["until"] = until.isoformat()
    return canonicalize_rule(raw)


def completion_relative_rule(after_days: int, *, until: date | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": RULE_VERSION,
        "kind": "completion_relative",
        "after_days": after_days,
    }
    if until is not None:
        raw["until"] = until.isoformat()
    return canonicalize_rule(raw)


def is_completion_relative(rule: Mapping[str, Any] | None) -> bool:
    return bool(rule and rule.get("kind") == "completion_relative")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _rule_time(rule: Mapping[str, Any]) -> time:
    value = _validate_time(rule.get("time"))
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour, minute)


def _next_month(year: int, month: int, offset: int) -> tuple[int, int]:
    zero_based = year * 12 + month - 1 + offset
    return zero_based // 12, zero_based % 12 + 1


def _within_until(candidate_date: date, rule: Mapping[str, Any]) -> bool:
    until = _optional_until(rule)
    return until is None or candidate_date <= until


def _weekly_candidate_date(
    current_date: date,
    *,
    weekdays: list[int],
    interval: int,
    anchor_week: date,
    offset: int,
) -> date | None:
    candidate = current_date + timedelta(days=offset)
    candidate_week = candidate - timedelta(days=candidate.weekday())
    weeks_from_anchor = (candidate_week - anchor_week).days // 7
    if weeks_from_anchor < 0 or weeks_from_anchor % interval != 0:
        return None
    if candidate.weekday() not in weekdays:
        return None
    return candidate


def _next_weekly(
    current_utc: datetime,
    rule: Mapping[str, Any],
    timezone_name: str,
) -> datetime | None:
    local_current = from_utc_to_user(current_utc, timezone_name)
    weekdays = list(rule["weekdays"])
    interval = int(rule["interval"])
    anchor_week = _parse_iso_date(rule["anchor_week"], "anchor_week")
    target_time = _rule_time(rule)
    for offset in range(0, interval * 7 + 14):
        candidate_date = _weekly_candidate_date(
            local_current.date(),
            weekdays=weekdays,
            interval=interval,
            anchor_week=anchor_week,
            offset=offset,
        )
        if candidate_date is None or not _within_until(candidate_date, rule):
            continue
        candidate_utc = to_utc(datetime.combine(candidate_date, target_time), timezone_name)
        if candidate_utc > _as_utc(current_utc):
            return candidate_utc
    return None


def _monthly_weekday_date(year: int, month: int, weekday: int, ordinal: int) -> date | None:
    first = date(year, month, 1)
    day = 1 + (weekday - first.weekday()) % 7 + (ordinal - 1) * 7
    if day > calendar.monthrange(year, month)[1]:
        return None
    return date(year, month, day)


def _last_weekday_date(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _next_monthly(
    current_utc: datetime,
    rule: Mapping[str, Any],
    timezone_name: str,
) -> datetime | None:
    local_current = from_utc_to_user(current_utc, timezone_name)
    target_time = _rule_time(rule)
    weekday = int(rule["weekday"])
    for offset in range(0, 240):
        year, month = _next_month(local_current.year, local_current.month, offset)
        if rule["kind"] == "monthly_nth":
            candidate_date = _monthly_weekday_date(year, month, weekday, int(rule["ordinal"]))
        else:
            candidate_date = _last_weekday_date(year, month, weekday)
        if candidate_date is None or not _within_until(candidate_date, rule):
            continue
        candidate_utc = to_utc(datetime.combine(candidate_date, target_time), timezone_name)
        if candidate_utc > _as_utc(current_utc):
            return candidate_utc
    return None


def _next_yearly(
    current_utc: datetime,
    rule: Mapping[str, Any],
    timezone_name: str,
) -> datetime | None:
    local_current = from_utc_to_user(current_utc, timezone_name)
    target_time = _rule_time(rule)
    month = int(rule["month"])
    day = int(rule["day"])
    for year_offset in range(0, 401):
        year = local_current.year + year_offset
        try:
            candidate_date = date(year, month, day)
        except ValueError:
            # A yearly 29 February rule is skipped in non-leap years rather
            # than silently changing the user's requested calendar date.
            continue
        if not _within_until(candidate_date, rule):
            return None
        candidate_utc = to_utc(datetime.combine(candidate_date, target_time), timezone_name)
        if candidate_utc > _as_utc(current_utc):
            return candidate_utc
    return None


def _next_legacy(
    current_utc: datetime,
    rule: Mapping[str, Any],
    timezone_name: str,
) -> datetime | None:
    recurrence_type = str(rule["recurrence_type"])
    interval = int(rule["interval"])
    if recurrence_type == "none":
        return None
    if recurrence_type == "minutes":
        candidate = _as_utc(current_utc) + timedelta(minutes=interval)
        candidate_date = from_utc_to_user(candidate, timezone_name).date()
        return candidate if _within_until(candidate_date, rule) else None
    if recurrence_type == "hourly":
        candidate = _as_utc(current_utc) + timedelta(hours=interval)
        candidate_date = from_utc_to_user(candidate, timezone_name).date()
        return candidate if _within_until(candidate_date, rule) else None

    local_current = from_utc_to_user(current_utc, timezone_name).replace(tzinfo=None)
    if recurrence_type == "daily":
        candidate_local = local_current + timedelta(days=interval)
    elif recurrence_type == "weekly":
        candidate_local = local_current + timedelta(weeks=interval)
    elif recurrence_type == "monthly":
        candidate_local = local_current
        anchor = rule.get("day_of_month") or candidate_local.day
        for _ in range(interval):
            year, month = _next_month(candidate_local.year, candidate_local.month, 1)
            candidate_local = candidate_local.replace(
                year=year,
                month=month,
                day=min(int(anchor), calendar.monthrange(year, month)[1]),
            )
    else:
        raise RecurrenceRuleError("Неподдерживаемый legacy recurrence_type")
    if not _within_until(candidate_local.date(), rule):
        return None
    return to_utc(candidate_local, timezone_name)


def next_occurrence(
    current_utc: datetime,
    rule: Mapping[str, Any],
    timezone_name: str,
    *,
    completion_at_utc: datetime | None = None,
) -> datetime | None:
    canonical = canonicalize_rule(rule)
    kind = canonical["kind"]
    if kind == "none":
        return None
    if kind == "legacy":
        return _next_legacy(current_utc, canonical, timezone_name)
    if kind in {"weekly_days", "weekdays"}:
        return _next_weekly(current_utc, canonical, timezone_name)
    if kind in {"monthly_nth", "monthly_last"}:
        return _next_monthly(current_utc, canonical, timezone_name)
    if kind == "yearly":
        return _next_yearly(current_utc, canonical, timezone_name)
    if kind == "completion_relative":
        if completion_at_utc is None:
            return None
        completion_local = from_utc_to_user(completion_at_utc, timezone_name).replace(tzinfo=None)
        candidate_local = completion_local + timedelta(days=int(canonical["after_days"]))
        if not _within_until(candidate_local.date(), canonical):
            return None
        return to_utc(candidate_local, timezone_name)
    raise RecurrenceRuleError("Неподдерживаемый вид правила повторения")


def advance_until_future(
    current_utc: datetime,
    rule: Mapping[str, Any],
    timezone_name: str,
    now_utc: datetime,
    *,
    completion_at_utc: datetime | None = None,
) -> datetime | None:
    candidate = next_occurrence(
        current_utc,
        rule,
        timezone_name,
        completion_at_utc=completion_at_utc,
    )
    steps = 0
    while candidate is not None and candidate <= _as_utc(now_utc):
        steps += 1
        if steps > MAX_ADVANCE_STEPS:
            raise RecurrenceRuleError("Не удалось bounded-вычислить следующее повторение")
        candidate = next_occurrence(candidate, rule, timezone_name)
    return candidate


def first_occurrence_after(
    now_local: datetime,
    rule: Mapping[str, Any],
    timezone_name: str,
) -> datetime | None:
    """Find the first calendar occurrence after the supplied local instant."""

    if now_local.tzinfo is None:
        now_utc = to_utc(now_local, timezone_name)
    else:
        now_utc = now_local.astimezone(UTC)
    # Subtracting one microsecond lets a matching current-minute schedule be
    # selected while keeping the normal next_occurrence API strictly forward.
    candidate = next_occurrence(
        now_utc - timedelta(microseconds=1),
        canonicalize_rule(rule),
        timezone_name,
    )
    return candidate
