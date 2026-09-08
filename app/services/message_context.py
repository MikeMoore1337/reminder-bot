from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from aiogram.types import Message
from sqlalchemy import delete, func, select

from app.db.models import Reminder, ReminderContext
from app.db.session import SessionLocal
from app.services.reminder_parser import (
    ClarificationRequest,
    ParsedReminder,
    parse_clarification_answer,
    parse_reminder_input,
)
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

MAX_CONTEXT_TEXT_LENGTH = 1200
MAX_CONTEXT_CAPTION_LENGTH = 1200
MAX_CONTEXT_URL_LENGTH = 2048
MAX_CONTEXT_SENDER_LABEL_LENGTH = 128
MAX_CONTEXT_CHAT_USERNAME_LENGTH = 64
MAX_CONTEXT_MEDIA_FILE_ID_LENGTH = 256
MAX_CONTEXT_MEDIA_FILE_NAME_LENGTH = 256
MAX_CONTEXT_MEDIA_MIME_LENGTH = 128
MAX_CONTEXT_SNAPSHOT_BYTES = 16_384
MAX_CONTEXT_DELIVERY_LENGTH = 2400
MAX_CONTEXT_RETENTION = timedelta(days=30)
MAX_TELEGRAM_IDENTIFIER = 10**18
MAX_CONTEXT_MEDIA_SIZE = 100_000_000
CONTEXT_PLACEHOLDER = "__telegram_context__"

_URL_RE = re.compile(r"(?P<url>(?:https?://|www\.)[^\s<>]+)", re.IGNORECASE)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_TIME_ONLY_RE = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})$")
_CONTEXT_MARKER_RE = re.compile(r"\s+об\s+этом\s*$", re.IGNORECASE)
_CONTEXT_PREFIX_RE = re.compile(
    r"^(?P<prefix>напомни|/remind)\s+об\s+этом(?:\s+(?P<tail>.*))?$",
    re.IGNORECASE,
)
_COMMAND_PREFIX_RE = re.compile(r"^(?:напомни|/remind)(?:\s|$)", re.IGNORECASE)


class ContextKind(StrEnum):
    FORWARDED = "forwarded"
    CHANNEL_POST = "channel_post"
    REPLY = "reply"
    LINK = "link"
    PHOTO = "photo"
    DOCUMENT = "document"
    ORDINARY = "ordinary"


CONTEXT_KIND_LABELS = {
    ContextKind.FORWARDED.value: "пересланное сообщение",
    ContextKind.CHANNEL_POST.value: "пост канала",
    ContextKind.REPLY.value: "сообщение в ответе",
    ContextKind.LINK.value: "ссылка",
    ContextKind.PHOTO.value: "фото",
    ContextKind.DOCUMENT.value: "документ",
    ContextKind.ORDINARY.value: "сообщение",
}


@dataclass(frozen=True, slots=True)
class MessageContextSnapshot:
    """Small provider-neutral snapshot/reference of one Telegram message."""

    kind: str
    source_chat_id: int | None = None
    source_chat_username: str | None = None
    source_message_id: int | None = None
    source_thread_id: int | None = None
    source_sender_label: str | None = None
    source_text: str | None = None
    source_caption: str | None = None
    source_url: str | None = None
    media_kind: str | None = None
    media_file_id: str | None = None
    media_file_name: str | None = None
    media_mime_type: str | None = None
    media_size: int | None = None
    source_date_utc: datetime | None = None


@dataclass
class ContextMetrics:
    extracted_by_kind: dict[str, int] = field(default_factory=dict)
    delivery_fallback_by_kind: dict[str, int] = field(default_factory=dict)
    cleanup_by_kind: dict[str, int] = field(default_factory=dict)
    cleanup_failures: int = 0

    def _record(self, values: dict[str, int], kind: str, count: int = 1) -> None:
        values[kind] = values.get(kind, 0) + count

    def extracted(self, kind: str) -> None:
        self._record(self.extracted_by_kind, kind)

    def delivery_fallback(self, kind: str) -> None:
        self._record(self.delivery_fallback_by_kind, kind)

    def cleanup(self, kind: str, count: int) -> None:
        self._record(self.cleanup_by_kind, kind, count)

    def snapshot(self) -> dict[str, int]:
        result = {
            f"context_extracted_{kind}": count for kind, count in self.extracted_by_kind.items()
        }
        result.update(
            {
                f"context_delivery_fallback_{kind}": count
                for kind, count in self.delivery_fallback_by_kind.items()
            }
        )
        result.update(
            {f"context_cleanup_{kind}": count for kind, count in self.cleanup_by_kind.items()}
        )
        result["context_cleanup_failures"] = self.cleanup_failures
        return result


