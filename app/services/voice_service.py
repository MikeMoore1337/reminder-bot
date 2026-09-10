from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Literal

from aiogram import Bot
from aiogram.types import Voice
from sqlalchemy import delete, select

from app.config import get_settings
from app.db.models import (
    ActionDraft,
    RecurrenceType,
    Reminder,
    ReminderClarification,
    User,
    VoiceReminderDraft,
)
from app.db.session import SessionLocal
from app.services.clarification_service import (
    CLARIFICATION_ORIGIN_VOICE,
    create_clarification,
    upsert_clarification_in_session,
)
from app.services.recurrence import decode_rule, encode_rule, legacy_rule
from app.services.reminder_parser import (
    ClarificationRequest,
    DeadlineRequest,
    ParsedReminder,
    parse_reminder_input,
)
from app.services.reminder_service import (
    MAX_REMINDER_TEXT_LENGTH,
    calculate_next_occurrence,
    create_reminder_in_session,
    validate_recurrence,
)
from app.services.speech_to_text import (
    SpeechToTextError,
    SpeechToTextProvider,
    WhisperCppSpeechToTextProvider,
    normalize_transcript,
)
from app.services.voice_media import (
    VoiceMediaError,
    VoiceMediaLimits,
    cleanup_orphaned_voice_temp_dirs,
    convert_voice_to_wav,
    download_voice,
    new_voice_temp_dir,
    validate_voice_metadata,
)
from app.utils.datetime_utils import from_utc_to_user, resolve_schedule_datetime, utc_now

logger = logging.getLogger(__name__)
settings = get_settings()

VOICE_PARSE_FAILURE_MESSAGE = (
    "Не смог разобрать расшифровку в напоминание. Повтори голосовое с датой, временем "
    "и текстом, например: «напомни завтра в 9 позвонить»."
)
VOICE_FLOW_BUSY_MESSAGE = (
    "Сейчас уже обрабатывается другое голосовое сообщение. Попробуй через несколько секунд."
)
VOICE_DRAFT_TTL = timedelta(minutes=15)
VOICE_PREVIEW_TRANSCRIPT_LIMIT = 1200
VOICE_PREVIEW_TEXT_LIMIT = 1200
VOICE_PREVIEW_RECURRENCE_LIMIT = 160
VOICE_PREVIEW_TIMEZONE_LIMIT = 128
VOICE_CLARIFICATION_TRANSCRIPT_LIMIT = 1400
VOICE_CLARIFICATION_PROMPT_LIMIT = 1000
VOICE_CLARIFICATION_TTL_SUFFIX = "Черновик действует 15 минут. /cancel отменит его."
VOICE_CORRECTION_EXAMPLE = "напомни через 2 минуты покормить собаку"
_VOICE_COMMAND_PREFIX_RE = re.compile(
    r"^(?:напомни|напомню|напомнить)(?:(?:\s*[,.:;—-]\s+|\s+)(.*))?$",
    re.IGNORECASE,
)


def _stt_public_message(category: str) -> str:
    return {
        "timeout": "Локальная расшифровка заняла слишком много времени.",
        "crashed": "Локальная расшифровка завершилась с ошибкой.",
        "malformed_output": "Не удалось получить текст из голосового сообщения.",
        "output_limit": "Расшифровка голосового сообщения слишком длинная.",
    }.get(category, "Локальная расшифровка сейчас недоступна.")


