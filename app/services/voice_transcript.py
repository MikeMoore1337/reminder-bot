from __future__ import annotations

import re
from datetime import datetime

from app.services.reminder_parser import (
    ClarificationRequest,
    DeadlineRequest,
    ParsedReminder,
    parse_reminder_input,
)

_VOICE_COMMAND_PREFIX_RE = re.compile(
    r"^(?:напомни|напомню|напомнить)(?:(?:\s*[,.:;—-]\s+|\s+)(.*))?$",
    re.IGNORECASE,
)


def _canonicalize_voice_command_prefix(transcript: str) -> str | None:
    match = _VOICE_COMMAND_PREFIX_RE.fullmatch(transcript)
    if match is None:
        return None
    remainder = (match.group(1) or "").strip()
    return f"напомни {remainder}" if remainder else "напомни"


def _voice_parse_candidates(transcript: str) -> list[str]:
    normalized = transcript.strip()
    canonical = _canonicalize_voice_command_prefix(normalized)
    if canonical is not None:
        return [canonical]
    if normalized.lower().startswith("/remind"):
        return [normalized]
    return [f"напомни {normalized}"]


def parse_voice_transcript(
    transcript: str,
    *,
    now_local: datetime,
) -> tuple[str, ParsedReminder | DeadlineRequest | ClarificationRequest | None]:
    """Use the deterministic parser; adding the command prefix is explicit and bounded."""

    for candidate in _voice_parse_candidates(transcript):
        parsed = parse_reminder_input(candidate, now_local=now_local)
        if parsed is not None:
            return candidate, parsed
    return transcript.strip(), None