context_metrics = ContextMetrics()


def get_context_metrics() -> dict[str, int]:
    return context_metrics.snapshot()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    return text[:limit]


def _normalize_id(value: Any, *, positive: bool = False) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if positive and value <= 0:
        return None
    if not positive and abs(value) > MAX_TELEGRAM_IDENTIFIER:
        return None
    return value


def _normalize_username(value: Any) -> str | None:
    normalized = _normalize_text(value, MAX_CONTEXT_CHAT_USERNAME_LENGTH)
    if normalized is None:
        return None
    normalized = normalized.removeprefix("@").strip()
    return normalized if _USERNAME_RE.fullmatch(normalized) else None


def _normalize_url(value: Any) -> str | None:
    normalized = _normalize_text(value, MAX_CONTEXT_URL_LENGTH)
    if normalized is None:
        return None
    normalized = normalized.rstrip(".,!?;:)]}")
    if normalized.lower().startswith("www."):
        normalized = f"https://{normalized}"
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        if not parsed.hostname:
            return None
    except ValueError:
        return None
    return normalized


def normalize_snapshot(snapshot: MessageContextSnapshot) -> MessageContextSnapshot:
    kind = str(snapshot.kind)
    if kind not in {item.value for item in ContextKind}:
        kind = ContextKind.ORDINARY.value
    media_kind = snapshot.media_kind if snapshot.media_kind in {"photo", "document"} else None
    media_size = _normalize_id(snapshot.media_size or 0, positive=True)
    if media_size is not None and media_size > MAX_CONTEXT_MEDIA_SIZE:
        media_size = MAX_CONTEXT_MEDIA_SIZE
    source_date = snapshot.source_date_utc
    if source_date is not None:
        source_date = _as_utc(source_date) if isinstance(source_date, datetime) else None
    return MessageContextSnapshot(
        kind=kind,
        source_chat_id=_normalize_id(snapshot.source_chat_id),
        source_chat_username=_normalize_username(snapshot.source_chat_username),
        source_message_id=_normalize_id(snapshot.source_message_id, positive=True),
        source_thread_id=_normalize_id(snapshot.source_thread_id, positive=True),
        source_sender_label=_normalize_text(
            snapshot.source_sender_label, MAX_CONTEXT_SENDER_LABEL_LENGTH
        ),
        source_text=_normalize_text(snapshot.source_text, MAX_CONTEXT_TEXT_LENGTH),
        source_caption=_normalize_text(snapshot.source_caption, MAX_CONTEXT_CAPTION_LENGTH),
        source_url=_normalize_url(snapshot.source_url),
        media_kind=media_kind,
        media_file_id=_normalize_text(snapshot.media_file_id, MAX_CONTEXT_MEDIA_FILE_ID_LENGTH),
        media_file_name=_normalize_text(
            snapshot.media_file_name, MAX_CONTEXT_MEDIA_FILE_NAME_LENGTH
        ),
        media_mime_type=_normalize_text(snapshot.media_mime_type, MAX_CONTEXT_MEDIA_MIME_LENGTH),
        media_size=media_size,
        source_date_utc=source_date,
    )