@dataclass
class VoiceMetrics:
    download_success: int = 0
    conversion_success: int = 0
    stt_success: int = 0
    parse_success: int = 0
    parse_clarification: int = 0
    parse_correction: int = 0
    confirmation_success: int = 0
    confirmation_stale: int = 0
    cancellation_success: int = 0
    cleanup_success: int = 0
    cleanup_failure: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    stt_latency_buckets: dict[str, int] = field(
        default_factory=lambda: {
            "lt_1s": 0,
            "lt_5s": 0,
            "lt_15s": 0,
            "lt_30s": 0,
            "gte_30s": 0,
        }
    )

    def failure(self, stage: str, category: str) -> None:
        key = f"{stage}:{category}"
        self.failure_counts[key] = self.failure_counts.get(key, 0) + 1

    def stt_latency(self, elapsed_seconds: float) -> None:
        if elapsed_seconds < 1:
            bucket = "lt_1s"
        elif elapsed_seconds < 5:
            bucket = "lt_5s"
        elif elapsed_seconds < 15:
            bucket = "lt_15s"
        elif elapsed_seconds < 30:
            bucket = "lt_30s"
        else:
            bucket = "gte_30s"
        self.stt_latency_buckets[bucket] += 1

    def snapshot(self) -> dict[str, int]:
        result = {
            "download_success": self.download_success,
            "conversion_success": self.conversion_success,
            "stt_success": self.stt_success,
            "parse_success": self.parse_success,
            "parse_clarification": self.parse_clarification,
            "parse_correction": self.parse_correction,
            "confirmation_success": self.confirmation_success,
            "confirmation_stale": self.confirmation_stale,
            "cancellation_success": self.cancellation_success,
            "cleanup_success": self.cleanup_success,
            "cleanup_failure": self.cleanup_failure,
        }
        for key, count in self.failure_counts.items():
            stage, category = key.split(":", 1)
            result[f"failure_{stage}_{category}"] = count
        result.update(
            {f"stt_latency_{bucket}": count for bucket, count in self.stt_latency_buckets.items()}
        )
        return result


voice_metrics = VoiceMetrics()
_stt_semaphore: asyncio.Semaphore | None = None
_stt_semaphore_limit: int | None = None


class VoiceProcessingError(RuntimeError):
    """Safe public error for the voice input boundary."""

    def __init__(self, category: str, public_message: str) -> None:
        super().__init__(public_message)
        self.category = category
        self.public_message = public_message


@dataclass(frozen=True, slots=True)
class VoiceProcessResult:
    draft: VoiceReminderDraft | None = None
    message: str | None = None


def get_voice_metrics() -> dict[str, int]:
    return voice_metrics.snapshot()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _escape_bounded(value: str, *, max_encoded_length: int) -> str:
    """Escape user text and keep the resulting HTML within a fixed bound."""

    normalized = value.strip()
    if not normalized:
        return "—"
    escaped = escape(normalized)
    if len(escaped) <= max_encoded_length:
        return escaped

    suffix = "…"
    if max_encoded_length <= len(suffix):
        return suffix[:max_encoded_length]

    low = 0
    high = len(normalized)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate = escape(normalized[:middle]) + suffix
        if len(candidate) <= max_encoded_length:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best or suffix


def format_voice_clarification_prompt(
    transcript: str,
    parser_prompt: str | None = None,
) -> str:
    """Render a private, bounded correction prompt for a voice attempt."""

    prompt = (parser_prompt or "Не удалось уверенно разобрать расписание.").strip()
    if prompt.endswith(VOICE_CLARIFICATION_TTL_SUFFIX):
        prompt = prompt[: -len(VOICE_CLARIFICATION_TTL_SUFFIX)].rstrip()
    return (
        "🎙 <b>Я распознал голосовое так:</b>\n"
        f"{_escape_bounded(transcript, max_encoded_length=VOICE_CLARIFICATION_TRANSCRIPT_LIMIT)}\n\n"
        f"{_escape_bounded(prompt, max_encoded_length=VOICE_CLARIFICATION_PROMPT_LIMIT)}\n\n"
        "Отправь исправленную полную команду одним сообщением.\n"
        f"Например: <code>{escape(VOICE_CORRECTION_EXAMPLE)}</code>\n\n"
        f"{VOICE_CLARIFICATION_TTL_SUFFIX}"
    )


def _voice_clarification_request(
    request: ClarificationRequest,
    transcript: str,
) -> ClarificationRequest:
    return ClarificationRequest(
        kind=request.kind,
        prompt=format_voice_clarification_prompt(transcript, request.prompt),
        raw_text=request.raw_text[:4096],
        mode=request.mode,
    )


