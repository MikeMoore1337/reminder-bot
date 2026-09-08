from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

CALLBACK_VERSION = "r1"
CALLBACK_MAX_BYTES = 64


class CallbackTarget(StrEnum):
    REMINDER = "r"
    OCCURRENCE = "o"
    VOICE_DRAFT = "v"


class CallbackOrigin(StrEnum):
    """Telegram surface that rendered the controls."""

    DELIVERY = "d"
    LIST = "l"
    VOICE = "v"


class CallbackAction(StrEnum):
    DONE = "done"
    SNOOZE = "snooze"
    SNOOZE_10 = "s10"
    SNOOZE_1H = "s1h"
    SNOOZE_EVENING = "sev"
    SNOOZE_TOMORROW = "stom"
    SNOOZE_CUSTOM = "scustom"
    EDIT = "edit"
    PAUSE = "pause"
    RESUME = "resume"
    DISABLE_PERSISTENT = "disable"
    DELETE = "delete"
    CREATE = "create"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class ReminderCallback:
    version: str
    action: CallbackAction
    target: CallbackTarget
    target_id: int
    revision: int
    origin: CallbackOrigin


_ID_RE = re.compile(r"[1-9][0-9]*\Z")
_REVISION_RE = re.compile(r"[0-9]+\Z")


def encode_callback(
    action: CallbackAction | str,
    target: CallbackTarget | str,
    target_id: int,
    revision: int,
    *,
    origin: CallbackOrigin | str = CallbackOrigin.DELIVERY,
) -> str:
    try:
        action_value = CallbackAction(action)
        target_value = CallbackTarget(target)
        origin_value = CallbackOrigin(origin)
    except ValueError as exc:
        raise ValueError("Unsupported callback action, target, or origin") from exc

    if target_id < 1 or revision < 0:
        raise ValueError("Callback identifiers must be positive and revision non-negative")

    payload = (
        f"{CALLBACK_VERSION}:{action_value.value}:{target_value.value}:"
        f"{target_id}:{revision}:{origin_value.value}"
    )
    if len(payload.encode("utf-8")) > CALLBACK_MAX_BYTES:
        raise ValueError("Callback data exceeds Telegram's callback_data limit")
    return payload


def parse_callback(data: str | None) -> ReminderCallback | None:
    if not isinstance(data, str):
        return None

    parts = data.split(":")
    if len(parts) not in {5, 6} or parts[0] != CALLBACK_VERSION:
        return None

    _, action_raw, target_raw, target_id_raw, revision_raw = parts[:5]
    origin_raw = parts[5] if len(parts) == 6 else CallbackOrigin.DELIVERY.value
    if not _ID_RE.fullmatch(target_id_raw) or not _REVISION_RE.fullmatch(revision_raw):
        return None

    try:
        action = CallbackAction(action_raw)
        target = CallbackTarget(target_raw)
        target_id = int(target_id_raw)
        revision = int(revision_raw)
        origin = CallbackOrigin(origin_raw)
    except (TypeError, ValueError, OverflowError):
        return None

    try:
        if len(data.encode("utf-8")) > CALLBACK_MAX_BYTES:
            return None
    except UnicodeEncodeError:
        return None

    return ReminderCallback(
        version=CALLBACK_VERSION,
        action=action,
        target=target,
        target_id=target_id,
        revision=revision,
        origin=origin,
    )
