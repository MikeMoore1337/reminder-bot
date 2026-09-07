from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from app.db.models import Reminder, ReminderClarification, User
from app.db.session import SessionLocal
from app.services.reminder_parser import ClarificationRequest, ParsedReminder

CLARIFICATION_TTL = timedelta(minutes=15)
MAX_CLARIFICATION_TEXT_LENGTH = 4096


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def create_clarification(
    user: User,
    request: ClarificationRequest,
    *,
    now_utc: datetime | None = None,
    expires_at: datetime | None = None,
) -> ReminderClarification:
    raw_text = request.raw_text.strip()
    if not raw_text or len(raw_text) > MAX_CLARIFICATION_TEXT_LENGTH:
        raise ValueError("Запрос на уточнение слишком длинный")
    current_time = _as_utc(now_utc or datetime.now(UTC))
    expiry = _as_utc(expires_at or (current_time + CLARIFICATION_TTL))

    async with SessionLocal() as session, session.begin():
        owner = await session.scalar(
            select(User).where(User.id == user.id, User.chat_id == user.chat_id).with_for_update()
        )
        if owner is None:
            raise ValueError("Пользователь не найден")
        existing = await session.scalar(
            select(ReminderClarification)
            .where(
                ReminderClarification.user_id == user.id,
                ReminderClarification.chat_id == user.chat_id,
            )
            .with_for_update()
        )
        if existing is None:
            existing = ReminderClarification(
                user_id=user.id,
                chat_id=user.chat_id,
                raw_text=raw_text,
                clarification_type=request.kind,
                prompt=request.prompt,
                expires_at=expiry,
            )
            session.add(existing)
        else:
            existing.raw_text = raw_text
            existing.clarification_type = request.kind
            existing.prompt = request.prompt
            existing.expires_at = expiry
        await session.flush()
        return existing


async def get_active_clarification(
    user: User,
    *,
    now_utc: datetime | None = None,
) -> ReminderClarification | None:
    current_time = _as_utc(now_utc or datetime.now(UTC))
    async with SessionLocal() as session, session.begin():
        clarification = await session.scalar(
            select(ReminderClarification)
            .where(
                ReminderClarification.user_id == user.id,
                ReminderClarification.chat_id == user.chat_id,
            )
            .with_for_update()
        )
        if clarification is None:
            return None
        if _as_utc(clarification.expires_at) <= current_time:
            await session.delete(clarification)
            return None
        return clarification


async def consume_clarification_and_create_reminder(
    user: User,
    clarification_id: int,
    raw_text: str,
    parsed: ParsedReminder,
    *,
    now_utc: datetime | None = None,
) -> Reminder | None:
    """Consume one exact clarification and create its reminder atomically.

    The owner row is locked before the clarification row so this operation has
    the same lock order as clarification replacement. A duplicate Telegram
    retry waits for the first transaction and then finds the consumed row.
    """

    current_time = _as_utc(now_utc or datetime.now(UTC))
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
                ReminderClarification.raw_text == raw_text,
            )
            .with_for_update()
        )
        if clarification is None:
            return None
        if _as_utc(clarification.expires_at) <= current_time:
            await session.delete(clarification)
            return None

        # Import locally to keep the service dependency graph acyclic.
        from app.services.reminder_service import create_reminder_in_session

        reminder = await create_reminder_in_session(
            session,
            owner,
            parsed.local_dt,
            parsed.text,
            parsed.recurrence_type,
            parsed.recurrence_interval,
            parsed.datetime_semantics,
            parsed.recurrence_rule,
            parsed.recurrence_day_of_month,
            now_utc=current_time,
        )
        await session.delete(clarification)
        await session.flush()
        return reminder


async def delete_clarification(user: User, clarification_id: int) -> bool:
    async with SessionLocal() as session, session.begin():
        result = cast(
            CursorResult[Any],
            await session.execute(
                delete(ReminderClarification).where(
                    ReminderClarification.id == clarification_id,
                    ReminderClarification.user_id == user.id,
                    ReminderClarification.chat_id == user.chat_id,
                )
            ),
        )
        return bool(result.rowcount)


async def cancel_clarification(user: User) -> bool:
    async with SessionLocal() as session, session.begin():
        result = cast(
            CursorResult[Any],
            await session.execute(
                delete(ReminderClarification).where(
                    ReminderClarification.user_id == user.id,
                    ReminderClarification.chat_id == user.chat_id,
                )
            ),
        )
        return bool(result.rowcount)