def _unsupported_voice_clarification_request(
    candidate: str,
    transcript: str,
) -> ClarificationRequest:
    return ClarificationRequest(
        kind="unsupported",
        prompt=format_voice_clarification_prompt(transcript),
        raw_text=candidate[:4096],
    )


def _voice_limits() -> VoiceMediaLimits:
    return VoiceMediaLimits(
        max_file_size_bytes=int(getattr(settings, "voice_max_file_size_bytes", 10_000_000)),
        max_duration_seconds=int(getattr(settings, "voice_max_duration_seconds", 120)),
        download_timeout_seconds=int(getattr(settings, "voice_download_timeout_seconds", 30)),
        conversion_timeout_seconds=int(getattr(settings, "voice_conversion_timeout_seconds", 30)),
        conversion_command=str(getattr(settings, "voice_conversion_command", "ffmpeg")),
    )


def _default_provider() -> WhisperCppSpeechToTextProvider:
    return WhisperCppSpeechToTextProvider(
        command=str(getattr(settings, "voice_stt_command", "whisper-cli")),
        model_path=getattr(settings, "voice_stt_model_path", None),
        language=str(getattr(settings, "voice_stt_language", "ru")),
        threads=int(getattr(settings, "voice_stt_threads", 2)),
        timeout_seconds=int(getattr(settings, "voice_stt_timeout_seconds", 90)),
    )


def _get_stt_semaphore() -> asyncio.Semaphore:
    global _stt_semaphore, _stt_semaphore_limit
    limit = int(getattr(settings, "voice_stt_max_concurrent_jobs", 1))
    if _stt_semaphore is None or _stt_semaphore_limit != limit:
        _stt_semaphore = asyncio.Semaphore(limit)
        _stt_semaphore_limit = limit
    return _stt_semaphore


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


def _prepare_draft_values(
    user: User,
    parsed: ParsedReminder,
    *,
    now_utc: datetime,
) -> tuple[datetime, str, str, int, int | None, str | None, str]:
    reminder_text = parsed.text.strip()
    if not reminder_text or len(reminder_text) > MAX_REMINDER_TEXT_LENGTH:
        raise ValueError("Текст напоминания должен содержать от 1 до 4096 символов")

    recurrence_type: str = str(parsed.recurrence_type)
    recurrence_interval: int = int(parsed.recurrence_interval)
    recurrence_day_of_month: int | None = parsed.recurrence_day_of_month
    canonical_rule: dict[str, Any] | None = None
    if parsed.recurrence_rule is not None:
        canonical_rule = decode_rule(parsed.recurrence_rule)
        if canonical_rule is None:
            raise ValueError("Правило повторения отсутствует")
        if canonical_rule["kind"] == "legacy":
            recurrence_type = str(canonical_rule["recurrence_type"])
            recurrence_interval = int(canonical_rule["interval"])
            if recurrence_day_of_month is None:
                recurrence_day_of_month = canonical_rule.get("day_of_month")
        else:
            recurrence_type = RecurrenceType.ADVANCED.value
            recurrence_interval = int(canonical_rule.get("interval", 1))
            recurrence_day_of_month = None
    elif recurrence_type == RecurrenceType.ADVANCED.value:
        raise ValueError("Для advanced recurrence требуется каноническое правило")

    validate_recurrence(recurrence_type, recurrence_interval)
    if canonical_rule is None and recurrence_type != RecurrenceType.NONE.value:
        canonical_rule = legacy_rule(
            recurrence_type,
            recurrence_interval,
            recurrence_day_of_month,
        )

    initial_at_utc = _as_utc(
        resolve_schedule_datetime(
            parsed.local_dt,
            user.timezone,
            semantics=parsed.datetime_semantics,
        )
    )
    current_time = _as_utc(now_utc)
    if recurrence_type == RecurrenceType.NONE.value:
        if initial_at_utc <= current_time:
            raise ValueError("Время напоминания уже прошло")
    else:
        steps = 0
        while initial_at_utc <= current_time:
            steps += 1
            if steps > 10_000:
                raise ValueError("Не удалось вычислить следующее повторение")
            next_dt = calculate_next_occurrence(
                initial_at_utc,
                recurrence_type,
                recurrence_interval,
                timezone_name=user.timezone,
                recurrence_day_of_month=recurrence_day_of_month,
                recurrence_rule=canonical_rule,
            )
            if next_dt is None:
                raise ValueError("Первое повторение должно попадать в правило")
            initial_at_utc = _as_utc(next_dt)

    return (
        initial_at_utc,
        reminder_text,
        recurrence_type,
        recurrence_interval,
        recurrence_day_of_month,
        encode_rule(canonical_rule) if canonical_rule is not None else None,
        parsed.mode,
    )


