from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from app.db.models import RecurrenceType
from app.services.recurrence import (
    completion_relative_rule,
    first_occurrence_after,
    legacy_rule,
    monthly_last_rule,
    monthly_nth_rule,
    weekly_rule,
    yearly_rule,
)
from app.utils.datetime_utils import DatetimeSemantics, from_utc_to_user

Recurrence = Literal[
    "none",
    "minutes",
    "hourly",
    "daily",
    "weekly",
    "monthly",
    "advanced",
]
MAX_INPUT_LENGTH = 4096
_NO_MATCH = object()

REMIND_COMMAND_RE = re.compile(
    r"^/remind\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s+(.+)$",
    flags=re.IGNORECASE,
)

REMIND_TEXT_RE = re.compile(
    r"^напомни\s+(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})\s+(.+)$",
    flags=re.IGNORECASE,
)
REMIND_ISO_RE = re.compile(
    r"^напомни\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s+(.+)$",
    flags=re.IGNORECASE,
)

TODAY_RE = re.compile(r"^напомни\s+сегодня\s+в\s+(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.IGNORECASE)
TOMORROW_RE = re.compile(r"^напомни\s+завтра\s+в\s+(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.IGNORECASE)
IN_HOURS_RE = re.compile(r"^напомни\s+через\s+(\d+)\s+час(?:а|ов)?\s+(.+)$", re.IGNORECASE)
IN_MINUTES_RE = re.compile(r"^напомни\s+через\s+(\d+)\s+мин(?:ут|уты|уту)?\s+(.+)$", re.IGNORECASE)
EVERY_DAY_RE = re.compile(
    r"^напомни\s+каждый\s+день\s+в\s+(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.IGNORECASE
)
EVERY_WEEK_RE = re.compile(
    r"^напомни\s+каждую\s+неделю\s+в\s+(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.IGNORECASE
)
EVERY_MONTH_RE = re.compile(
    r"^напомни\s+каждый\s+месяц\s+в\s+(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.IGNORECASE
)
EVERY_MINUTES_RE = re.compile(
    r"^напомни\s+каждые\s+(\d+)\s+(?:минут(?:у|ы)?|мин)\s+(.+)$", re.IGNORECASE
)
EVERY_HOUR_RE = re.compile(r"^напомни\s+каждый\s+час\s+(.+)$", re.IGNORECASE)
EVERY_HOURS_RE = re.compile(r"^напомни\s+каждые\s+(\d+)\s+час(?:а|ов)?\s+(.+)$", re.IGNORECASE)

_TIME_WITH_TAIL_RE = re.compile(
    r"^напомни\s+(.+?)\s+в\s+(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.IGNORECASE
)
_UNTIL_RE = re.compile(
    r"(?:повторять\s+)?до\s+(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
_COMPLETION_RE = re.compile(
    r"\s+(?:через\s+(?P<before_days>\d+)\s+дн(?:я|ей)?\s+после\s+"
    r"(?:фактического\s+)?выполнения|после\s+(?:фактического\s+)?выполнения\s+"
    r"(?:через\s+)?(?P<after_days>\d+)\s+дн(?:я|ей)?)\s*[:;,\-]?\s*",
    re.IGNORECASE,
)

_WEEKDAY_PATTERNS: tuple[tuple[int, str], ...] = (
    (0, r"(?:понедельник\w*|пн)"),
    (1, r"(?:вторник\w*|вт)"),
    (2, r"(?:сред\w*|ср)"),
    (3, r"(?:четверг\w*|чт)"),
    (4, r"(?:пятниц\w*|пт)"),
    (5, r"(?:суббот\w*|сб)"),
    (6, r"(?:воскресень\w*|вс)"),
)
_MONTH_PATTERNS: tuple[tuple[int, str], ...] = (
    (1, r"январ\w*"),
    (2, r"феврал\w*"),
    (3, r"март\w*"),
    (4, r"апрел\w*"),
    (5, r"ма[йя]\w*"),
    (6, r"июн\w*"),
    (7, r"июл\w*"),
    (8, r"август\w*"),
    (9, r"сентябр\w*"),
    (10, r"октябр\w*"),
    (11, r"ноябр\w*"),
    (12, r"декабр\w*"),
)
_ORDINAL_WORDS = {
    "первый": 1,
    "первую": 1,
    "первая": 1,
    "второй": 2,
    "вторую": 2,
    "вторая": 2,
    "третий": 3,
    "третью": 3,
    "третья": 3,
    "четвертый": 4,
    "четвертую": 4,
    "четвертая": 4,
    "пятый": 5,
    "пятую": 5,
    "пятая": 5,
}
_ORDINAL_RE = re.compile(r"(?P<ordinal>[1-5])(?:-?й|-?я|-?ая|-?ый|-?ую)?", re.IGNORECASE)


@dataclass(slots=True)
class ParsedReminder:
    local_dt: datetime
    text: str
    recurrence_type: Recurrence = "none"
    recurrence_interval: int = 1
    datetime_semantics: DatetimeSemantics = "wall_clock"
    recurrence_rule: dict[str, Any] | None = None
    recurrence_day_of_month: int | None = None


@dataclass(frozen=True, slots=True)
class ClarificationRequest:
    """A bounded request for user input when deterministic parsing cannot decide."""

    kind: str
    prompt: str
    raw_text: str


def _parse_datetime(value: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(value, fmt)
    except ValueError:
        return None


def _build_time(base_dt: datetime, hour: str, minute: str | None) -> datetime | None:
    try:
        return base_dt.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
    except ValueError:
        return None


def _add_elapsed_interval(base_dt: datetime, interval: timedelta) -> datetime:
    if base_dt.tzinfo is None:
        return base_dt + interval
    return (base_dt.astimezone(UTC) + interval).astimezone(base_dt.tzinfo)


def _timezone_name(now_local: datetime) -> str:
    return str(getattr(now_local.tzinfo, "key", None) or "UTC")


def _parse_bound(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _extract_until(value: str) -> tuple[str, date | None, bool]:
    match = _UNTIL_RE.search(value)
    if match is None:
        return value, None, False
    until = _parse_bound(match.group(1))
    return f"{value[: match.start()]} {value[match.end() :]}".strip(), until, True


def _weekday_numbers(value: str) -> list[int]:
    result: set[int] = set()
    for weekday, pattern in _WEEKDAY_PATTERNS:
        if re.search(rf"(?<![а-яё]){pattern}(?![а-яё])", value, re.IGNORECASE):
            result.add(weekday)
    return sorted(result)


def _month_number(value: str) -> int | None:
    for month, pattern in _MONTH_PATTERNS:
        if re.search(rf"(?<![а-яё]){pattern}(?![а-яё])", value, re.IGNORECASE):
            return month
    return None


def _ordinal_number(value: str) -> int | None:
    for word, ordinal in _ORDINAL_WORDS.items():
        if re.search(rf"(?<![а-яё]){word}(?![а-яё])", value, re.IGNORECASE):
            return ordinal
    match = _ORDINAL_RE.search(value)
    if match is not None:
        return int(match.group("ordinal"))
    return None


def _target_time(hour: str, minute: str | None) -> time | None:
    try:
        return time(int(hour), int(minute or 0))
    except ValueError:
        return None


def _first_advanced_local(now_local: datetime, rule: dict[str, Any]) -> datetime | None:
    first_utc = first_occurrence_after(now_local, rule, _timezone_name(now_local))
    if first_utc is None:
        return None
    if now_local.tzinfo is None:
        return first_utc.replace(tzinfo=None)
    return from_utc_to_user(first_utc, _timezone_name(now_local)).replace(tzinfo=None)


def _rule_from_prefix(
    prefix: str,
    *,
    target_time: time,
    now_local: datetime,
    until: date | None,
) -> dict[str, Any] | None:
    normalized = " ".join(prefix.lower().replace("ё", "е").split())
    weekdays = _weekday_numbers(normalized)
    anchor_week = now_local.date() - timedelta(days=now_local.weekday())

    if "будн" in normalized or "рабоч" in normalized:
        return weekly_rule(
            [0, 1, 2, 3, 4],
            target_time,
            anchor_week=anchor_week,
            until=until,
            kind="weekdays",
        )

    if "месяц" in normalized and weekdays:
        if "последн" in normalized:
            return monthly_last_rule(weekdays[0], target_time, until=until)
        ordinal = _ordinal_number(normalized)
        if ordinal is not None:
            return monthly_nth_rule(weekdays[0], ordinal, target_time, until=until)

    if "год" in normalized or "ежегод" in normalized:
        month = _month_number(normalized)
        if month is None:
            return None
        day_match = re.search(
            r"(?<!\d)([0-3]?\d)(?:-?го)?\s+(?:январ\w*|феврал\w*|март\w*|"
            r"апрел\w*|ма[йя]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|"
            r"ноябр\w*|декабр\w*)",
            normalized,
        )
        if day_match is None:
            return None
        try:
            day = int(day_match.group(1))
            return yearly_rule(month, day, target_time, until=until)
        except ValueError:
            return None

    interval_match = re.search(r"кажд(?:ые|ую)\s+(\d+)\s+недел", normalized)
    if "недел" in normalized and (weekdays or "каждую неделю" in normalized or interval_match):
        interval = int(interval_match.group(1)) if interval_match else 1
        return weekly_rule(
            weekdays or [now_local.weekday()],
            target_time,
            interval=interval,
            anchor_week=anchor_week,
            until=until,
        )

    if (
        ("кажд" in normalized and weekdays)
        or "по дням" in normalized
        or (weekdays and re.search(r"\bпо\s+", normalized) is not None)
    ):
        return weekly_rule(
            weekdays or [now_local.weekday()],
            target_time,
            anchor_week=anchor_week,
            until=until,
        )

    if "каждый день" in normalized or "ежеднев" in normalized:
        if until is None:
            return None
        return legacy_rule(RecurrenceType.DAILY.value, 1, until=until)

    if "каждый месяц" in normalized or "ежемесяч" in normalized:
        if until is None:
            return None
        return legacy_rule(RecurrenceType.MONTHLY.value, 1, now_local.day, until=until)

    return None


def _parse_completion_relative(
    text: str,
    now_local: datetime,
) -> ParsedReminder | ClarificationRequest | object:
    match = _COMPLETION_RE.search(text)
    if match is None or not text.lower().startswith("напомни"):
        return _NO_MATCH

    before = text[len("напомни") : match.start()].strip(" ,")
    after = text[match.end() :].strip(" ,")
    days = int(match.group("before_days") or match.group("after_days") or 0)
    if not before or days < 1:
        return ClarificationRequest(
            kind="completion_schedule",
            prompt=(
                "Уточни первое время и текст: например «напомни завтра в 9 отчёт, "
                "через 3 дня после выполнения». Черновик действует 15 минут."
            ),
            raw_text=text[:MAX_INPUT_LENGTH],
        )

    # Both natural orders are accepted: the marker can precede the reminder
    # text ("... после выполнения: текст") or follow it ("... текст, после
    # выполнения"). The first form is unambiguous by parsing the prefix with
    # a private placeholder; the second is split at the explicit time token.
    parsed_before = parse_reminder_input(
        f"напомни {before} __completion_initial__",
        now_local=now_local,
    )
    if isinstance(parsed_before, ParsedReminder) and parsed_before.text == "__completion_initial__":
        raw_schedule = before
        reminder_text = after
    else:
        iso_match = re.match(r"^(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})\s+(.+)$", before)
        date_match = re.match(r"^(\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2})\s+(.+)$", before)
        time_match = re.search(r"\sв\s+\d{1,2}(?::\d{2})?\b", before, re.IGNORECASE)
        if iso_match is not None:
            raw_schedule, reminder_text = iso_match.groups()
        elif date_match is not None:
            raw_schedule, reminder_text = date_match.groups()
        elif time_match is not None:
            raw_schedule = before[: time_match.end()].strip()
            reminder_text = before[time_match.end() :].strip(" ,")
        else:
            raw_schedule, reminder_text = before, ""

    if not raw_schedule or not reminder_text:
        return ClarificationRequest(
            kind="completion_schedule",
            prompt=(
                "Уточни первое время и текст: например «напомни завтра в 9 отчёт, "
                "через 3 дня после выполнения». Черновик действует 15 минут."
            ),
            raw_text=text[:MAX_INPUT_LENGTH],
        )

    cleaned_schedule, until, had_until = _extract_until(raw_schedule)
    if had_until and until is None:
        return ClarificationRequest(
            kind="completion_until",
            prompt="Не смог разобрать дату окончания. Укажи её как 2026-12-31 и повтори команду.",
            raw_text=text[:MAX_INPUT_LENGTH],
        )
    parsed_initial = parse_reminder_input(
        f"напомни {cleaned_schedule} __completion_initial__",
        now_local=now_local,
    )
    if not isinstance(parsed_initial, ParsedReminder):
        return ClarificationRequest(
            kind="completion_schedule",
            prompt=(
                "Уточни первое время в явном формате: «напомни 2026-09-10 09:00 "
                "текст, через 3 дня после выполнения»."
            ),
            raw_text=text[:MAX_INPUT_LENGTH],
        )
    try:
        rule = completion_relative_rule(days, until=until)
    except ValueError:
        return ClarificationRequest(
            kind="completion_delay",
            prompt="Интервал после выполнения должен быть от 1 до 3650 дней. Повтори команду.",
            raw_text=text[:MAX_INPUT_LENGTH],
        )
    return ParsedReminder(
        local_dt=parsed_initial.local_dt,
        text=reminder_text,
        recurrence_type=RecurrenceType.ADVANCED.value,
        recurrence_interval=1,
        recurrence_rule=rule,
    )


def _parse_advanced_recurrence(
    text: str,
    now_local: datetime,
) -> ParsedReminder | ClarificationRequest | object:
    completion = _parse_completion_relative(text, now_local)
    if completion is not _NO_MATCH:
        return completion

    match = _TIME_WITH_TAIL_RE.match(text)
    if match is None:
        return _NO_MATCH
    prefix, hour, minute, tail = match.groups()
    target_time = _target_time(hour, minute)
    if target_time is None:
        return None

    cleaned_prefix, until, had_until = _extract_until(prefix)
    cleaned_tail, tail_until, tail_had_until = _extract_until(tail)
    if (had_until and until is None) or (tail_had_until and tail_until is None):
        return ClarificationRequest(
            kind="recurrence_until",
            prompt="Не смог разобрать дату окончания. Укажи её как 2026-12-31 и повтори команду.",
            raw_text=text[:MAX_INPUT_LENGTH],
        )
    if until is None:
        until = tail_until
    rule = _rule_from_prefix(
        cleaned_prefix,
        target_time=target_time,
        now_local=now_local,
        until=until,
    )
    if rule is None:
        return _NO_MATCH
    reminder_text = cleaned_tail.strip()
    if not reminder_text:
        return ClarificationRequest(
            kind="reminder_text",
            prompt="Добавь текст напоминания после расписания. Черновик действует 15 минут.",
            raw_text=text[:MAX_INPUT_LENGTH],
        )
    if rule["kind"] == "legacy":
        first_local = _build_time(now_local, hour, minute)
        if first_local is not None and first_local.replace(tzinfo=None) <= now_local.replace(
            tzinfo=None
        ):
            first_local += timedelta(days=1)
        if first_local is not None and until is not None and first_local.date() > until:
            first_local = None
    else:
        first_local = _first_advanced_local(now_local, rule)
    if first_local is None:
        return ClarificationRequest(
            kind="recurrence_until",
            prompt="Дата окончания уже раньше первого возможного повторения. Укажи более позднюю дату.",
            raw_text=text[:MAX_INPUT_LENGTH],
        )
    recurrence_type = (
        RecurrenceType.ADVANCED.value if rule["kind"] != "legacy" else str(rule["recurrence_type"])
    )
    interval = int(rule.get("interval", 1))
    day_of_month = (
        int(rule["day_of_month"])
        if rule["kind"] == "legacy" and rule.get("day_of_month") is not None
        else None
    )
    return ParsedReminder(
        local_dt=first_local,
        text=reminder_text,
        recurrence_type=recurrence_type,  # type: ignore[arg-type]
        recurrence_interval=interval,
        recurrence_rule=rule,
        recurrence_day_of_month=day_of_month,
    )


def _ambiguous_clarification(text: str) -> ClarificationRequest | None:
    normalized = text.lower().replace("ё", "е")
    if not normalized.startswith(("напомни", "/remind")):
        return None
    if re.search(
        r"\bв\s+(?:понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|"
        r"суббот\w*|воскресень\w*)\s+в\s+\d",
        normalized,
    ):
        prompt = "Уточни полную дату и время: например «напомни 2026-09-11 08:00 текст»."
        kind = "ambiguous_weekday"
    elif "завтра вечером" in normalized:
        prompt = "Во сколько именно завтра? Ответь полной командой или укажи HH:MM."
        kind = "ambiguous_evening"
    elif "часов в девять" in normalized:
        prompt = "Уточни дату и точное время (с минутами), например 2026-09-11 09:00."
        kind = "ambiguous_clock"
    elif "после обеда" in normalized:
        prompt = "Укажи точное время вместо «после обеда», например 14:00."
        kind = "ambiguous_clock"
    else:
        prompt = (
            "Не смог однозначно разобрать расписание. Повтори полной командой с датой и "
            "временем, например «напомни 2026-09-11 09:00 текст»."
        )
        kind = "unsupported"
    return ClarificationRequest(
        kind=kind,
        prompt=f"{prompt}\nЧерновик действует 15 минут. /cancel отменит его.",
        raw_text=text[:MAX_INPUT_LENGTH],
    )


def parse_reminder_input(
    raw_text: str,
    now_local: datetime,
) -> ParsedReminder | ClarificationRequest | None:
    text = raw_text.strip()
    if not text or len(text) > MAX_INPUT_LENGTH:
        return None

    completion = _parse_completion_relative(text, now_local)
    if completion is not _NO_MATCH:
        return (
            completion if isinstance(completion, (ParsedReminder, ClarificationRequest)) else None
        )

    command_match = REMIND_COMMAND_RE.match(text)
    if command_match:
        date_part, time_part, reminder_text = command_match.groups()
        local_dt = _parse_datetime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M")
        if local_dt is None:
            return None
        return ParsedReminder(local_dt=local_dt, text=reminder_text.strip())

    iso_match = REMIND_ISO_RE.match(text)
    if iso_match:
        date_part, time_part, reminder_text = iso_match.groups()
        local_dt = _parse_datetime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M")
        if local_dt is None:
            return None
        return ParsedReminder(local_dt=local_dt, text=reminder_text.strip())

    text_match = REMIND_TEXT_RE.match(text)
    if text_match:
        date_part, time_part, reminder_text = text_match.groups()
        local_dt = _parse_datetime(f"{date_part} {time_part}", "%d.%m.%Y %H:%M")
        if local_dt is None:
            return None
        return ParsedReminder(local_dt=local_dt, text=reminder_text.strip())

    m = TODAY_RE.match(text)
    if m:
        hour, minute, reminder_text = m.groups()
        local_dt = _build_time(now_local, hour, minute)
        if local_dt is None:
            return None
        return ParsedReminder(local_dt=local_dt, text=reminder_text.strip())

    m = TOMORROW_RE.match(text)
    if m:
        hour, minute, reminder_text = m.groups()
        base_dt = now_local + timedelta(days=1)
        local_dt = _build_time(base_dt, hour, minute)
        if local_dt is None:
            return None
        return ParsedReminder(local_dt=local_dt, text=reminder_text.strip())

    m = IN_HOURS_RE.match(text)
    if m:
        hours, reminder_text = m.groups()
        dt = _add_elapsed_interval(now_local, timedelta(hours=int(hours)))
        return ParsedReminder(
            local_dt=dt,
            text=reminder_text.strip(),
            datetime_semantics="instant",
        )

    m = IN_MINUTES_RE.match(text)
    if m:
        minutes, reminder_text = m.groups()
        dt = _add_elapsed_interval(now_local, timedelta(minutes=int(minutes)))
        return ParsedReminder(
            local_dt=dt,
            text=reminder_text.strip(),
            datetime_semantics="instant",
        )

    advanced = _parse_advanced_recurrence(text, now_local)
    if advanced is not _NO_MATCH:
        return advanced if isinstance(advanced, (ParsedReminder, ClarificationRequest)) else None

    m = EVERY_DAY_RE.match(text)
    if m:
        hour, minute, reminder_text = m.groups()
        local_dt = _build_time(now_local, hour, minute)
        if local_dt is None:
            return None
        return ParsedReminder(
            local_dt=local_dt, text=reminder_text.strip(), recurrence_type="daily"
        )

    m = EVERY_WEEK_RE.match(text)
    if m:
        hour, minute, reminder_text = m.groups()
        local_dt = _build_time(now_local, hour, minute)
        if local_dt is None:
            return None
        return ParsedReminder(
            local_dt=local_dt, text=reminder_text.strip(), recurrence_type="weekly"
        )

    m = EVERY_MONTH_RE.match(text)
    if m:
        hour, minute, reminder_text = m.groups()
        local_dt = _build_time(now_local, hour, minute)
        if local_dt is None:
            return None
        return ParsedReminder(
            local_dt=local_dt,
            text=reminder_text.strip(),
            recurrence_type="monthly",
            recurrence_day_of_month=local_dt.day,
        )

    m = EVERY_MINUTES_RE.match(text)
    if m:
        minutes, reminder_text = m.groups()
        interval = int(minutes)
        dt = _add_elapsed_interval(now_local, timedelta(minutes=interval))
        return ParsedReminder(
            local_dt=dt,
            text=reminder_text.strip(),
            recurrence_type="minutes",
            recurrence_interval=interval,
            datetime_semantics="instant",
        )

    m = EVERY_HOUR_RE.match(text)
    if m:
        (reminder_text,) = m.groups()
        dt = _add_elapsed_interval(now_local, timedelta(hours=1))
        return ParsedReminder(
            local_dt=dt,
            text=reminder_text.strip(),
            recurrence_type="hourly",
            datetime_semantics="instant",
        )

    m = EVERY_HOURS_RE.match(text)
    if m:
        hours, reminder_text = m.groups()
        interval = int(hours)
        dt = _add_elapsed_interval(now_local, timedelta(hours=interval))
        return ParsedReminder(
            local_dt=dt,
            text=reminder_text.strip(),
            recurrence_type="hourly",
            recurrence_interval=interval,
            datetime_semantics="instant",
        )

    return _ambiguous_clarification(text)


def _explicit_answer_datetime(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M"):
        parsed = _parse_datetime(value, fmt)
        if parsed is not None:
            return parsed
    return None


def _clarification_text(raw_text: str) -> str | None:
    value = raw_text.strip()
    patterns = (
        r"^напомни\s+в\s+пятниц\w*\s+в\s+\d{1,2}(?::\d{2})?\s+(.+)$",
        r"^напомни\s+завтра\s+вечером\s+(.+)$",
        r"^напомни\s+часов\s+в\s+девять\s+(.+)$",
        r"^напомни\s+после\s+обеда\s+(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, value, re.IGNORECASE)
        if match is not None:
            return match.group(1).strip()
    return None


def parse_clarification_answer(
    raw_text: str,
    answer: str,
    *,
    now_local: datetime,
) -> ParsedReminder | ClarificationRequest | None:
    """Resolve a stored clarification without accepting an ambiguous shortcut."""

    parsed = parse_reminder_input(answer, now_local=now_local)
    if isinstance(parsed, ParsedReminder):
        return parsed

    value = answer.strip()
    local_dt = _explicit_answer_datetime(value)
    reminder_text = _clarification_text(raw_text)
    if local_dt is not None and reminder_text:
        return ParsedReminder(local_dt=local_dt, text=reminder_text)

    if re.fullmatch(r"\d{1,2}:\d{2}", value) and "завтра вечером" in raw_text.lower():
        hour, minute = (int(part) for part in value.split(":", 1))
        local_dt = _build_time(now_local + timedelta(days=1), str(hour), str(minute))
        if local_dt is not None and reminder_text:
            return ParsedReminder(local_dt=local_dt, text=reminder_text)
    return None


def parse_recurrence(text: str) -> tuple[str, int]:
    text = text.lower()

    # каждые X минут
    m = re.search(r"каждые\s+(\d+)\s+(?:минут(?:у|ы)?|мин)", text)
    if m:
        return RecurrenceType.MINUTES.value, int(m.group(1))

    # каждый час
    if "каждый час" in text:
        return RecurrenceType.HOURLY.value, 1

    # каждые X часов
    m = re.search(r"каждые\s+(\d+)\s+час", text)
    if m:
        return RecurrenceType.HOURLY.value, int(m.group(1))

    # день
    if "каждый день" in text:
        return RecurrenceType.DAILY.value, 1

    # неделя
    if "каждую неделю" in text:
        return RecurrenceType.WEEKLY.value, 1

    # месяц
    if "каждый месяц" in text:
        return RecurrenceType.MONTHLY.value, 1

    return RecurrenceType.NONE.value, 1
