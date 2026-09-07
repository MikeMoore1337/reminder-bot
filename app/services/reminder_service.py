from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta
from html import escape
from typing import cast

from sqlalchemy import func, select

from app.db.models import RecurrenceType, Reminder, User
from app.db.session import SessionLocal
from app.utils.datetime_utils import (
    DatetimeSemantics,
    from_utc_to_user,
    resolve_schedule_datetime,
    to_utc,
    utc_now,
)

logger = logging.getLogger(__name__)


def _next_month(dt: datetime, day_of_month: int | None = None) -> datetime:
    year = dt.year + (1 if dt.month == 12 else 0)
    month = 1 if dt.month == 12 else dt.month + 1
    day = min(day_of_month or dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def validate_recurrence(recurrence_type: str, recurrence_interval: int) -> None:
    if recurrence_interval < 1:
        raise ValueError("Интервал повторения должен быть больше 0")

    if recurrence_type == RecurrenceType.MINUTES.value and recurrence_interval < 5:
        raise ValueError("Минимальный интервал для повторения в минутах - 5 минут")

    if (
        recurrence_type
        in {
            RecurrenceType.HOURLY.value,
            RecurrenceType.DAILY.value,
            RecurrenceType.WEEKLY.value,
            RecurrenceType.MONTHLY.value,
        }
        and recurrence_interval < 1
    ):
        raise ValueError("Интервал повторения должен быть не меньше 1")


def calculate_next_occurrence(
    remind_at_utc: datetime,
    recurrence_type: str,
    recurrence_interval: int,
    timezone_name: str = "UTC",
    recurrence_day_of_month: int | None = None,
) -> datetime | None:
    if recurrence_type == RecurrenceType.NONE.value:
        return None

    if recurrence_type == RecurrenceType.MINUTES.value:
        return remind_at_utc + timedelta(minutes=recurrence_interval)

    if recurrence_type == RecurrenceType.HOURLY.value:
        return remind_at_utc + timedelta(hours=recurrence_interval)

    local_dt = from_utc_to_user(remind_at_utc, timezone_name).replace(tzinfo=None)

    if recurrence_type == RecurrenceType.DAILY.value:
        next_local_dt = local_dt + timedelta(days=recurrence_interval)
        return to_utc(next_local_dt, timezone_name)

    if recurrence_type == RecurrenceType.WEEKLY.value:
        next_local_dt = local_dt + timedelta(weeks=recurrence_interval)
        return to_utc(next_local_dt, timezone_name)

    if recurrence_type == RecurrenceType.MONTHLY.value:
        next_dt = local_dt
        for _ in range(recurrence_interval):
            next_dt = _next_month(next_dt, recurrence_day_of_month)
        return to_utc(next_dt, timezone_name)

    raise ValueError(f"Unsupported recurrence_type: {recurrence_type}")


def advance_occurrence_until_future(
    remind_at_utc: datetime,
    recurrence_type: str,
    recurrence_interval: int,
    timezone_name: str,
    recurrence_day_of_month: int | None,
    now_utc: datetime,
) -> datetime | None:
    next_occurrence = calculate_next_occurrence(
        remind_at_utc,
        recurrence_type,
        recurrence_interval,
        timezone_name=timezone_name,
        recurrence_day_of_month=recurrence_day_of_month,
    )
    while next_occurrence is not None and next_occurrence <= now_utc:
        next_occurrence = calculate_next_occurrence(
            next_occurrence,
            recurrence_type,
            recurrence_interval,
            timezone_name=timezone_name,
            recurrence_day_of_month=recurrence_day_of_month,
        )
    return next_occurrence


def build_snooze_state(
    canonical_at_utc: datetime,
    now_utc: datetime,
    minutes: int,
) -> tuple[datetime, datetime]:
    if minutes < 1:
        raise ValueError("Время откладывания должно быть больше 0 минут")
    return canonical_at_utc, now_utc + timedelta(minutes=minutes)


def delivery_at_utc(reminder: Reminder) -> datetime:
    return reminder.delivery_at_utc or reminder.remind_at_utc


async def create_reminder(
    user: User,
    local_dt: datetime,
    text: str,
    recurrence_type: str = "none",
    recurrence_interval: int = 1,
    datetime_semantics: DatetimeSemantics = "wall_clock",
) -> Reminder:
    validate_recurrence(recurrence_type, recurrence_interval)

    remind_at_utc = resolve_schedule_datetime(
        local_dt,
        user.timezone,
        semantics=datetime_semantics,
    )
    now_utc = utc_now()
    recurrence_day_of_month = (
        local_dt.day if recurrence_type == RecurrenceType.MONTHLY.value else None
    )

    if recurrence_type == RecurrenceType.NONE.value and remind_at_utc <= now_utc:
        raise ValueError("Время напоминания уже прошло")

    if recurrence_type != RecurrenceType.NONE.value:
        while remind_at_utc <= now_utc:
            next_dt = calculate_next_occurrence(
                remind_at_utc,
                recurrence_type,
                recurrence_interval,
                timezone_name=user.timezone,
                recurrence_day_of_month=recurrence_day_of_month,
            )
            if next_dt is None:
                break
            remind_at_utc = next_dt

    async with SessionLocal() as session:
        reminder = Reminder(
            user_id=user.id,
            chat_id=user.chat_id,
            text=text,
            remind_at_utc=remind_at_utc,
            status="pending",
            schedule_timezone=user.timezone,
            delivery_at_utc=remind_at_utc,
            recurrence_type=recurrence_type,
            recurrence_interval=recurrence_interval,
            recurrence_day_of_month=recurrence_day_of_month,
        )
        session.add(reminder)
        await session.commit()
        await session.refresh(reminder)

        logger.info(
            "Created reminder",
            extra={
                "extra_data": (
                    f"reminder_id={reminder.id} "
                    f"user_id={user.id} "
                    f"remind_at_utc={reminder.remind_at_utc.isoformat()} "
                    f"recurrence_type={reminder.recurrence_type} "
                    f"recurrence_interval={reminder.recurrence_interval}"
                )
            },
        )
        return reminder


async def list_pending_reminders(user: User) -> list[Reminder]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Reminder)
            .where(
                Reminder.user_id == user.id,
                Reminder.status == "pending",
            )
            .order_by(func.coalesce(Reminder.delivery_at_utc, Reminder.remind_at_utc).asc())
        )
        return list(result.scalars().all())