async def create_voice_draft(
    user: User,
    transcript: str,
    parsed: ParsedReminder,
    *,
    source_message_id: int | None = None,
    now_utc: datetime | None = None,
) -> VoiceReminderDraft:
    current_time = _as_utc(now_utc or utc_now())
    if not transcript.strip() or len(transcript) > 4096:
        raise ValueError("Расшифровка голосового сообщения слишком длинная")
    (
        initial_at_utc,
        reminder_text,
        recurrence_type,
        recurrence_interval,
        recurrence_day_of_month,
        recurrence_rule,
        mode,
    ) = _prepare_draft_values(user, parsed, now_utc=current_time)
    expiry = current_time + timedelta(
        seconds=int(getattr(settings, "voice_draft_ttl_seconds", VOICE_DRAFT_TTL.total_seconds()))
    )
    normalized_source_id = (
        source_message_id if source_message_id and source_message_id > 0 else None
    )

    async with SessionLocal() as session, session.begin():
        owner = await session.scalar(
            select(User).where(User.id == user.id, User.chat_id == user.chat_id).with_for_update()
        )
        if owner is None:
            raise ValueError("Пользователь не найден")

        await session.execute(
            delete(ActionDraft).where(
                ActionDraft.user_id == owner.id,
                ActionDraft.chat_id == owner.chat_id,
            )
        )
        await session.execute(
            delete(ReminderClarification).where(
                ReminderClarification.user_id == owner.id,
                ReminderClarification.chat_id == owner.chat_id,
            )
        )
        existing = await session.scalar(
            select(VoiceReminderDraft)
            .where(
                VoiceReminderDraft.user_id == owner.id,
                VoiceReminderDraft.chat_id == owner.chat_id,
            )
            .with_for_update()
        )
        if existing is not None:
            if (
                normalized_source_id is not None
                and existing.source_message_id == normalized_source_id
                and _as_utc(existing.expires_at) > current_time
            ):
                return existing
            await session.delete(existing)
            await session.flush()

        draft = VoiceReminderDraft(
            user_id=owner.id,
            chat_id=owner.chat_id,
            source_message_id=normalized_source_id,
            transcript=transcript.strip(),
            reminder_text=reminder_text,
            remind_at_utc=initial_at_utc,
            schedule_timezone=owner.timezone,
            datetime_semantics=parsed.datetime_semantics,
            recurrence_type=recurrence_type,
            recurrence_interval=recurrence_interval,
            recurrence_day_of_month=recurrence_day_of_month,
            recurrence_rule=recurrence_rule,
            mode=mode,
            action_revision=1,
            expires_at=expiry,
        )
        session.add(draft)
        await session.flush()
        return draft


