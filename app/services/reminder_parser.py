from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from app.db.models import RecurrenceType
from app.services.recurrence import (
    RecurrenceRuleError,
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
_DEADLINE_RE = re.compile(
    r"^(?P<prefix>/(?:deadline|remind_deadline)|/remind\s+(?:до|дедлайн\w*|deadline)|"
    r"напомни\s+(?:до|дедлайн\w*|deadline))\s+"
    r"(?P<date>\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}\s+[а-яё]+\s+\d{4}|сегодня|завтра)\s+"
    r"(?:(?:в\s+)?(?P<time>\d{1,2}:\d{2})\s+)?(?P<tail>.+)$",
    flags=re.IGNORECASE,
)
_NATURAL_DEADLINE_RE = re.compile(
    r"^(?P<task>.+?)\s+(?:до|к)\s+"
    r"(?P<date>\d{1,2}\.\d{1,2}(?:\.\d{4})?|"
    r"\d{1,2}\s+[а-яё]+(?:\s+\d{4})?|сегодня|завтра)"
    r"(?:\s+(?:в|к)\s*)?(?P<time>\d{1,2}:\d{2})?"
    r"(?:\s*\|\s*(?P<options>.+))?$",
    flags=re.IGNORECASE,
)

TODAY_RE = re.compile(r"^напомни\s+сегодня\s+в\s+(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.IGNORECASE)
TOMORROW_RE = re.compile(r"^напомни\s+завтра\s+в\s+(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.IGNORECASE)
_RELATIVE_BODY_SEPARATOR = r"(?:\s*[,.:;—-]\s*|\s+)"
IN_HOURS_RE = re.compile(
    rf"^напомни\s+через\s+(\d+)\s+час(?:а|ов)?{_RELATIVE_BODY_SEPARATOR}(.+)$",
    re.IGNORECASE,
)
IN_MINUTES_RE = re.compile(
    rf"^напомни\s+через\s+(\d+)\s+мин(?:ут|уты|уту)?{_RELATIVE_BODY_SEPARATOR}(.+)$",
    re.IGNORECASE,
)
_SPOKEN_RELATIVE_RE = re.compile(
    r"^напомни\s+через\s+(?P<number>[а-яё]+(?:\s+[а-яё]+)?)\s+"
    rf"(?P<unit>час(?:а|ов)?|мин(?:ут|уты|уту)?){_RELATIVE_BODY_SEPARATOR}"
    r"(?P<text>.+)$",
    re.IGNORECASE,
)
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
    r"(?:повторять\s+)?до\s+("
    r"\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}\s+[а-яё]+(?:\s+\d{4})?"
    r")(?:$|(?=[\s,;:]))",
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
_PERSISTENT_MODE_WORDS = r"(?:важн\w*|постоянн\w*|important|persistent)"
_MODE_PREFIX_RE = re.compile(
    rf"^(?P<prefix>/remind|напомни)\s+{_PERSISTENT_MODE_WORDS}"
    rf"(?:\s+напоминание)?(?:\s*[:,-]\s*|\s+)(?P<rest>.+)$",
    re.IGNORECASE,
)
_MODE_SUFFIX_RE = re.compile(
    rf"\s+\[(?P<mode>{_PERSISTENT_MODE_WORDS})\]\s*$",
    re.IGNORECASE,
)

_RUSSIAN_NUMBER_WORDS = {
    "один": 1,
    "одна": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
}
_RUSSIAN_TENS = {20, 30, 40, 50, 60}


def _parse_russian_number_words(value: str) -> int | None:
    words = value.casefold().replace("ё", "е").split()
    if not 1 <= len(words) <= 2:
        return None

    first = _RUSSIAN_NUMBER_WORDS.get(words[0])
    if first is None:
        return None
    if len(words) == 1:
        return first

    second = _RUSSIAN_NUMBER_WORDS.get(words[1])
    if first not in _RUSSIAN_TENS or second is None or not 1 <= second <= 9:
        return None
    return first + second


@dataclass(slots=True)
class ParsedReminder:
    local_dt: datetime
    text: str
    recurrence_type: Recurrence = "none"
    recurrence_interval: int = 1
    datetime_semantics: DatetimeSemantics = "wall_clock"
    recurrence_rule: dict[str, Any] | None = None
    recurrence_day_of_month: int | None = None
    mode: str = "normal"


@dataclass(frozen=True, slots=True)
class ClarificationRequest:
    """A bounded request for user input when deterministic parsing cannot decide."""

    kind: str
    prompt: str
    raw_text: str
    mode: str = "normal"


DEFAULT_DEADLINE_POINT_CODES = ("day_before", "before_deadline", "at_deadline")


@dataclass(frozen=True, slots=True)
class DeadlineRequest:
    """A deadline request that must be previewed and explicitly confirmed."""

    local_dt: datetime
    text: str
    point_codes: tuple[str, ...] = DEFAULT_DEADLINE_POINT_CODES
    overdue_after_minutes: int | None = None
    datetime_semantics: DatetimeSemantics = "wall_clock"
    mode: str = "normal"


def restore_clarification_mode(parsed: ParsedReminder, mode: str) -> ParsedReminder:
    """Keep a persisted persistent-mode request through an ambiguous answer."""

    if mode == "persistent":
        parsed.mode = "persistent"
    return parsed


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


def _parse_spoken_relative_interval(
    text: str,
    now_local: datetime,
) -> ParsedReminder | None:
    match = _SPOKEN_RELATIVE_RE.match(text)
    if match is None:
        return None

    amount = _parse_russian_number_words(match.group("number"))
    if amount is None:
        return None

    unit = match.group("unit").casefold()
    interval = timedelta(hours=amount) if unit.startswith("час") else timedelta(minutes=amount)
    return ParsedReminder(
        local_dt=_add_elapsed_interval(now_local, interval),
        text=match.group("text").strip(),
        datetime_semantics="instant",
    )


def _timezone_name(now_local: datetime) -> str:
    return str(getattr(now_local.tzinfo, "key", None) or "UTC")


def _parse_bound(value: str, *, default_year: int | None = None) -> date | None:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    parts = " ".join(value.lower().replace("ё", "е").split()).split()
    if len(parts) not in {2, 3} or not parts[0].isdigit():
        return None
    month = next(
        (month_number for stem, month_number in _DEADLINE_MONTH_STEMS if parts[1].startswith(stem)),
        None,
    )
    if month is None:
        return None
    if len(parts) == 3:
        if not parts[2].isdigit():
            return None
        year = int(parts[2])
    else:
        if default_year is None:
            return None
        year = default_year
    try:
        return date(year, month, int(parts[0]))
    except ValueError:
        return None


_DEADLINE_MONTH_STEMS: tuple[tuple[str, int], ...] = (
    ("январ", 1),
    ("феврал", 2),
    ("март", 3),
    ("апрел", 4),
    ("май", 5),
    ("мая", 5),
    ("июн", 6),
    ("июл", 7),
    ("август", 8),
    ("сентябр", 9),
    ("октябр", 10),
    ("ноябр", 11),
    ("декабр", 12),
)


def _parse_deadline_datetime(
    date_part: str,
    time_part: str | None,
    *,
    now_local: datetime,
) -> datetime | None:
    normalized_date = date_part.lower().replace("ё", "е")
    if normalized_date in {"сегодня", "завтра"}:
        base_date = now_local.date() + timedelta(days=normalized_date == "завтра")
        return _build_time(
            datetime.combine(base_date, time.min),
            *(time_part.split(":") if time_part else ("23", "59")),
        )

    if "." in date_part:
        date_parts = date_part.split(".")
        if len(date_parts) == 2:
            try:
                candidate = date(now_local.year, int(date_parts[1]), int(date_parts[0]))
            except ValueError:
                return None
            if candidate < now_local.date():
                candidate = date(now_local.year + 1, int(date_parts[1]), int(date_parts[0]))
            base_date = candidate
        else:
            parsed_date = _parse_bound(date_part)
            if parsed_date is None:
                return None
            base_date = parsed_date
        return _build_time(
            datetime.combine(base_date, time.min),
            *(time_part.split(":") if time_part else ("23", "59")),
        )
    if "-" in date_part:
        if not time_part:
            time_part = "23:59"
        return _parse_datetime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M")

    date_parts = normalized_date.split()
    if len(date_parts) not in {2, 3} or not date_parts[0].isdigit():
        return None
    month = next(
        (
            month_number
            for stem, month_number in _DEADLINE_MONTH_STEMS
            if date_parts[1].startswith(stem)
        ),
        None,
    )
    if month is None:
        return None
    try:
        year = int(date_parts[2]) if len(date_parts) == 3 else now_local.year
        base_date = date(year, month, int(date_parts[0]))
    except ValueError:
        return None
    if len(date_parts) == 2 and base_date < now_local.date():
        try:
            base_date = date(year + 1, month, int(date_parts[0]))
        except ValueError:
            return None
    return _build_time(
        datetime.combine(base_date, time.min),
        *(time_part.split(":") if time_part else ("23", "59")),
    )


def _extract_until(
    value: str,
    *,
    default_year: int | None = None,
) -> tuple[str, date | None, bool]:
    match = _UNTIL_RE.search(value)
    if match is None:
        return value, None, False
    until = _parse_bound(match.group(1), default_year=default_year)
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

    cleaned_schedule, until, had_until = _extract_until(
        raw_schedule,
        default_year=now_local.year,
    )
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

    cleaned_prefix, until, had_until = _extract_until(
        prefix,
        default_year=now_local.year,
    )
    cleaned_tail, tail_until, tail_had_until = _extract_until(
        tail,
        default_year=now_local.year,
    )
    if (had_until and until is None) or (tail_had_until and tail_until is None):
        return ClarificationRequest(
            kind="recurrence_until",
            prompt="Не смог разобрать дату окончания. Укажи её как 2026-12-31 и повтори команду.",
            raw_text=text[:MAX_INPUT_LENGTH],
        )
    if until is None:
        until = tail_until
    try:
        rule = _rule_from_prefix(
            cleaned_prefix,
            target_time=target_time,
            now_local=now_local,
            until=until,
        )
    except RecurrenceRuleError:
        return ClarificationRequest(
            kind="recurrence_rule",
            prompt=(
                "Интервал недельного правила должен быть от 1 до 52. "
                "Повтори команду с допустимым интервалом. Черновик действует 15 минут."
            ),
            raw_text=text[:MAX_INPUT_LENGTH],
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


def _extract_mode_marker(raw_text: str) -> tuple[str, str]:
    """Extract an explicit important/persistent marker from the command shell."""

    text = raw_text.strip()
    suffix = _MODE_SUFFIX_RE.search(text)
    if suffix is not None:
        return "persistent", text[: suffix.start()].rstrip()

    prefix = _MODE_PREFIX_RE.match(text)
    if prefix is not None:
        command = prefix.group("prefix")
        return "persistent", f"{command} {prefix.group('rest').strip()}"
    return "normal", text


def _deadline_policy_request(
    options: str,
    *,
    raw_text: str,
) -> tuple[tuple[str, ...], int | None] | ClarificationRequest:
    normalized = " ".join(options.lower().replace("ё", "е").split())
    if not normalized:
        return DEFAULT_DEADLINE_POINT_CODES, None
    if normalized in {"без эскалации", "только дедлайн", "только в срок"}:
        return ("at_deadline",), None

    codes: list[str] = []
    overdue_after_minutes: int | None = None
    segments = [
        segment.strip() for segment in re.split(r"\s*,\s*|\s+и\s+", normalized) if segment.strip()
    ]
    for segment in segments:
        if re.search(r"за\s+недел\w*|недел\w*\s+до", segment):
            code = "week_before"
        elif re.search(r"за\s+день|накануне|день\s+до", segment):
            code = "day_before"
        elif re.search(r"утр\w*|утро\s+дедлайна", segment):
            code = "deadline_morning"
        elif re.search(r"за\s+час|час\s+до|перед\s+дедлайном", segment):
            code = "before_deadline"
        elif re.search(r"в\s+срок|в\s+момент\s+дедлайна|в\s+дедлайн", segment):
            code = "at_deadline"
        elif re.search(r"просроч|после\s+(?:срока|дедлайна)", segment):
            match = re.search(
                r"через\s+(\d+)\s+(минут\w*|час\w*|дн\w*)",
                segment,
            )
            if match is None:
                overdue_after_minutes = 60
            else:
                count = int(match.group(1))
                unit = match.group(2)
                multiplier = 1 if unit.startswith("мин") else 60 if unit.startswith("час") else 1440
                overdue_after_minutes = count * multiplier
            code = "overdue"
        else:
            return ClarificationRequest(
                kind="deadline_policy",
                prompt=(
                    "Не понял точки защиты дедлайна. Используй после символа |: "
                    "«за день, за час, в срок» или «за день, в срок, просрочено через час»."
                ),
                raw_text=raw_text[:MAX_INPUT_LENGTH],
            )
        if code in codes:
            return ClarificationRequest(
                kind="deadline_policy",
                prompt="Каждую точку дедлайна можно указать только один раз. Повтори команду.",
                raw_text=raw_text[:MAX_INPUT_LENGTH],
            )
        codes.append(code)

    if "at_deadline" not in codes:
        codes.append("at_deadline")
    return tuple(codes), overdue_after_minutes


def _parse_deadline_input(
    raw_text: str,
    now_local: datetime,
) -> DeadlineRequest | ClarificationRequest | object:
    normalized_text = raw_text.strip()
    match = _DEADLINE_RE.match(normalized_text)
    natural_match = None if match is not None else _NATURAL_DEADLINE_RE.match(normalized_text)
    if match is None and natural_match is None:
        return _NO_MATCH

    if match is not None:
        date_part = match.group("date")
        time_part = match.group("time")
        tail = match.group("tail").strip()
    else:
        assert natural_match is not None
        date_part = natural_match.group("date")
        time_part = natural_match.group("time")
        tail = natural_match.group("task").strip()
        options_from_match = natural_match.group("options")
        if options_from_match:
            tail = f"{tail} | {options_from_match.strip()}"
        if tail.lower().startswith("напомни "):
            tail = tail[8:].strip()

    local_dt = _parse_deadline_datetime(date_part, time_part, now_local=now_local)
    if local_dt is None:
        return ClarificationRequest(
            kind="deadline_datetime",
            prompt="Не смог разобрать дату дедлайна. Укажи её как 2026-09-10 18:00.",
            raw_text=raw_text[:MAX_INPUT_LENGTH],
        )

    reminder_text = tail
    options = ""
    if "|" in tail:
        reminder_text, options = (part.strip() for part in tail.split("|", 1))
    elif ";" in tail:
        possible_text, possible_options = (part.strip() for part in tail.split(";", 1))
        if re.search(
            r"за\s+недел|за\s+день|за\s+час|утр|срок|дедлайн|просроч|после",
            possible_options,
            re.IGNORECASE,
        ):
            reminder_text, options = possible_text, possible_options
    if not reminder_text:
        return ClarificationRequest(
            kind="deadline_text",
            prompt="Добавь текст задачи после даты дедлайна. Например: «оплатить VPS». ",
            raw_text=raw_text[:MAX_INPUT_LENGTH],
        )

    policy = _deadline_policy_request(options, raw_text=raw_text)
    if isinstance(policy, ClarificationRequest):
        return policy
    point_codes, overdue_after_minutes = policy
    return DeadlineRequest(
        local_dt=local_dt,
        text=reminder_text,
        point_codes=point_codes,
        overdue_after_minutes=overdue_after_minutes,
    )


def _parse_reminder_input(
    raw_text: str,
    now_local: datetime,
) -> ParsedReminder | DeadlineRequest | ClarificationRequest | None:
    text = raw_text.strip()
    if not text or len(text) > MAX_INPUT_LENGTH:
        return None

    advanced = _parse_advanced_recurrence(text, now_local)
    if advanced is not _NO_MATCH:
        return advanced if isinstance(advanced, (ParsedReminder, ClarificationRequest)) else None

    deadline = _parse_deadline_input(text, now_local)
    if deadline is not _NO_MATCH:
        return (
            deadline
            if isinstance(deadline, (ParsedReminder, DeadlineRequest, ClarificationRequest))
            else None
        )

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

    spoken_relative = _parse_spoken_relative_interval(text, now_local)
    if spoken_relative is not None:
        return spoken_relative

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


def parse_reminder_input(
    raw_text: str,
    now_local: datetime,
) -> ParsedReminder | DeadlineRequest | ClarificationRequest | None:
    """Parse a reminder and preserve an explicit normal/persistent mode marker."""

    mode, normalized_text = _extract_mode_marker(raw_text)
    parsed = _parse_reminder_input(normalized_text, now_local)
    if isinstance(parsed, ParsedReminder):
        parsed.mode = mode
    elif isinstance(parsed, (ClarificationRequest, DeadlineRequest)):
        parsed = replace(parsed, mode=mode)
    return parsed


def parse_deadline_input(
    raw_text: str,
    now_local: datetime,
) -> DeadlineRequest | ClarificationRequest | None:
    """Parse only the explicit deadline syntax used by the confirmation flow."""

    mode, normalized_text = _extract_mode_marker(raw_text)
    parsed = _parse_deadline_input(normalized_text, now_local)
    if parsed is _NO_MATCH:
        return None
    if isinstance(parsed, (DeadlineRequest, ClarificationRequest)):
        return replace(parsed, mode=mode)
    return None


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
    mode: str = "normal",
) -> ParsedReminder | ClarificationRequest | None:
    """Resolve a stored clarification without accepting an ambiguous shortcut."""

    parsed = parse_reminder_input(answer, now_local=now_local)
    if isinstance(parsed, ParsedReminder):
        return restore_clarification_mode(parsed, mode)

    value = answer.strip()
    local_dt = _explicit_answer_datetime(value)
    reminder_text = _clarification_text(raw_text)
    if local_dt is not None and reminder_text:
        return restore_clarification_mode(
            ParsedReminder(local_dt=local_dt, text=reminder_text),
            mode,
        )

    normalized_raw = raw_text.lower().replace("ё", "е")
    if re.fullmatch(r"\d{1,2}:\d{2}", value) and (
        "завтра вечером" in normalized_raw or "после обеда" in normalized_raw
    ):
        hour, minute = (int(part) for part in value.split(":", 1))
        tomorrow = "завтра вечером" in normalized_raw
        local_dt = _build_time(
            now_local + timedelta(days=1 if tomorrow else 0),
            str(hour),
            str(minute),
        )
        if not tomorrow and local_dt is not None and local_dt <= now_local:
            local_dt += timedelta(days=1)
        if local_dt is not None and reminder_text:
            return restore_clarification_mode(
                ParsedReminder(local_dt=local_dt, text=reminder_text),
                mode,
            )
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