def serialize_context_snapshot(snapshot: MessageContextSnapshot) -> str:
    normalized = normalize_snapshot(snapshot)
    payload = {
        key: value
        for key, value in {
            "kind": normalized.kind,
            "source_chat_id": normalized.source_chat_id,
            "source_chat_username": normalized.source_chat_username,
            "source_message_id": normalized.source_message_id,
            "source_thread_id": normalized.source_thread_id,
            "source_sender_label": normalized.source_sender_label,
            "source_text": normalized.source_text,
            "source_caption": normalized.source_caption,
            "source_url": normalized.source_url,
            "media_kind": normalized.media_kind,
            "media_file_id": normalized.media_file_id,
            "media_file_name": normalized.media_file_name,
            "media_mime_type": normalized.media_mime_type,
            "media_size": normalized.media_size,
            "source_date_utc": (
                normalized.source_date_utc.isoformat()
                if normalized.source_date_utc is not None
                else None
            ),
        }.items()
        if value is not None
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_CONTEXT_SNAPSHOT_BYTES:
        raise ValueError("Контекст сообщения слишком длинный")
    return serialized


def deserialize_context_snapshot(value: str | None) -> MessageContextSnapshot | None:
    if not value or len(value.encode("utf-8")) > MAX_CONTEXT_SNAPSHOT_BYTES:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("kind"), str):
        return None
    if payload["kind"] not in {item.value for item in ContextKind}:
        return None
    for key in (
        "source_chat_username",
        "source_sender_label",
        "source_text",
        "source_caption",
        "source_url",
        "media_kind",
        "media_file_id",
        "media_file_name",
        "media_mime_type",
    ):
        if payload.get(key) is not None and not isinstance(payload.get(key), str):
            return None
    for key in (
        "source_chat_id",
        "source_message_id",
        "source_thread_id",
        "media_size",
    ):
        if payload.get(key) is not None and (
            isinstance(payload.get(key), bool) or not isinstance(payload.get(key), int)
        ):
            return None
    source_date = payload.get("source_date_utc")
    parsed_date: datetime | None = None
    if source_date is not None:
        if not isinstance(source_date, str):
            return None
        try:
            parsed_date = datetime.fromisoformat(source_date)
        except ValueError:
            return None
    try:
        snapshot = MessageContextSnapshot(
            kind=payload["kind"],
            source_chat_id=payload.get("source_chat_id"),
            source_chat_username=payload.get("source_chat_username"),
            source_message_id=payload.get("source_message_id"),
            source_thread_id=payload.get("source_thread_id"),
            source_sender_label=payload.get("source_sender_label"),
            source_text=payload.get("source_text"),
            source_caption=payload.get("source_caption"),
            source_url=payload.get("source_url"),
            media_kind=payload.get("media_kind"),
            media_file_id=payload.get("media_file_id"),
            media_file_name=payload.get("media_file_name"),
            media_mime_type=payload.get("media_mime_type"),
            media_size=payload.get("media_size"),
            source_date_utc=parsed_date,
        )
        normalized = normalize_snapshot(snapshot)
        return normalized
    except (TypeError, ValueError, OverflowError):
        return None


def context_expiry(*, now_utc: datetime | None = None) -> datetime:
    return _as_utc(now_utc or utc_now()) + MAX_CONTEXT_RETENTION


def reminder_context_model_kwargs(
    snapshot: MessageContextSnapshot,
    *,
    reminder_id: int,
    user_id: int,
    chat_id: int,
    now_utc: datetime,
) -> dict[str, Any]:
    normalized = normalize_snapshot(snapshot)
    return {
        "reminder_id": reminder_id,
        "user_id": user_id,
        "chat_id": chat_id,
        "kind": normalized.kind,
        "source_chat_id": normalized.source_chat_id,
        "source_chat_username": normalized.source_chat_username,
        "source_message_id": normalized.source_message_id,
        "source_thread_id": normalized.source_thread_id,
        "source_sender_label": normalized.source_sender_label,
        "source_text": normalized.source_text,
        "source_caption": normalized.source_caption,
        "source_url": normalized.source_url,
        "media_kind": normalized.media_kind,
        "media_file_id": normalized.media_file_id,
        "media_file_name": normalized.media_file_name,
        "media_mime_type": normalized.media_mime_type,
        "media_size": normalized.media_size,
        "source_date_utc": normalized.source_date_utc,
        "expires_at": context_expiry(now_utc=now_utc),
    }


def _chat_id(chat: Any) -> int | None:
    return _normalize_id(getattr(chat, "id", None))


def _chat_username(chat: Any) -> str | None:
    return _normalize_username(getattr(chat, "username", None))