async def consume_voice_clarification_to_draft(
    user: User,
    clarification_id: int,
    raw_text: str,
    parsed: ParsedReminder,
    *,
    now_utc: datetime | None = None,
) -> VoiceReminderDraft | None:
    """Turn one exact voice clarification into a confirmation-gated draft."""

    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        owner = await session.scalar(
            select(User).where(User.id == user.id, User.chat_id == user.chat_id).with_for_update()
        )
        if owner is None:
            raise ValueError("Пользователь не найден")

        clarification = await session.scalar(
            select(ReminderClarification)
            .where(
                ReminderClarification.id == clarification_id,
                ReminderClarification.user_id == owner.id,
                ReminderClarification.chat_id == owner.chat_id,
                ReminderClarification.origin == CLARIFICATION_ORIGIN_VOICE,
                ReminderClarification.raw_text == raw_text,
            )
            .with_for_update()
        )
        if clarification is None:
            return None
        if _as_utc(clarification.expires_at) <= current_time:
            await session.delete(clarification)
            return None

        (
            initial_at_utc,
            reminder_text,
            recurrence_type,
            recurrence_interval,
            recurrence_day_of_month,
            recurrence_rule,
            mode,
        ) = _prepare_draft_values(owner, parsed, now_utc=current_time)
        transcript = (clarification.voice_transcript or clarification.raw_text).strip()
        if not transcript:
            raise ValueError("Расшифровка голосового сообщения отсутствует")

        await session.execute(
            delete(ActionDraft).where(
                ActionDraft.user_id == owner.id,
                ActionDraft.chat_id == owner.chat_id,
            )
        )
        existing = await session.scalar(
            select(VoiceReminderDraft)
            .where(
                VoiceReminderDraft.user_id == owner.id,
                VoiceReminderDraft.chat_id == owner.chat_id,
            )
            .with_for_update()
        )
        if existing is not None:
            await session.delete(existing)
            await session.flush()

        expiry = current_time + timedelta(
            seconds=int(
                getattr(settings, "voice_draft_ttl_seconds", VOICE_DRAFT_TTL.total_seconds())
            )
        )
        draft = VoiceReminderDraft(
            user_id=owner.id,
            chat_id=owner.chat_id,
            source_message_id=clarification.source_message_id,
            transcript=transcript,
            reminder_text=reminder_text,
            remind_at_utc=initial_at_utc,
            schedule_timezone=owner.timezone,
            datetime_semantics=parsed.datetime_semantics,
            recurrence_type=recurrence_type,
            recurrence_interval=recurrence_interval,
            recurrence_day_of_month=recurrence_day_of_month,
            recurrence_rule=recurrence_rule,
            mode=mode,
            action_revision=1,
            expires_at=expiry,
        )
        session.add(draft)
        await session.delete(clarification)
        await session.flush()
        voice_metrics.parse_correction += 1
        return draft


async def get_active_voice_draft(
    user: User,
    *,
    now_utc: datetime | None = None,
) -> VoiceReminderDraft | None:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        draft = await session.scalar(
            select(VoiceReminderDraft)
            .where(
                VoiceReminderDraft.user_id == user.id,
                VoiceReminderDraft.chat_id == user.chat_id,
            )
            .with_for_update()
        )
        if draft is None:
            return None
        if _as_utc(draft.expires_at) <= current_time:
            await session.delete(draft)
            return None
        return draft


async def bind_voice_preview_message(
    user: User,
    draft_id: int,
    *,
    revision: int,
    message_id: int,
    now_utc: datetime | None = None,
) -> bool:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        draft = await session.scalar(
            select(VoiceReminderDraft)
            .where(
                VoiceReminderDraft.id == draft_id,
                VoiceReminderDraft.user_id == user.id,
                VoiceReminderDraft.chat_id == user.chat_id,
                VoiceReminderDraft.action_revision == revision,
            )
            .with_for_update()
        )
        if draft is None or _as_utc(draft.expires_at) <= current_time:
            return False
        draft.preview_message_id = message_id
        return True