async def delete_reminder_any_status(user: User, reminder_id: int) -> bool:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Reminder).where(
                Reminder.id == reminder_id,
                Reminder.user_id == user.id,
            )
        )
        reminder = result.scalar_one_or_none()
        if reminder is None:
            return False

        await session.delete(reminder)
        await session.commit()

        logger.info(
            "Deleted reminder",
            extra={"extra_data": f"reminder_id={reminder_id} user_id={user.id}"},
        )
        return True


async def cancel_reminder(user: User, reminder_id: int) -> bool:
    return await delete_reminder_any_status(user=user, reminder_id=reminder_id)


async def snooze_reminder(user: User, reminder_id: int, minutes: int = 10) -> Reminder | None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Reminder).where(
                Reminder.id == reminder_id,
                Reminder.user_id == user.id,
            )
        )
        reminder = result.scalar_one_or_none()
        if reminder is None:
            return None

        reminder.chat_id = user.chat_id
        canonical_at_utc, snoozed_until_utc = build_snooze_state(
            reminder.remind_at_utc,
            utc_now(),
            minutes,
        )
        reminder.remind_at_utc = canonical_at_utc
        reminder.delivery_at_utc = snoozed_until_utc
        reminder.snoozed_until_utc = snoozed_until_utc
        reminder.status = "pending"
        reminder.retry_count = 0
        reminder.error_text = None

        await session.commit()
        await session.refresh(reminder)
        return cast(Reminder | None, reminder)


async def set_last_message_id(reminder_id: int, message_id: int | None) -> None:
    async with SessionLocal() as session, session.begin():
        reminder = await session.get(Reminder, reminder_id)
        if reminder is not None:
            reminder.last_message_id = message_id


async def get_stats() -> dict[str, int]:
    async with SessionLocal() as session:
        total_users = await session.scalar(select(func.count()).select_from(User))
        total_reminders = await session.scalar(select(func.count()).select_from(Reminder))
        pending_reminders = await session.scalar(
            select(func.count()).select_from(Reminder).where(Reminder.status == "pending")
        )
        failed_reminders = await session.scalar(
            select(func.count()).select_from(Reminder).where(Reminder.status == "failed")
        )
        recurring_reminders = await session.scalar(
            select(func.count())
            .select_from(Reminder)
            .where(Reminder.recurrence_type != RecurrenceType.NONE.value)
        )
        sent_today = await session.scalar(
            select(func.count())
            .select_from(Reminder)
            .where(
                Reminder.sent_at.is_not(None),
                Reminder.sent_at >= utc_now() - timedelta(days=1),
            )
        )

        return {
            "total_users": total_users or 0,
            "total_reminders": total_reminders or 0,
            "pending_reminders": pending_reminders or 0,
            "failed_reminders": failed_reminders or 0,
            "recurring_reminders": recurring_reminders or 0,
            "sent_last_24h": sent_today or 0,
        }


async def get_failed_reminders(limit: int = 20) -> list[Reminder]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Reminder)
            .where(Reminder.status == "failed")
            .order_by(Reminder.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


def format_recurrence(reminder: Reminder) -> str:
    recurrence_type = str(reminder.recurrence_type)
    interval = int(reminder.recurrence_interval)

    if recurrence_type == RecurrenceType.NONE.value:
        return "нет"

    if recurrence_type == RecurrenceType.MINUTES.value:
        return "каждые 1 минуту" if interval == 1 else f"каждые {interval} минут"

    if recurrence_type == RecurrenceType.HOURLY.value:
        return "каждый час" if interval == 1 else f"каждые {interval} часов"

    if recurrence_type == RecurrenceType.DAILY.value:
        return "каждый день" if interval == 1 else f"каждые {interval} дней"

    if recurrence_type == RecurrenceType.WEEKLY.value:
        return "каждую неделю" if interval == 1 else f"каждые {interval} недель"

    if recurrence_type == RecurrenceType.MONTHLY.value:
        return "каждый месяц" if interval == 1 else f"каждые {interval} месяцев"

    return recurrence_type


def format_reminder_for_user(reminder: Reminder, timezone_name: str) -> str:
    local_dt = from_utc_to_user(delivery_at_utc(reminder), timezone_name)
    return (
        f"ID: {reminder.id}\n"
        f"Когда: {local_dt.strftime('%d.%m.%Y %H:%M')}\n"
        f"Повтор: {format_recurrence(reminder)}\n"
        f"Текст: {escape(reminder.text)}"
    )
