from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from app.services.reminder_parser import (
    ClarificationRequest,
    DeadlineRequest,
    ParsedReminder,
    _parse_russian_number_words,
    parse_reminder_input,
)

_VOICE_COMMAND_PREFIX_RE = re.compile(
    r"^(?:напомни|напомню|напомнить)(?:(?:\s*[,.:;—-]\s+|\s+)(.*))?$",
    re.IGNORECASE,
)
_VOICE_RELATIVE_BODY_SEPARATOR = r"(?:\s*[,.:;—-]\s+|\s+)"
_WHISPER_MINUTE_ARTIFACT_RE = re.compile(
    r"^напомни\s+через\s+"
    r"(?P<number>\d+|[а-яё]+(?:\s+[а-яё]+)?)\s+минута"
    rf"(?P<separator>{_VOICE_RELATIVE_BODY_SEPARATOR})(?P<body>.+)$",
    re.IGNORECASE,
)


def _canonicalize_voice_command_prefix(transcript: str) -> str | None:
    match = _VOICE_COMMAND_PREFIX_RE.fullmatch(transcript)
    if match is None:
        return None
    remainder = (match.group(1) or "").strip()
    return f"напомни {remainder}" if remainder else "напомни"


def _numeric_minute_value_is_safe(number: str, now_local: datetime) -> bool:
    try:
        interval = timedelta(minutes=int(number))
        if now_local.tzinfo is None:
            now_local + interval
        else:
            (now_local.astimezone(UTC) + interval).astimezone(now_local.tzinfo)
    except (OverflowError, ValueError):
        return False
    return True


def _normalize_whisper_minute_artifact(candidate: str, *, now_local: datetime) -> str:
    """Normalize one bounded Whisper artifact without touching the transcript/body."""

    match = _WHISPER_MINUTE_ARTIFACT_RE.fullmatch(candidate)
    if match is None:
        return candidate

    number = match.group("number")
    if number.isdecimal():
        if not _numeric_minute_value_is_safe(number, now_local):
            return candidate
    elif _parse_russian_number_words(number) is None:
        return candidate

    body = match.group("body")
    if not body.strip():
        return candidate

    return f"напомни через {number} минуты{match.group('separator')}{body}"


def _voice_parse_candidates(transcript: str, *, now_local: datetime) -> list[str]:
    normalized = transcript.strip()
    canonical = _canonicalize_voice_command_prefix(normalized)
    if canonical is not None:
        return [_normalize_whisper_minute_artifact(canonical, now_local=now_local)]
    if normalized.lower().startswith("/remind"):
        return [normalized]
    return [f"напомни {normalized}"]


def parse_voice_transcript(
    transcript: str,
    *,
    now_local: datetime,
) -> tuple[str, ParsedReminder | DeadlineRequest | ClarificationRequest | None]:
    """Use the deterministic parser; adding the command prefix is explicit and bounded."""

    for candidate in _voice_parse_candidates(transcript, now_local=now_local):
        parsed = parse_reminder_input(candidate, now_local=now_local)
        if parsed is not None:
            return candidate, parsed
    return transcript.strip(), None