def _label(value: Any) -> str | None:
    username = _normalize_username(getattr(value, "username", None))
    if username:
        return f"@{username}"
    title = _normalize_text(getattr(value, "title", None), MAX_CONTEXT_SENDER_LABEL_LENGTH)
    if title:
        return title
    first_name = _normalize_text(getattr(value, "first_name", None), 64)
    last_name = _normalize_text(getattr(value, "last_name", None), 64)
    full_name = " ".join(part for part in (first_name, last_name) if part)
    return _normalize_text(full_name, MAX_CONTEXT_SENDER_LABEL_LENGTH)


def _first_url(value: str | None, entities: Any) -> str | None:
    if not value:
        return None
    for entity in entities or ():
        entity_type = getattr(entity, "type", None)
        candidate: Any = None
        if entity_type == "text_link":
            candidate = getattr(entity, "url", None)
        elif entity_type == "url":
            extractor = getattr(entity, "extract_from", None)
            if callable(extractor):
                candidate = extractor(value)
            else:
                offset = getattr(entity, "offset", None)
                length = getattr(entity, "length", None)
                if isinstance(offset, int) and isinstance(length, int):
                    candidate = value[offset : offset + length]
        normalized = _normalize_url(candidate)
        if normalized:
            return normalized
    match = _URL_RE.search(value)
    return _normalize_url(match.group("url")) if match else None


def _media_fields(message: Any) -> dict[str, Any]:
    photos = getattr(message, "photo", None)
    if photos:
        try:
            photo = max(
                photos,
                key=lambda item: (
                    int(getattr(item, "width", 0) or 0) * int(getattr(item, "height", 0) or 0),
                    int(getattr(item, "file_size", 0) or 0),
                ),
            )
        except (TypeError, ValueError):
            photo = photos[-1]
        return {
            "media_kind": "photo",
            "media_file_id": getattr(photo, "file_id", None),
            "media_file_name": None,
            "media_mime_type": "image/*",
            "media_size": getattr(photo, "file_size", None),
        }
    document = getattr(message, "document", None)
    if document is not None:
        return {
            "media_kind": "document",
            "media_file_id": getattr(document, "file_id", None),
            "media_file_name": getattr(document, "file_name", None),
            "media_mime_type": getattr(document, "mime_type", None),
            "media_size": getattr(document, "file_size", None),
        }
    return {}


def _message_fields(
    source: Any,
    *,
    kind: str,
    source_chat_id: int | None,
    source_chat_username: str | None,
    source_message_id: int | None,
    source_thread_id: int | None = None,
    source_sender_label: str | None = None,
    source_date: datetime | None = None,
) -> MessageContextSnapshot:
    text = getattr(source, "text", None)
    caption = getattr(source, "caption", None)
    media = _media_fields(source)
    return normalize_snapshot(
        MessageContextSnapshot(
            kind=kind,
            source_chat_id=source_chat_id,
            source_chat_username=source_chat_username,
            source_message_id=source_message_id,
            source_thread_id=_normalize_id(
                source_thread_id or getattr(source, "message_thread_id", None), positive=True
            ),
            source_sender_label=source_sender_label
            or _label(getattr(source, "sender_chat", None))
            or _label(getattr(source, "from_user", None)),
            source_text=text,
            source_caption=caption,
            source_url=_first_url(text, getattr(source, "entities", None))
            or _first_url(caption, getattr(source, "caption_entities", None)),
            source_date_utc=source_date or getattr(source, "date", None),
            **media,
        )
    )


def _has_forward(message: Any) -> bool:
    return any(
        getattr(message, name, None) is not None
        for name in (
            "forward_origin",
            "forward_date",
            "forward_from",
            "forward_from_chat",
            "forward_from_message_id",
            "forward_sender_name",
        )
    )


def is_contextual_message(message: Message) -> bool:
    """Return whether a message carries a source worth persisting."""

    if _has_forward(message) or getattr(message, "reply_to_message", None) is not None:
        return True
    if getattr(message, "photo", None) or getattr(message, "document", None):
        return True
    return bool(
        _first_url(getattr(message, "text", None), getattr(message, "entities", None))
        or _first_url(getattr(message, "caption", None), getattr(message, "caption_entities", None))
    )