async def start_voice_draft_correction(
    user: User,
    draft_id: int,
    *,
    expected_revision: int,
    expected_message_id: int,
    now_utc: datetime | None = None,
) -> ReminderClarification | None:
    """Atomically replace one validated voice draft with voice clarification."""

    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        owner = await session.scalar(
            select(User).where(User.id == user.id, User.chat_id == user.chat_id).with_for_update()
        )
        if owner is None:
            return None

        draft = await session.scalar(
            select(VoiceReminderDraft)
            .where(
                VoiceReminderDraft.id == draft_id,
                VoiceReminderDraft.user_id == owner.id,
                VoiceReminderDraft.chat_id == owner.chat_id,
                VoiceReminderDraft.action_revision == expected_revision,
            )
            .with_for_update()
        )
        if draft is None:
            return None
        if _as_utc(draft.expires_at) <= current_time:
            await session.delete(draft)
            return None
        if draft.preview_message_id is None or draft.preview_message_id != expected_message_id:
            return None

        transcript = draft.transcript.strip()
        if not transcript:
            return None
        source_message_id = draft.source_message_id
        request = ClarificationRequest(
            kind="voice_correction",
            prompt=format_voice_clarification_prompt(transcript),
            raw_text=transcript[:4096],
            mode=draft.mode,
        )
        await session.execute(
            delete(ActionDraft).where(
                ActionDraft.user_id == owner.id,
                ActionDraft.chat_id == owner.chat_id,
            )
        )
        await session.delete(draft)
        await session.flush()
        return await upsert_clarification_in_session(
            session,
            owner,
            request,
            now_utc=current_time,
            origin=CLARIFICATION_ORIGIN_VOICE,
            voice_transcript=transcript,
            source_message_id=source_message_id,
            clear_action_draft=False,
            clear_voice_draft=False,
        )


async def confirm_voice_draft(
    user: User,
    draft_id: int,
    *,
    expected_revision: int,
    expected_message_id: int | None = None,
    now_utc: datetime | None = None,
) -> Reminder | None:
    """Atomically validate, create, and consume one voice draft."""

    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        owner = await session.scalar(
            select(User).where(User.id == user.id, User.chat_id == user.chat_id).with_for_update()
        )
        if owner is None:
            voice_metrics.confirmation_stale += 1
            return None
        draft = await session.scalar(
            select(VoiceReminderDraft)
            .where(
                VoiceReminderDraft.id == draft_id,
                VoiceReminderDraft.user_id == owner.id,
                VoiceReminderDraft.chat_id == owner.chat_id,
                VoiceReminderDraft.action_revision == expected_revision,
            )
            .with_for_update()
        )
        if draft is None:
            voice_metrics.confirmation_stale += 1
            return None
        if _as_utc(draft.expires_at) <= current_time:
            await session.delete(draft)
            voice_metrics.confirmation_stale += 1
            return None
        if draft.preview_message_id is not None and draft.preview_message_id != expected_message_id:
            voice_metrics.confirmation_stale += 1
            return None

        if draft.datetime_semantics == "instant":
            local_dt = _as_utc(draft.remind_at_utc)
            datetime_semantics: Literal["wall_clock", "instant"] = "instant"
        elif draft.datetime_semantics == "wall_clock":
            local_dt = from_utc_to_user(draft.remind_at_utc, draft.schedule_timezone).replace(
                tzinfo=None
            )
            datetime_semantics = "wall_clock"
        else:
            raise ValueError("У голосового черновика некорректная семантика времени")
        reminder = await create_reminder_in_session(
            session,
            owner,
            local_dt,
            draft.reminder_text,
            draft.recurrence_type,
            draft.recurrence_interval,
            datetime_semantics,
            draft.recurrence_rule,
            draft.recurrence_day_of_month,
            now_utc=current_time,
            schedule_timezone=draft.schedule_timezone,
            mode=draft.mode,
        )
        await session.delete(draft)
        await session.flush()
        voice_metrics.confirmation_success += 1
        return reminder


async def discard_voice_draft(
    user: User,
    draft_id: int,
    *,
    expected_revision: int,
    expected_message_id: int | None = None,
    now_utc: datetime | None = None,
) -> bool:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        draft = await session.scalar(
            select(VoiceReminderDraft)
            .where(
                VoiceReminderDraft.id == draft_id,
                VoiceReminderDraft.user_id == user.id,
                VoiceReminderDraft.chat_id == user.chat_id,
                VoiceReminderDraft.action_revision == expected_revision,
            )
            .with_for_update()
        )
        if draft is None or (
            draft.preview_message_id is not None and draft.preview_message_id != expected_message_id
        ):
            return False
        if _as_utc(draft.expires_at) <= current_time:
            await session.delete(draft)
            return False
        await session.delete(draft)
        voice_metrics.cancellation_success += 1
        return True


