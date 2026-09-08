from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from html import escape
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DeadlinePlanState,
    DeadlineReminderDraft,
    DeadlineStepState,
    Reminder,
    ReminderDeadlinePlan,
    ReminderDeadlineStep,
    ReminderKind,
    ReminderOccurrence,
    ReminderState,
    User,
)
from app.db.session import SessionLocal
from app.services.message_context import (
    MessageContextSnapshot,
    deserialize_context_snapshot,
    serialize_context_snapshot,
)
from app.services.reminder_parser import DeadlineRequest
from app.utils.datetime_utils import from_utc_to_user, resolve_schedule_datetime, to_utc, utc_now

logger = logging.getLogger(__name__)

DEADLINE_DRAFT_TTL = timedelta(minutes=15)
MAX_DEADLINE_STEPS = 6
MAX_OVERDUE_AFTER_MINUTES = 7 * 24 * 60

DEFAULT_DEADLINE_POINT_CODES = ("day_before", "before_deadline", "at_deadline")


@dataclass
class DeadlineMetrics:
    """Process-local counters for safe deadline-plan observability."""

    planned: int = 0
    delivered: int = 0
    skipped: int = 0
    completed: int = 0
    escalated: int = 0
    bounded_suppressions: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "planned": self.planned,
            "delivered": self.delivered,
            "skipped": self.skipped,
            "completed": self.completed,
            "escalated": self.escalated,
            "bounded_suppressions": self.bounded_suppressions,
        }


deadline_metrics = DeadlineMetrics()


def get_deadline_metrics() -> dict[str, int]:
    return deadline_metrics.snapshot()


def _record_deadline_event(
    event: str,
    *,
    reminder_id: int | None = None,
    plan_revision: int | None = None,
    sequence: int | None = None,
    step_code: str | None = None,
    state: str | None = None,
    count: int = 1,
) -> None:
    if count < 1:
        return
    metric_name = {
        "planned": "planned",
        "delivered": "delivered",
        "skipped": "skipped",
        "completed": "completed",
        "escalated": "escalated",
        "bounded_suppression": "bounded_suppressions",
    }.get(event)
    if metric_name is not None:
        setattr(deadline_metrics, metric_name, getattr(deadline_metrics, metric_name) + count)
    fields = [f"event={event}", f"count={count}"]
    if reminder_id is not None:
        fields.append(f"reminder_id={reminder_id}")
    if plan_revision is not None:
        fields.append(f"plan_revision={plan_revision}")
    if sequence is not None:
        fields.append(f"sequence={sequence}")
    if step_code is not None:
        fields.append(f"step_code={step_code}")
    if state is not None:
        fields.append(f"state={state}")
    logger.info("Deadline plan transition", extra={"extra_data": " ".join(fields)})


_POINT_LABELS = {
    "week_before": "За неделю до дедлайна",
    "day_before": "За день до дедлайна",
    "deadline_morning": "Утром в день дедлайна",
    "before_deadline": "За час до дедлайна",
    "at_deadline": "В момент дедлайна",
    "overdue": "После дедлайна",
}
_POINT_KINDS = {
    "week_before": "pre_deadline",
    "day_before": "pre_deadline",
    "deadline_morning": "pre_deadline",
    "before_deadline": "pre_deadline",
    "at_deadline": "deadline",
    "overdue": "overdue",
}
_POINT_ALIASES = {
    "week": "week_before",
    "week_before": "week_before",
    "за неделю": "week_before",
    "day": "day_before",
    "day_before": "day_before",
    "за день": "day_before",
    "morning": "deadline_morning",
    "deadline_morning": "deadline_morning",
    "утром": "deadline_morning",
    "hour": "before_deadline",
    "hour_before": "before_deadline",
    "before_deadline": "before_deadline",
    "за час": "before_deadline",
    "deadline": "at_deadline",
    "at_deadline": "at_deadline",
    "в срок": "at_deadline",
    "overdue": "overdue",
    "after_deadline": "overdue",
    "просрочено": "overdue",
}