def extract_message_context(message: Message) -> MessageContextSnapshot | None:
    """Extract a bounded snapshot without fetching URLs or downloading media."""

    current_chat = getattr(message, "chat", None)
    current_chat_id = _chat_id(current_chat)
    current_chat_username = _chat_username(current_chat)
    current_message_id = _normalize_id(getattr(message, "message_id", None), positive=True)
    current_date = getattr(message, "date", None)

    reply = getattr(message, "reply_to_message", None)
    if reply is not None:
        snapshot = _message_fields(
            reply,
            kind=ContextKind.REPLY.value,
            source_chat_id=_chat_id(getattr(reply, "chat", None)) or current_chat_id,
            source_chat_username=_chat_username(getattr(reply, "chat", None))
            or current_chat_username,
            source_message_id=_normalize_id(getattr(reply, "message_id", None), positive=True),
            source_sender_label=_label(getattr(reply, "sender_chat", None))
            or _label(getattr(reply, "from_user", None)),
        )
        context_metrics.extracted(snapshot.kind)
        return snapshot

    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        origin_type = str(getattr(origin, "type", ""))
        origin_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        origin_user = getattr(origin, "sender_user", None)
        kind = (
            ContextKind.CHANNEL_POST.value
            if origin_type == "channel"
            else ContextKind.FORWARDED.value
        )
        snapshot = _message_fields(
            message,
            kind=kind,
            source_chat_id=_chat_id(origin_chat) or current_chat_id,
            source_chat_username=_chat_username(origin_chat) or current_chat_username,
            source_message_id=_normalize_id(getattr(origin, "message_id", None), positive=True)
            or current_message_id,
            source_sender_label=_label(origin_chat)
            or _label(origin_user)
            or _normalize_text(getattr(origin, "sender_user_name", None), 128),
            source_date=getattr(origin, "date", None) or current_date,
        )
        context_metrics.extracted(snapshot.kind)
        return snapshot

    legacy_chat = getattr(message, "forward_from_chat", None)
    if _has_forward(message):
        kind = (
            ContextKind.CHANNEL_POST.value
            if getattr(legacy_chat, "type", None) == "channel"
            else ContextKind.FORWARDED.value
        )
        snapshot = _message_fields(
            message,
            kind=kind,
            source_chat_id=_chat_id(legacy_chat) or current_chat_id,
            source_chat_username=_chat_username(legacy_chat) or current_chat_username,
            source_message_id=_normalize_id(
                getattr(message, "forward_from_message_id", None), positive=True
            )
            or current_message_id,
            source_sender_label=_label(legacy_chat)
            or _label(getattr(message, "forward_from", None))
            or _normalize_text(getattr(message, "forward_sender_name", None), 128),
            source_date=getattr(message, "forward_date", None) or current_date,
        )
        context_metrics.extracted(snapshot.kind)
        return snapshot

    if (
        getattr(current_chat, "type", None) == "channel"
        or getattr(getattr(message, "sender_chat", None), "type", None) == "channel"
    ):
        kind = ContextKind.CHANNEL_POST.value
    elif getattr(message, "photo", None):
        kind = ContextKind.PHOTO.value
    elif getattr(message, "document", None):
        kind = ContextKind.DOCUMENT.value
    elif _first_url(
        getattr(message, "text", None), getattr(message, "entities", None)
    ) or _first_url(getattr(message, "caption", None), getattr(message, "caption_entities", None)):
        kind = ContextKind.LINK.value
    else:
        kind = ContextKind.ORDINARY.value

    if current_chat_id is None and current_message_id is None:
        return None
    snapshot = _message_fields(
        message,
        kind=kind,
        source_chat_id=current_chat_id,
        source_chat_username=current_chat_username,
        source_message_id=current_message_id,
        source_sender_label=_label(getattr(message, "sender_chat", None))
        or _label(getattr(message, "from_user", None)),
    )
    if not any(
        (
            snapshot.source_text,
            snapshot.source_caption,
            snapshot.source_url,
            snapshot.media_file_id,
            snapshot.source_message_id,
        )
    ):
        return None
    context_metrics.extracted(snapshot.kind)
    return snapshot


def context_kind_label(kind: str | None) -> str:
    return CONTEXT_KIND_LABELS.get(kind or "", "сообщение")