async def cancel_voice_reminder_draft(user: User) -> bool:
    async with SessionLocal() as session, session.begin():
        result = await session.execute(
            delete(VoiceReminderDraft).where(
                VoiceReminderDraft.user_id == user.id,
                VoiceReminderDraft.chat_id == user.chat_id,
            )
        )
        deleted = bool(getattr(result, "rowcount", 0))
        if deleted:
            voice_metrics.cancellation_success += 1
        return deleted


async def cleanup_expired_voice_drafts(*, now_utc: datetime | None = None) -> int:
    current_time = _as_utc(now_utc or utc_now())
    try:
        async with SessionLocal() as session, session.begin():
            result = await session.execute(
                delete(VoiceReminderDraft).where(VoiceReminderDraft.expires_at <= current_time)
            )
            deleted = int(getattr(result, "rowcount", 0) or 0)
        orphaned = await asyncio.to_thread(
            cleanup_orphaned_voice_temp_dirs,
            getattr(settings, "voice_temp_dir", None),
            max_age_seconds=int(
                getattr(settings, "voice_draft_ttl_seconds", VOICE_DRAFT_TTL.total_seconds())
            ),
            now_epoch=current_time.timestamp(),
        )
        voice_metrics.cleanup_success += deleted + orphaned
        return deleted + orphaned
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        voice_metrics.cleanup_failure += 1
        logger.warning(
            "Voice draft cleanup failed",
            extra={"extra_data": f"error_type={type(exc).__name__[:80]}"},
        )
        return 0


def format_voice_draft_preview(draft: VoiceReminderDraft) -> str:
    local_dt = from_utc_to_user(draft.remind_at_utc, draft.schedule_timezone)
    if draft.recurrence_type == RecurrenceType.NONE.value:
        recurrence = "нет"
    elif draft.recurrence_rule:
        try:
            rule = decode_rule(draft.recurrence_rule)
            recurrence = str(rule.get("kind", draft.recurrence_type)) if rule else "неизвестно"
        except ValueError:
            recurrence = "неизвестно"
    else:
        recurrence = f"{draft.recurrence_type}, интервал {draft.recurrence_interval}"
    return (
        "🎙 <b>Проверь голосовое напоминание</b>\n\n"
        f"<b>Распознано голосом:</b> {_escape_bounded(draft.transcript, max_encoded_length=VOICE_PREVIEW_TRANSCRIPT_LIMIT)}\n"
        f"<b>Текст:</b> {_escape_bounded(draft.reminder_text, max_encoded_length=VOICE_PREVIEW_TEXT_LIMIT)}\n"
        f"<b>Когда:</b> {local_dt.strftime('%d.%m.%Y %H:%M')}\n"
        f"<b>Повтор:</b> {_escape_bounded(recurrence, max_encoded_length=VOICE_PREVIEW_RECURRENCE_LIMIT)}\n"
        f"<b>Режим:</b> {'важное (с повтором)' if draft.mode == 'persistent' else 'обычное'}\n"
        f"<b>Часовой пояс:</b> <code>{_escape_bounded(draft.schedule_timezone, max_encoded_length=VOICE_PREVIEW_TIMEZONE_LIMIT)}</code>\n\n"
        "Нажми «Создать», чтобы сохранить напоминание, «Исправить» — чтобы изменить команду, "
        "или «Отмена»."
    )