@dataclass(frozen=True, slots=True)
class PlannedDeadlineStep:
    code: str
    kind: str
    label: str
    scheduled_at_utc: datetime
    state: str = DeadlineStepState.PENDING.value
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedDeadlinePlan:
    deadline_at_utc: datetime
    schedule_timezone: str
    overdue_after_minutes: int | None
    steps: tuple[PlannedDeadlineStep, ...]

    @property
    def pending_steps(self) -> tuple[PlannedDeadlineStep, ...]:
        return tuple(step for step in self.steps if step.state == DeadlineStepState.PENDING.value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_deadline_points(value: Sequence[str] | str | None) -> tuple[str, ...]:
    raw_values = (
        list(DEFAULT_DEADLINE_POINT_CODES)
        if value is None
        else [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, str)
        else [str(part).strip() for part in value if str(part).strip()]
    )
    if not raw_values:
        raise ValueError("Укажи хотя бы одну точку плана дедлайна")

    normalized: list[str] = []
    for raw in raw_values:
        code = _POINT_ALIASES.get(raw.lower().replace("ё", "е"), raw.lower())
        if code not in _POINT_LABELS:
            raise ValueError(f"Неизвестная точка плана дедлайна: {raw}")
        if code in normalized:
            raise ValueError("Каждую точку дедлайна можно указать только один раз")
        normalized.append(code)

    if "at_deadline" not in normalized:
        normalized.append("at_deadline")
    if len(normalized) > MAX_DEADLINE_STEPS:
        raise ValueError(f"План дедлайна может содержать не более {MAX_DEADLINE_STEPS} точек")
    return tuple(normalized)


def normalize_overdue_after_minutes(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Интервал после дедлайна должен быть числом минут")
    if not 1 <= value <= MAX_OVERDUE_AFTER_MINUTES:
        raise ValueError(
            f"Интервал после дедлайна должен быть от 1 до {MAX_OVERDUE_AFTER_MINUTES} минут"
        )
    return value


def _point_candidate(
    code: str,
    *,
    deadline_at_utc: datetime,
    timezone_name: str,
    overdue_after_minutes: int | None,
) -> datetime:
    deadline = _as_utc(deadline_at_utc)
    local_deadline = from_utc_to_user(deadline, timezone_name).replace(tzinfo=None)
    local_clock = local_deadline.time().replace(second=0, microsecond=0)
    if code == "week_before":
        return to_utc(
            datetime.combine(local_deadline.date() - timedelta(days=7), local_clock),
            timezone_name,
        )
    if code == "day_before":
        return to_utc(
            datetime.combine(local_deadline.date() - timedelta(days=1), local_clock),
            timezone_name,
        )
    if code == "deadline_morning":
        return to_utc(datetime.combine(local_deadline.date(), time(9, 0)), timezone_name)
    if code == "before_deadline":
        return deadline - timedelta(hours=1)
    if code == "at_deadline":
        return deadline
    if code == "overdue":
        if overdue_after_minutes is None:
            overdue_after_minutes = 60
        return deadline + timedelta(minutes=overdue_after_minutes)
    raise ValueError(f"Неизвестная точка плана дедлайна: {code}")


def build_deadline_plan(
    deadline_at_utc: datetime,
    timezone_name: str,
    *,
    now_utc: datetime | None = None,
    point_codes: Sequence[str] | str | None = None,
    overdue_after_minutes: int | None = None,
) -> GeneratedDeadlinePlan:
    """Build a bounded, deterministic plan without touching persistence."""

    deadline = _as_utc(deadline_at_utc)
    current_time = _as_utc(now_utc or utc_now())
    codes = normalize_deadline_points(point_codes)
    overdue_minutes = normalize_overdue_after_minutes(overdue_after_minutes)
    if "overdue" in codes and overdue_minutes is None:
        overdue_minutes = 60

    candidates: list[tuple[int, str, datetime]] = []
    for position, code in enumerate(codes):
        candidate = _point_candidate(
            code,
            deadline_at_utc=deadline,
            timezone_name=timezone_name,
            overdue_after_minutes=overdue_minutes,
        )
        if code not in {"at_deadline", "overdue"} and candidate >= deadline:
            raise ValueError(f"Точка «{_POINT_LABELS[code]}» должна быть до дедлайна")
        if code == "overdue" and candidate <= deadline:
            raise ValueError("Точка после дедлайна должна быть позже самого дедлайна")
        candidates.append((position, code, candidate))

    candidates.sort(key=lambda item: (item[2], item[0]))
    result: list[PlannedDeadlineStep] = []
    previous_at: datetime | None = None
    for _, code, candidate in candidates:
        if previous_at is not None and candidate == previous_at:
            raise ValueError("Две точки плана дедлайна назначены на одно время")
        previous_at = candidate
        result.append(
            PlannedDeadlineStep(
                code=code,
                kind=_POINT_KINDS[code],
                label=_POINT_LABELS[code],
                scheduled_at_utc=candidate,
                state=(
                    DeadlineStepState.SKIPPED.value
                    if candidate < current_time
                    else DeadlineStepState.PENDING.value
                ),
                skip_reason="already_elapsed" if candidate < current_time else None,
            )
        )
    return GeneratedDeadlinePlan(
        deadline_at_utc=deadline,
        schedule_timezone=timezone_name,
        overdue_after_minutes=overdue_minutes,
        steps=tuple(result),
    )


def _serialize_plan(plan: GeneratedDeadlinePlan) -> str:
    return json.dumps(
        [
            {
                "code": step.code,
                "kind": step.kind,
                "label": step.label,
                "scheduled_at_utc": _as_utc(step.scheduled_at_utc).isoformat(),
                "state": step.state,
                "skip_reason": step.skip_reason,
            }
            for step in plan.steps
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _deserialize_plan(value: str) -> tuple[PlannedDeadlineStep, ...]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 24_000:
        raise ValueError("План дедлайна повреждён")
    try:
        raw_steps = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("План дедлайна повреждён") from exc
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_DEADLINE_STEPS:
        raise ValueError("План дедлайна повреждён")
    result: list[PlannedDeadlineStep] = []
    previous_at: datetime | None = None
    for raw in raw_steps:
        if not isinstance(raw, dict):
            raise ValueError("План дедлайна повреждён")
        code = raw.get("code")
        kind = raw.get("kind")
        label = raw.get("label")
        state = raw.get("state")
        if (
            not isinstance(code, str)
            or code not in _POINT_LABELS
            or not isinstance(kind, str)
            or kind != _POINT_KINDS[code]
            or not isinstance(label, str)
            or label != _POINT_LABELS[code]
            or state not in {item.value for item in DeadlineStepState}
        ):
            raise ValueError("План дедлайна повреждён")
        try:
            scheduled_at = _as_utc(datetime.fromisoformat(str(raw["scheduled_at_utc"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("План дедлайна повреждён") from exc
        if previous_at is not None and scheduled_at <= previous_at:
            raise ValueError("План дедлайна нарушает порядок точек")
        previous_at = scheduled_at
        reason = raw.get("skip_reason")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("План дедлайна повреждён")
        result.append(
            PlannedDeadlineStep(
                code=code,
                kind=kind,
                label=label,
                scheduled_at_utc=scheduled_at,
                state=state,
                skip_reason=reason,
            )
        )
    return tuple(result)


def _plan_from_request(
    user: User,
    request: DeadlineRequest,
    *,
    now_utc: datetime,
) -> GeneratedDeadlinePlan:
    if request.mode != "normal":
        raise ValueError("Важный режим нельзя объединить с планом дедлайна")
    deadline_at_utc = resolve_schedule_datetime(
        request.local_dt,
        user.timezone,
        semantics=request.datetime_semantics,
    )
    if deadline_at_utc <= _as_utc(now_utc):
        raise ValueError("Срок дедлайна уже прошёл")
    return build_deadline_plan(
        deadline_at_utc,
        user.timezone,
        now_utc=now_utc,
        point_codes=request.point_codes,
        overdue_after_minutes=request.overdue_after_minutes,
    )


def _first_pending(plan: GeneratedDeadlinePlan) -> PlannedDeadlineStep | None:
    return next(iter(plan.pending_steps), None)


def _set_deadline_mirror(
    reminder: Reminder,
    plan: ReminderDeadlinePlan,
    step: ReminderDeadlineStep | None,
) -> None:
    reminder.deadline_at_utc = _as_utc(plan.deadline_at_utc)
    reminder.deadline_plan_state = plan.state
    reminder.deadline_plan_revision = plan.revision
    reminder.deadline_current_step_sequence = step.sequence if step is not None else None
    reminder.deadline_current_step_code = step.code if step is not None else None
    reminder.deadline_current_step_label = step.label if step is not None else None
    reminder.deadline_total_steps = plan.total_steps
    reminder.deadline_overdue_after_minutes = plan.overdue_after_minutes


def _clear_delivery_identity(reminder: Reminder) -> None:
    reminder.last_message_id = None
    reminder.last_delivery_occurrence_utc = None
    reminder.sent_at = None
    reminder.error_text = None
    reminder.retry_count = 0
    reminder.attempt_count = 0
    reminder.next_retry_at = None
    reminder.processing_started_at = None
    reminder.lease_until = None
    reminder.lease_token = None


def _occurrence_matches(
    reminder: Reminder,
    occurrence: ReminderOccurrence,
    *,
    expected_revision: int,
    expected_message_id: int | None,
) -> bool:
    return (
        occurrence.action_revision == expected_revision
        and occurrence.status == "delivered"
        and (expected_message_id is None or occurrence.message_id == expected_message_id)
        and (expected_message_id is None or reminder.last_message_id == expected_message_id)
        and reminder.last_delivery_occurrence_utc is not None
        and _as_utc(reminder.last_delivery_occurrence_utc) == _as_utc(occurrence.occurrence_at_utc)
        and reminder.state
        not in {
            ReminderState.COMPLETED.value,
            ReminderState.CANCELLED.value,
            ReminderState.FAILED.value,
        }
        and reminder.status != "processing"
    )


async def _load_owned_deadline_action(
    session: AsyncSession,
    user: User,
    reminder_id: int,
    *,
    expected_revision: int,
    expected_occurrence_id: int | None,
    expected_occurrence_at_utc: datetime | None,
    expected_message_id: int | None,
) -> tuple[Reminder, ReminderDeadlinePlan, ReminderOccurrence | None] | None:
    reminder = await session.scalar(
        select(Reminder)
        .where(
            Reminder.id == reminder_id,
            Reminder.user_id == user.id,
            Reminder.chat_id == user.chat_id,
        )
        .with_for_update()
    )
    if reminder is None or reminder.kind != ReminderKind.DEADLINE.value:
        return None
    plan = await session.scalar(
        select(ReminderDeadlinePlan)
        .where(
            ReminderDeadlinePlan.reminder_id == reminder.id,
            ReminderDeadlinePlan.user_id == user.id,
            ReminderDeadlinePlan.chat_id == user.chat_id,
        )
        .with_for_update()
    )
    if plan is None:
        return None
    occurrence: ReminderOccurrence | None = None
    if expected_occurrence_id is not None:
        occurrence = await session.scalar(
            select(ReminderOccurrence)
            .where(
                ReminderOccurrence.id == expected_occurrence_id,
                ReminderOccurrence.reminder_id == reminder.id,
            )
            .with_for_update()
        )
        if occurrence is None or not _occurrence_matches(
            reminder,
            occurrence,
            expected_revision=expected_revision,
            expected_message_id=expected_message_id,
        ):
            return None
        if expected_occurrence_at_utc is not None and _as_utc(
            occurrence.occurrence_at_utc
        ) != _as_utc(expected_occurrence_at_utc):
            return None
    elif reminder.action_revision != expected_revision:
        return None
    if reminder.status == "processing" or reminder.state in {
        ReminderState.COMPLETED.value,
        ReminderState.CANCELLED.value,
        ReminderState.FAILED.value,
    }:
        return None
    return reminder, plan, occurrence


async def create_deadline_draft(
    user: User,
    request: DeadlineRequest,
    *,
    raw_text: str,
    source_message_id: int | None = None,
    context: MessageContextSnapshot | None = None,
    now_utc: datetime | None = None,
) -> DeadlineReminderDraft:
    current_time = _as_utc(now_utc or utc_now())
    generated = _plan_from_request(user, request, now_utc=current_time)
    serialized_context = serialize_context_snapshot(context) if context is not None else None
    async with SessionLocal() as session, session.begin():
        await session.execute(
            delete(DeadlineReminderDraft).where(
                DeadlineReminderDraft.user_id == user.id,
                DeadlineReminderDraft.chat_id == user.chat_id,
            )
        )
        draft = DeadlineReminderDraft(
            user_id=user.id,
            chat_id=user.chat_id,
            source_message_id=source_message_id,
            raw_text=raw_text[:4096],
            reminder_text=request.text.strip(),
            deadline_at_utc=generated.deadline_at_utc,
            schedule_timezone=user.timezone,
            point_codes_json=json.dumps(list(normalize_deadline_points(request.point_codes))),
            overdue_after_minutes=generated.overdue_after_minutes,
            plan_json=_serialize_plan(generated),
            context_snapshot=serialized_context,
            action_revision=1,
            expires_at=current_time + DEADLINE_DRAFT_TTL,
        )
        session.add(draft)
        await session.flush()
        return draft


async def get_active_deadline_draft(
    user: User,
    *,
    now_utc: datetime | None = None,
) -> DeadlineReminderDraft | None:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        draft = await session.scalar(
            select(DeadlineReminderDraft)
            .where(
                DeadlineReminderDraft.user_id == user.id,
                DeadlineReminderDraft.chat_id == user.chat_id,
            )
            .with_for_update()
        )
        if draft is None:
            return None
        if _as_utc(draft.expires_at) <= current_time:
            await session.delete(draft)
            return None
        return draft


async def bind_deadline_preview_message(
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
            select(DeadlineReminderDraft)
            .where(
                DeadlineReminderDraft.id == draft_id,
                DeadlineReminderDraft.user_id == user.id,
                DeadlineReminderDraft.chat_id == user.chat_id,
            )
            .with_for_update()
        )
        if (
            draft is None
            or draft.action_revision != revision
            or _as_utc(draft.expires_at) <= current_time
        ):
            return False
        draft.preview_message_id = message_id
        return True


async def confirm_deadline_draft(
    user: User,
    draft_id: int,
    *,
    expected_revision: int,
    expected_message_id: int,
    now_utc: datetime | None = None,
) -> Reminder | None:
    from app.services.reminder_service import create_reminder_in_session

    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        draft = await session.scalar(
            select(DeadlineReminderDraft)
            .where(
                DeadlineReminderDraft.id == draft_id,
                DeadlineReminderDraft.user_id == user.id,
                DeadlineReminderDraft.chat_id == user.chat_id,
            )
            .with_for_update()
        )
        if draft is None:
            return None
        if draft.action_revision != expected_revision:
            return None
        if draft.preview_message_id is not None and draft.preview_message_id != expected_message_id:
            return None
        if _as_utc(draft.expires_at) <= current_time:
            await session.delete(draft)
            return None

        steps = _deserialize_plan(draft.plan_json)
        first = next(
            (step for step in steps if step.state == DeadlineStepState.PENDING.value),
            None,
        )
        if first is None:
            await session.delete(draft)
            return None
        context = deserialize_context_snapshot(draft.context_snapshot)
        local_first = from_utc_to_user(first.scheduled_at_utc, draft.schedule_timezone).replace(
            tzinfo=None
        )
        reminder = await create_reminder_in_session(
            session,
            user,
            local_first,
            draft.reminder_text,
            schedule_timezone=draft.schedule_timezone,
            context=context,
            kind=ReminderKind.DEADLINE.value,
            deadline_at_utc=draft.deadline_at_utc,
            now_utc=current_time,
        )
        plan = ReminderDeadlinePlan(
            reminder_id=reminder.id,
            user_id=user.id,
            chat_id=user.chat_id,
            deadline_at_utc=draft.deadline_at_utc,
            schedule_timezone=draft.schedule_timezone,
            state=DeadlinePlanState.ACTIVE.value,
            revision=1,
            current_step_sequence=steps.index(first),
            total_steps=len(steps),
            overdue_after_minutes=draft.overdue_after_minutes,
        )
        session.add(plan)
        await session.flush()
        for sequence, step in enumerate(steps):
            session.add(
                ReminderDeadlineStep(
                    plan_id=plan.id,
                    revision=1,
                    sequence=sequence,
                    code=step.code,
                    kind=step.kind,
                    label=step.label,
                    scheduled_at_utc=step.scheduled_at_utc,
                    state=step.state,
                    skip_reason=step.skip_reason,
                )
            )
        current_row = ReminderDeadlineStep(
            plan_id=plan.id,
            revision=1,
            sequence=steps.index(first),
            code=first.code,
            kind=first.kind,
            label=first.label,
            scheduled_at_utc=first.scheduled_at_utc,
            state=first.state,
            skip_reason=first.skip_reason,
        )
        _set_deadline_mirror(reminder, plan, current_row)
        _record_deadline_event(
            "planned",
            reminder_id=reminder.id,
            plan_revision=plan.revision,
            count=len(steps),
        )
        initial_skipped = sum(step.state == DeadlineStepState.SKIPPED.value for step in steps)
        if initial_skipped:
            _record_deadline_event(
                "skipped",
                reminder_id=reminder.id,
                plan_revision=plan.revision,
                count=initial_skipped,
            )
            _record_deadline_event(
                "bounded_suppression",
                reminder_id=reminder.id,
                plan_revision=plan.revision,
                count=initial_skipped,
            )
        await session.delete(draft)
        await session.flush()
        return reminder


async def discard_deadline_draft(
    user: User,
    draft_id: int,
    *,
    expected_revision: int,
    expected_message_id: int,
) -> bool:
    async with SessionLocal() as session, session.begin():
        draft = await session.scalar(
            select(DeadlineReminderDraft)
            .where(
                DeadlineReminderDraft.id == draft_id,
                DeadlineReminderDraft.user_id == user.id,
                DeadlineReminderDraft.chat_id == user.chat_id,
            )
            .with_for_update()
        )
        if draft is None or draft.action_revision != expected_revision:
            return False
        if draft.preview_message_id is not None and draft.preview_message_id != expected_message_id:
            return False
        await session.delete(draft)
        return True


async def cancel_deadline_reminder_draft(user: User) -> bool:
    async with SessionLocal() as session, session.begin():
        result = await session.execute(
            delete(DeadlineReminderDraft).where(
                DeadlineReminderDraft.user_id == user.id,
                DeadlineReminderDraft.chat_id == user.chat_id,
            )
        )
        return bool(getattr(result, "rowcount", 0))


async def cleanup_expired_deadline_drafts(*, now_utc: datetime | None = None) -> int:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        result = await session.execute(
            delete(DeadlineReminderDraft).where(DeadlineReminderDraft.expires_at <= current_time)
        )
        return int(getattr(result, "rowcount", 0) or 0)


def format_deadline_draft_preview(draft: DeadlineReminderDraft) -> str:
    deadline_local = from_utc_to_user(draft.deadline_at_utc, draft.schedule_timezone)
    steps = _deserialize_plan(draft.plan_json)
    lines = [
        "<b>⏳ Проверь план дедлайна</b>",
        f"<b>Задача:</b> {escape(draft.reminder_text)}",
        (
            f"<b>Дедлайн:</b> {deadline_local.strftime('%d.%m.%Y %H:%M')} "
            f"({escape(draft.schedule_timezone)})"
        ),
        "",
        "<b>Включённые точки:</b>",
    ]
    for step in steps:
        local_step = from_utc_to_user(step.scheduled_at_utc, draft.schedule_timezone)
        suffix = (
            f" — пропущено: {step.skip_reason}"
            if step.state == DeadlineStepState.SKIPPED.value
            else ""
        )
        lines.append(
            f"• {escape(step.label)} — {local_step.strftime('%d.%m.%Y %H:%M')}{escape(suffix)}"
        )
    if draft.overdue_after_minutes is None:
        lines.append("• После дедлайна: выключено")
    else:
        lines.append(f"• После дедлайна: через {draft.overdue_after_minutes} мин.")
    lines.extend(("", "Нажми «Создать», чтобы сохранить план, или «Отмена»."))
    return "\n".join(lines)


async def get_current_deadline_step(
    session: AsyncSession,
    reminder: Reminder,
    *,
    for_update: bool = False,
) -> ReminderDeadlineStep | None:
    plan_query = select(ReminderDeadlinePlan).where(ReminderDeadlinePlan.reminder_id == reminder.id)
    if for_update:
        plan_query = plan_query.with_for_update()
    plan = await session.scalar(plan_query)
    if plan is None or plan.state != DeadlinePlanState.ACTIVE.value:
        return None
    if plan.current_step_sequence is None:
        return None
    step_query = select(ReminderDeadlineStep).where(
        ReminderDeadlineStep.plan_id == plan.id,
        ReminderDeadlineStep.revision == plan.revision,
        ReminderDeadlineStep.sequence == plan.current_step_sequence,
        ReminderDeadlineStep.state == DeadlineStepState.PENDING.value,
    )
    if for_update:
        step_query = step_query.with_for_update()
    return cast(ReminderDeadlineStep | None, await session.scalar(step_query))


async def reconcile_deadline_for_claim(
    session: AsyncSession,
    reminder: Reminder,
    *,
    now_utc: datetime,
) -> bool:
    """Return whether a deadline reminder still has a claimable active step."""

    if reminder.kind != ReminderKind.DEADLINE.value:
        return True
    plan = await session.scalar(
        select(ReminderDeadlinePlan)
        .where(ReminderDeadlinePlan.reminder_id == reminder.id)
        .with_for_update()
    )
    if plan is None or plan.state != DeadlinePlanState.ACTIVE.value:
        if plan is not None:
            _set_deadline_mirror(reminder, plan, None)
        reminder.status = "sent"
        reminder.state = ReminderState.PAUSED.value
        reminder.delivery_at_utc = None
        reminder.snoozed_until_utc = None
        return False
    step = await get_current_deadline_step(session, reminder, for_update=True)
    if step is None:
        plan.state = DeadlinePlanState.EXHAUSTED.value
        plan.stop_reason = "no_pending_steps"
        plan.current_step_sequence = None
        _set_deadline_mirror(reminder, plan, None)
        reminder.status = "sent"
        reminder.state = ReminderState.DELIVERED.value
        reminder.delivery_at_utc = None
        reminder.snoozed_until_utc = None
        return False
    _set_deadline_mirror(reminder, plan, step)
    if _as_utc(reminder.remind_at_utc) != _as_utc(step.scheduled_at_utc):
        reminder.remind_at_utc = step.scheduled_at_utc
        reminder.delivery_at_utc = step.scheduled_at_utc
    return True


async def finalize_deadline_delivery_success(
    session: AsyncSession,
    reminder: Reminder,
    occurrence: ReminderOccurrence,
    *,
    now_utc: datetime,
) -> bool:
    plan = await session.scalar(
        select(ReminderDeadlinePlan)
        .where(ReminderDeadlinePlan.reminder_id == reminder.id)
        .with_for_update()
    )
    if plan is None or plan.state != DeadlinePlanState.ACTIVE.value:
        return False
    step = None
    if occurrence.deadline_step_id is not None:
        step = await session.scalar(
            select(ReminderDeadlineStep)
            .where(
                ReminderDeadlineStep.id == occurrence.deadline_step_id,
                ReminderDeadlineStep.plan_id == plan.id,
            )
            .with_for_update()
        )
    if step is None:
        step = await get_current_deadline_step(session, reminder, for_update=True)
    if (
        step is None
        or step.revision != plan.revision
        or step.state != DeadlineStepState.PENDING.value
    ):
        return False
    step.state = DeadlineStepState.DELIVERED.value
    step.delivered_at = _as_utc(now_utc)
    step.message_id = occurrence.message_id
    _record_deadline_event(
        "delivered",
        reminder_id=reminder.id,
        plan_revision=plan.revision,
        sequence=step.sequence,
        step_code=step.code,
        state=step.state,
    )
    if step.kind == "overdue":
        _record_deadline_event(
            "escalated",
            reminder_id=reminder.id,
            plan_revision=plan.revision,
            sequence=step.sequence,
            step_code=step.code,
            state=step.state,
        )

    next_step: ReminderDeadlineStep | None = None
    skipped_count = 0
    pending = await session.scalars(
        select(ReminderDeadlineStep)
        .where(
            ReminderDeadlineStep.plan_id == plan.id,
            ReminderDeadlineStep.revision == plan.revision,
            ReminderDeadlineStep.sequence > step.sequence,
            ReminderDeadlineStep.state == DeadlineStepState.PENDING.value,
        )
        .order_by(ReminderDeadlineStep.sequence.asc())
        .with_for_update()
    )
    for candidate in pending:
        if _as_utc(candidate.scheduled_at_utc) < _as_utc(now_utc):
            candidate.state = DeadlineStepState.SKIPPED.value
            candidate.skip_reason = "missed_after_previous_delivery"
            skipped_count += 1
            continue
        next_step = candidate
        break

    if skipped_count:
        _record_deadline_event(
            "skipped",
            reminder_id=reminder.id,
            plan_revision=plan.revision,
            count=skipped_count,
        )
        _record_deadline_event(
            "bounded_suppression",
            reminder_id=reminder.id,
            plan_revision=plan.revision,
            count=skipped_count,
        )

    if next_step is None:
        plan.state = DeadlinePlanState.EXHAUSTED.value
        plan.stop_reason = "plan_complete"
        plan.current_step_sequence = None
        _set_deadline_mirror(reminder, plan, None)
        reminder.status = "sent"
        reminder.state = ReminderState.DELIVERED.value
        reminder.delivery_at_utc = None
        reminder.snoozed_until_utc = None
    else:
        plan.current_step_sequence = next_step.sequence
        _set_deadline_mirror(reminder, plan, next_step)
        reminder.remind_at_utc = next_step.scheduled_at_utc
        reminder.delivery_at_utc = next_step.scheduled_at_utc
        reminder.status = "pending"
        reminder.state = ReminderState.SCHEDULED.value
        reminder.snoozed_until_utc = None
    return True


async def finalize_deadline_delivery_failure(
    session: AsyncSession,
    reminder: Reminder,
    occurrence: ReminderOccurrence | None,
    *,
    now_utc: datetime,
    terminal: bool,
) -> None:
    plan = await session.scalar(
        select(ReminderDeadlinePlan)
        .where(ReminderDeadlinePlan.reminder_id == reminder.id)
        .with_for_update()
    )
    if plan is None or not terminal:
        return
    step = None
    if occurrence is not None and occurrence.deadline_step_id is not None:
        step = await session.scalar(
            select(ReminderDeadlineStep)
            .where(
                ReminderDeadlineStep.id == occurrence.deadline_step_id,
                ReminderDeadlineStep.plan_id == plan.id,
            )
            .with_for_update()
        )
    if step is None:
        step = await get_current_deadline_step(session, reminder, for_update=True)
    if step is not None:
        step.state = DeadlineStepState.FAILED.value
        step.failed_at = _as_utc(now_utc)
    plan.state = DeadlinePlanState.EXHAUSTED.value
    plan.stop_reason = "delivery_failed"
    plan.current_step_sequence = None
    _set_deadline_mirror(reminder, plan, None)


async def complete_deadline_in_session(
    session: AsyncSession,
    reminder: Reminder,
    occurrence: ReminderOccurrence,
    *,
    now_utc: datetime,
) -> bool:
    plan = await session.scalar(
        select(ReminderDeadlinePlan)
        .where(ReminderDeadlinePlan.reminder_id == reminder.id)
        .with_for_update()
    )
    if plan is None or plan.state in {
        DeadlinePlanState.COMPLETED.value,
        DeadlinePlanState.CANCELLED.value,
    }:
        return False
    pending = list(
        (
            await session.scalars(
                select(ReminderDeadlineStep)
                .where(
                    ReminderDeadlineStep.plan_id == plan.id,
                    ReminderDeadlineStep.revision == plan.revision,
                    ReminderDeadlineStep.state == DeadlineStepState.PENDING.value,
                )
                .with_for_update()
            )
        ).all()
    )
    for step in pending:
        step.state = DeadlineStepState.SKIPPED.value
        step.skip_reason = "completed"
    pending_count = len(pending)
    if pending_count:
        _record_deadline_event(
            "skipped",
            reminder_id=reminder.id,
            plan_revision=plan.revision,
            count=pending_count,
        )
    plan.state = DeadlinePlanState.COMPLETED.value
    plan.stop_reason = "user_completed"
    plan.completed_at = _as_utc(now_utc)
    plan.current_step_sequence = None
    occurrence.status = "completed"
    occurrence.completed_at = _as_utc(now_utc)
    occurrence.action_revision += 1
    _set_deadline_mirror(reminder, plan, None)
    reminder.state = ReminderState.COMPLETED.value
    reminder.status = "sent"
    reminder.completed_at = _as_utc(now_utc)
    reminder.delivery_at_utc = None
    reminder.snoozed_until_utc = None
    reminder.action_revision += 1
    _clear_delivery_identity(reminder)
    _record_deadline_event(
        "completed",
        reminder_id=reminder.id,
        plan_revision=plan.revision,
        state=plan.state,
    )
    return True


async def disable_deadline_in_session(
    session: AsyncSession,
    reminder: Reminder,
    plan: ReminderDeadlinePlan,
    *,
    occurrence: ReminderOccurrence | None,
    now_utc: datetime,
) -> bool:
    if plan.state != DeadlinePlanState.ACTIVE.value:
        return False
    plan.state = DeadlinePlanState.DISABLED.value
    plan.stop_reason = "user_disabled"
    plan.disabled_at = _as_utc(now_utc)
    _set_deadline_mirror(reminder, plan, None)
    reminder.action_revision += 1
    reminder.status = "sent"
    reminder.delivery_at_utc = None
    reminder.snoozed_until_utc = None
    if occurrence is not None:
        occurrence.action_revision = reminder.action_revision
        reminder.state = ReminderState.DELIVERED.value
    else:
        reminder.state = ReminderState.PAUSED.value
        reminder.paused_at = _as_utc(now_utc)
        _clear_delivery_identity(reminder)
    _record_deadline_event(
        "disabled",
        reminder_id=reminder.id,
        plan_revision=plan.revision,
        state=plan.state,
    )
    return True


async def enable_deadline_in_session(
    session: AsyncSession,
    reminder: Reminder,
    plan: ReminderDeadlinePlan,
    *,
    now_utc: datetime,
) -> bool:
    if plan.state != DeadlinePlanState.DISABLED.value or plan.current_step_sequence is None:
        return False
    step = await session.scalar(
        select(ReminderDeadlineStep)
        .where(
            ReminderDeadlineStep.plan_id == plan.id,
            ReminderDeadlineStep.revision == plan.revision,
            ReminderDeadlineStep.sequence == plan.current_step_sequence,
            ReminderDeadlineStep.state == DeadlineStepState.PENDING.value,
        )
        .with_for_update()
    )
    if step is None:
        return False
    plan.state = DeadlinePlanState.ACTIVE.value
    plan.stop_reason = None
    plan.disabled_at = None
    _set_deadline_mirror(reminder, plan, step)
    reminder.remind_at_utc = step.scheduled_at_utc
    reminder.delivery_at_utc = step.scheduled_at_utc
    reminder.state = ReminderState.SCHEDULED.value
    reminder.status = "pending"
    reminder.snoozed_until_utc = None
    reminder.paused_at = None
    reminder.completed_at = None
    reminder.action_revision += 1
    _clear_delivery_identity(reminder)
    _record_deadline_event(
        "enabled",
        reminder_id=reminder.id,
        plan_revision=plan.revision,
        sequence=step.sequence,
        step_code=step.code,
        state=plan.state,
    )
    return True


async def cancel_deadline_in_session(
    session: AsyncSession,
    reminder: Reminder,
    *,
    now_utc: datetime,
) -> None:
    plan = await session.scalar(
        select(ReminderDeadlinePlan)
        .where(ReminderDeadlinePlan.reminder_id == reminder.id)
        .with_for_update()
    )
    if plan is None:
        return
    pending = list(
        (
            await session.scalars(
                select(ReminderDeadlineStep)
                .where(
                    ReminderDeadlineStep.plan_id == plan.id,
                    ReminderDeadlineStep.revision == plan.revision,
                    ReminderDeadlineStep.state == DeadlineStepState.PENDING.value,
                )
                .with_for_update()
            )
        ).all()
    )
    for step in pending:
        step.state = DeadlineStepState.SKIPPED.value
        step.skip_reason = "cancelled"
    if pending:
        _record_deadline_event(
            "skipped",
            reminder_id=reminder.id,
            plan_revision=plan.revision,
            count=len(pending),
        )
    plan.state = DeadlinePlanState.CANCELLED.value
    plan.stop_reason = "user_cancelled"
    plan.cancelled_at = _as_utc(now_utc)
    plan.current_step_sequence = None
    _set_deadline_mirror(reminder, plan, None)
    _record_deadline_event(
        "cancelled",
        reminder_id=reminder.id,
        plan_revision=plan.revision,
        state=plan.state,
    )


async def edit_deadline_plan(
    user: User,
    reminder_id: int,
    request: DeadlineRequest,
    *,
    expected_revision: int,
    expected_occurrence_id: int | None = None,
    expected_occurrence_at_utc: datetime | None = None,
    expected_message_id: int | None = None,
    now_utc: datetime | None = None,
) -> Reminder | None:
    current_time = _as_utc(now_utc or utc_now())
    generated = _plan_from_request(user, request, now_utc=current_time)
    first = _first_pending(generated)
    if first is None:
        return None

    async with SessionLocal() as session, session.begin():
        target = await _load_owned_deadline_action(
            session,
            user,
            reminder_id,
            expected_revision=expected_revision,
            expected_occurrence_id=expected_occurrence_id,
            expected_occurrence_at_utc=expected_occurrence_at_utc,
            expected_message_id=expected_message_id,
        )
        if target is None:
            return None
        reminder, plan, _occurrence = target
        old_steps = list(
            (
                await session.scalars(
                    select(ReminderDeadlineStep)
                    .where(
                        ReminderDeadlineStep.plan_id == plan.id,
                        ReminderDeadlineStep.revision == plan.revision,
                        ReminderDeadlineStep.state == DeadlineStepState.PENDING.value,
                    )
                    .with_for_update()
                )
            ).all()
        )
        for step in old_steps:
            step.state = DeadlineStepState.SKIPPED.value
            step.skip_reason = "edited"

        plan.revision += 1
        plan.deadline_at_utc = generated.deadline_at_utc
        plan.schedule_timezone = generated.schedule_timezone
        plan.state = DeadlinePlanState.ACTIVE.value
        plan.stop_reason = None
        plan.completed_at = None
        plan.disabled_at = None
        plan.cancelled_at = None
        plan.current_step_sequence = generated.steps.index(first)
        plan.total_steps = len(generated.steps)
        plan.overdue_after_minutes = generated.overdue_after_minutes
        for sequence, planned_step in enumerate(generated.steps):
            session.add(
                ReminderDeadlineStep(
                    plan_id=plan.id,
                    revision=plan.revision,
                    sequence=sequence,
                    code=planned_step.code,
                    kind=planned_step.kind,
                    label=planned_step.label,
                    scheduled_at_utc=planned_step.scheduled_at_utc,
                    state=planned_step.state,
                    skip_reason=planned_step.skip_reason,
                )
            )
        current_row = ReminderDeadlineStep(
            plan_id=plan.id,
            revision=plan.revision,
            sequence=generated.steps.index(first),
            code=first.code,
            kind=first.kind,
            label=first.label,
            scheduled_at_utc=first.scheduled_at_utc,
            state=first.state,
            skip_reason=first.skip_reason,
        )
        reminder.text = request.text.strip()
        reminder.kind = ReminderKind.DEADLINE.value
        reminder.remind_at_utc = first.scheduled_at_utc
        reminder.delivery_at_utc = first.scheduled_at_utc
        reminder.schedule_timezone = generated.schedule_timezone
        reminder.recurrence_type = "none"
        reminder.recurrence_interval = 1
        reminder.recurrence_day_of_month = None
        reminder.recurrence_rule = None
        reminder.state = ReminderState.SCHEDULED.value
        reminder.status = "pending"
        reminder.snoozed_until_utc = None
        reminder.paused_at = None
        reminder.completed_at = None
        reminder.action_revision += 1
        _clear_delivery_identity(reminder)
        _set_deadline_mirror(reminder, plan, current_row)
        if old_steps:
            _record_deadline_event(
                "skipped",
                reminder_id=reminder.id,
                plan_revision=plan.revision - 1,
                count=len(old_steps),
            )
        _record_deadline_event(
            "planned",
            reminder_id=reminder.id,
            plan_revision=plan.revision,
            count=len(generated.steps),
        )
        new_skipped = sum(step.state == DeadlineStepState.SKIPPED.value for step in generated.steps)
        if new_skipped:
            _record_deadline_event(
                "skipped",
                reminder_id=reminder.id,
                plan_revision=plan.revision,
                count=new_skipped,
            )
            _record_deadline_event(
                "bounded_suppression",
                reminder_id=reminder.id,
                plan_revision=plan.revision,
                count=new_skipped,
            )
        return reminder


async def disable_deadline_plan(
    user: User,
    reminder_id: int,
    *,
    expected_revision: int,
    expected_occurrence_id: int | None = None,
    expected_occurrence_at_utc: datetime | None = None,
    expected_message_id: int | None = None,
    now_utc: datetime | None = None,
) -> bool:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        target = await _load_owned_deadline_action(
            session,
            user,
            reminder_id,
            expected_revision=expected_revision,
            expected_occurrence_id=expected_occurrence_id,
            expected_occurrence_at_utc=expected_occurrence_at_utc,
            expected_message_id=expected_message_id,
        )
        if target is None:
            return False
        reminder, plan, occurrence = target
        return await disable_deadline_in_session(
            session,
            reminder,
            plan,
            occurrence=occurrence,
            now_utc=current_time,
        )


async def enable_deadline_plan(
    user: User,
    reminder_id: int,
    *,
    expected_revision: int,
    expected_occurrence_id: int | None = None,
    expected_occurrence_at_utc: datetime | None = None,
    expected_message_id: int | None = None,
    now_utc: datetime | None = None,
) -> bool:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        target = await _load_owned_deadline_action(
            session,
            user,
            reminder_id,
            expected_revision=expected_revision,
            expected_occurrence_id=expected_occurrence_id,
            expected_occurrence_at_utc=expected_occurrence_at_utc,
            expected_message_id=expected_message_id,
        )
        if target is None:
            return False
        reminder, plan, _occurrence = target
        return await enable_deadline_in_session(
            session,
            reminder,
            plan,
            now_utc=current_time,
        )