def context_reminder_text(snapshot: MessageContextSnapshot) -> str:
    normalized = normalize_snapshot(snapshot)
    if (
        normalized.media_kind
        and normalized.source_caption
        and _COMMAND_PREFIX_RE.match(normalized.source_caption)
    ):
        return "Фото из Telegram" if normalized.media_kind == "photo" else "Документ из Telegram"
    value = normalized.source_text or normalized.source_caption
    if value:
        return value[:MAX_CONTEXT_TEXT_LENGTH]
    if normalized.media_file_name:
        return f"Документ: {normalized.media_file_name}"
    if normalized.media_kind == "photo":
        return "Фото из Telegram"
    if normalized.media_kind == "document":
        return "Документ из Telegram"
    if normalized.source_message_id is not None:
        return f"{context_kind_label(normalized.kind).capitalize()} #{normalized.source_message_id}"
    return "Контекст из Telegram"


def _message_reference(snapshot: MessageContextSnapshot) -> str | None:
    if snapshot.source_message_id is None:
        return None
    if snapshot.source_chat_username:
        return f"https://t.me/{snapshot.source_chat_username}/{snapshot.source_message_id}"
    if snapshot.source_chat_id is not None:
        return f"chat_id={snapshot.source_chat_id}, message_id={snapshot.source_message_id}"
    return f"message_id={snapshot.source_message_id}"


def format_context_for_delivery(
    snapshot: MessageContextSnapshot | None,
    *,
    fallback_kind: str | None = None,
    media_unavailable: bool = False,
    max_length: int = MAX_CONTEXT_DELIVERY_LENGTH,
) -> str:
    if snapshot is None:
        if fallback_kind is None:
            return ""
        return (
            f"📎 Контекст ({context_kind_label(fallback_kind)}): исходное сообщение "
            "истекло или недоступно. Напоминание сохранено без него."
        )[:max_length]

    normalized = normalize_snapshot(snapshot)
    lines = [f"📎 Контекст: {context_kind_label(normalized.kind)}"]
    if normalized.source_sender_label:
        lines.append(f"Источник: {normalized.source_sender_label}")
    if normalized.source_text:
        lines.append(f"Текст: {normalized.source_text}")
    if normalized.source_caption and normalized.source_caption != normalized.source_text:
        lines.append(f"Подпись: {normalized.source_caption}")
    if normalized.source_url and normalized.source_url not in {
        normalized.source_text,
        normalized.source_caption,
    }:
        lines.append(f"Ссылка: {normalized.source_url}")
    reference = _message_reference(normalized)
    if reference:
        lines.append(f"Сообщение: {reference}")
    if normalized.media_kind:
        media_label = normalized.media_file_name or normalized.media_kind
        if media_unavailable:
            lines.append(f"Медиа: {media_label} недоступно; сохранена ссылка на источник")
        else:
            lines.append(f"Медиа: {media_label} (без локальной копии)")
    return "\n".join(lines)[:max_length]


def build_context_clarification() -> ClarificationRequest:
    return ClarificationRequest(
        kind="context_schedule",
        prompt=(
            "Контекст сохранён. Когда напомнить? Например: «завтра в 9», "
            "«через 2 часа» или «2026-09-10 18:30».\n"
            "Черновик действует 15 минут. /cancel отменит его."
        ),
        raw_text="напомни об этом",
    )


def _context_candidate(raw_text: str) -> str | None:
    value = raw_text.strip()
    if not _COMMAND_PREFIX_RE.match(value):
        return None
    match = _CONTEXT_PREFIX_RE.match(value)
    if match:
        tail = (match.group("tail") or "").strip()
        if not tail:
            return None
        value = f"напомни {tail}"
    value = _CONTEXT_MARKER_RE.sub("", value).strip()
    if not value:
        return None
    return f"{value} {CONTEXT_PLACEHOLDER}"


def parse_context_reminder_input(
    raw_text: str,
    *,
    now_local: datetime,
) -> ParsedReminder | ClarificationRequest | None:
    candidate = _context_candidate(raw_text)
    if candidate:
        parsed = parse_reminder_input(candidate, now_local=now_local)
        if isinstance(parsed, ParsedReminder):
            return parsed
    return parse_reminder_input(raw_text, now_local=now_local)