async def process_voice_message(
    bot: Bot,
    user: User,
    voice: Voice,
    *,
    source_message_id: int | None = None,
    provider: SpeechToTextProvider | None = None,
    now_utc: datetime | None = None,
) -> VoiceProcessResult:
    limits = _voice_limits()
    try:
        validate_voice_metadata(voice, limits)
    except VoiceMediaError as exc:
        voice_metrics.failure("validation", exc.category)
        raise VoiceProcessingError(exc.category, exc.public_message) from exc

    active_provider = provider or _default_provider()
    is_available = getattr(active_provider, "is_available", None)
    if callable(is_available) and not is_available():
        voice_metrics.failure("stt", "unavailable")
        raise VoiceProcessingError("unavailable", "Локальная расшифровка сейчас недоступна.")

    semaphore = _get_stt_semaphore()
    try:
        await asyncio.wait_for(
            semaphore.acquire(),
            timeout=int(getattr(settings, "voice_stt_queue_timeout_seconds", 5)),
        )
    except TimeoutError as exc:
        voice_metrics.failure("concurrency", "busy")
        raise VoiceProcessingError("busy", VOICE_FLOW_BUSY_MESSAGE) from exc

    temp_dir: Path | None = None
    current_time = _as_utc(now_utc or utc_now())
    try:
        try:
            temp_dir = new_voice_temp_dir(getattr(settings, "voice_temp_dir", None))
            source = temp_dir / "input.ogg"
            wav = temp_dir / "normalized.wav"
            await download_voice(bot, voice, source, limits)
            voice_metrics.download_success += 1
            await convert_voice_to_wav(source, wav, limits)
            voice_metrics.conversion_success += 1
        except VoiceMediaError as exc:
            voice_metrics.failure("media", exc.category)
            raise VoiceProcessingError(exc.category, exc.public_message) from exc

        started = time.monotonic()
        try:
            transcript = normalize_transcript(await active_provider.transcribe(wav))
        except SpeechToTextError as exc:
            voice_metrics.failure("stt", exc.category)
            raise VoiceProcessingError(exc.category, _stt_public_message(exc.category)) from exc
        except asyncio.CancelledError:
            voice_metrics.failure("stt", "cancelled")
            raise
        except Exception as exc:
            voice_metrics.failure("stt", "crashed")
            raise VoiceProcessingError(
                "crashed", "Локальная расшифровка завершилась с ошибкой."
            ) from exc
        else:
            voice_metrics.stt_success += 1
            voice_metrics.stt_latency(time.monotonic() - started)

        candidate, parsed = parse_voice_transcript(
            transcript,
            now_local=from_utc_to_user(current_time, user.timezone),
        )
        if isinstance(parsed, ClarificationRequest):
            voice_request = _voice_clarification_request(parsed, transcript)
            await create_clarification(
                user,
                voice_request,
                now_utc=current_time,
                origin=CLARIFICATION_ORIGIN_VOICE,
                voice_transcript=transcript,
                source_message_id=source_message_id,
            )
            voice_metrics.parse_clarification += 1
            return VoiceProcessResult(message=voice_request.prompt)
        if not isinstance(parsed, ParsedReminder):
            voice_request = _unsupported_voice_clarification_request(candidate, transcript)
            await create_clarification(
                user,
                voice_request,
                now_utc=current_time,
                origin=CLARIFICATION_ORIGIN_VOICE,
                voice_transcript=transcript,
                source_message_id=source_message_id,
            )
            voice_metrics.failure("parse", "unsupported")
            voice_metrics.parse_clarification += 1
            return VoiceProcessResult(message=voice_request.prompt)

        try:
            draft = await create_voice_draft(
                user,
                transcript,
                parsed,
                source_message_id=source_message_id,
                now_utc=current_time,
            )
        except ValueError as exc:
            voice_metrics.failure("parse", "validation")
            raise VoiceProcessingError(
                "parse", "Не удалось подготовить напоминание из этой расшифровки."
            ) from exc
        voice_metrics.parse_success += 1
        logger.info(
            "Voice reminder draft created",
            extra={
                "extra_data": (
                    f"transcript_length={len(transcript)} "
                    f"candidate_prefixed={candidate != transcript.strip()}"
                )
            },
        )
        return VoiceProcessResult(draft=draft)
    finally:
        if temp_dir is not None:
            try:
                shutil.rmtree(temp_dir)
            except OSError as exc:
                voice_metrics.cleanup_failure += 1
                logger.warning(
                    "Voice temporary media cleanup failed",
                    extra={"extra_data": f"error_type={type(exc).__name__[:80]}"},
                )
            else:
                voice_metrics.cleanup_success += 1
        semaphore.release()