def _time_only_context_answer(value: str, now_local: datetime) -> ParsedReminder | None:
    match = _TIME_ONLY_RE.fullmatch(value)
    if match is None:
        return None
    try:
        local_dt = now_local.replace(
            hour=int(match.group("hour")),
            minute=int(match.group("minute")),
            second=0,
            microsecond=0,
        )
    except ValueError:
        return None
    if local_dt <= now_local:
        local_dt += timedelta(days=1)
    return ParsedReminder(local_dt=local_dt, text=CONTEXT_PLACEHOLDER)


def parse_context_clarification_answer(
    raw_text: str,
    answer: str,
    *,
    now_local: datetime,
) -> ParsedReminder | None:
    parsed = parse_clarification_answer(raw_text, answer, now_local=now_local)
    if isinstance(parsed, ParsedReminder):
        return parsed

    value = answer.strip()
    if not value:
        return None

    normalized_raw = raw_text.lower().replace("ё", "е")
    time_match = _TIME_ONLY_RE.fullmatch(value)
    if time_match is not None and (
        "завтра вечером" in normalized_raw or "после обеда" in normalized_raw
    ):
        prefix = "завтра" if "завтра вечером" in normalized_raw else "сегодня"
        candidate = f"напомни {prefix} в {value} {CONTEXT_PLACEHOLDER}"
    elif time_match is not None:
        return _time_only_context_answer(value, now_local)
    else:
        candidate = (
            f"{value} {CONTEXT_PLACEHOLDER}"
            if _COMMAND_PREFIX_RE.match(value)
            else (f"напомни {value} {CONTEXT_PLACEHOLDER}")
        )
    resolved = parse_reminder_input(candidate, now_local=now_local)
    return resolved if isinstance(resolved, ParsedReminder) else None


def _row_to_snapshot(row: ReminderContext) -> MessageContextSnapshot:
    return normalize_snapshot(
        MessageContextSnapshot(
            kind=row.kind,
            source_chat_id=row.source_chat_id,
            source_chat_username=row.source_chat_username,
            source_message_id=row.source_message_id,
            source_thread_id=row.source_thread_id,
            source_sender_label=row.source_sender_label,
            source_text=row.source_text,
            source_caption=row.source_caption,
            source_url=row.source_url,
            media_kind=row.media_kind,
            media_file_id=row.media_file_id,
            media_file_name=row.media_file_name,
            media_mime_type=row.media_mime_type,
            media_size=row.media_size,
            source_date_utc=row.source_date_utc,
        )
    )


async def get_context_for_delivery(
    reminder_id: int,
    user_id: int,
    chat_id: int,
    *,
    now_utc: datetime | None = None,
) -> MessageContextSnapshot | None:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session:
        row = await session.scalar(
            select(ReminderContext)
            .join(Reminder, Reminder.id == ReminderContext.reminder_id)
            .where(
                ReminderContext.reminder_id == reminder_id,
                ReminderContext.user_id == user_id,
                ReminderContext.chat_id == chat_id,
                Reminder.user_id == user_id,
                Reminder.chat_id == chat_id,
                ReminderContext.expires_at > current_time,
            )
        )
        return _row_to_snapshot(row) if row is not None else None


async def cleanup_expired_reminder_contexts(*, now_utc: datetime | None = None) -> int:
    current_time = _as_utc(now_utc or utc_now())
    try:
        async with SessionLocal() as session, session.begin():
            grouped = await session.execute(
                select(ReminderContext.kind, func.count(ReminderContext.id))
                .where(ReminderContext.expires_at <= current_time)
                .group_by(ReminderContext.kind)
            )
            counts = [(str(kind), int(count)) for kind, count in grouped.all()]
            result = await session.execute(
                delete(ReminderContext).where(ReminderContext.expires_at <= current_time)
            )
            deleted = int(getattr(result, "rowcount", 0) or 0)
            for kind, count in counts:
                context_metrics.cleanup(kind, count)
            return deleted
    except Exception as exc:
        context_metrics.cleanup_failures += 1
        logger.warning(
            "Reminder context cleanup failed",
            extra={"extra_data": f"error_type={type(exc).__name__[:80]}"},
        )
        return 0
