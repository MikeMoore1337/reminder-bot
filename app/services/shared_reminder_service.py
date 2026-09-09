from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    OccurrenceState,
    Reminder,
    ReminderDelivery,
    ReminderDeliveryState,
    ReminderKind,
    ReminderOccurrence,
    ReminderState,
    SharedInviteState,
    SharedMembershipState,
    SharedReminderInvite,
    SharedReminderMembership,
    User,
)
from app.db.session import SessionLocal
from app.services.timezone_service import is_private_chat_id
from app.utils.datetime_utils import utc_now

MAX_SHARED_PARTICIPANTS = 5
MAX_PENDING_SHARED_INVITES = 5
SHARED_PAGE_SIZE = 20
MAX_SHARED_PAGE_NUMBER = 1_000_000
SHARED_INVITE_TTL = timedelta(hours=24)
SHARED_INVITE_RETENTION = timedelta(days=7)
SHARED_MEMBERSHIP_RETENTION = timedelta(days=30)
SHARED_DELIVERY_RETENTION = timedelta(days=30)
SHARED_INVITE_PREFIX = "sr_"

_INVITE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{24,64}\Z")
_ACTIVE_REMINDER_STATES = (
    ReminderState.SCHEDULED.value,
    ReminderState.SNOOZED.value,
    ReminderState.DELIVERED.value,
)
_TERMINAL_REMINDER_STATES = (
    ReminderState.COMPLETED.value,
    ReminderState.CANCELLED.value,
    ReminderState.FAILED.value,
)


class SharedReminderError(ValueError):
    """A safe, user-facing shared-reminder validation failure."""


@dataclass(frozen=True, slots=True)
class SharedInvite:
    reminder_id: int
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class InviteAcceptance:
    accepted: bool
    already_member: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SharedMemberView:
    membership_id: int
    revision: int


@dataclass(frozen=True, slots=True)
class SharedInviteView:
    invite_id: int
    revision: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SharedReminderView:
    reminder: Reminder
    is_owner: bool
    participant_count: int
    members: tuple[SharedMemberView, ...] = ()
    pending_invites: tuple[SharedInviteView, ...] = ()
    membership_id: int | None = None
    membership_revision: int | None = None


@dataclass(frozen=True, slots=True)
class SharedReminderPage:
    items: tuple[SharedReminderView, ...]
    page: int
    page_size: int
    has_previous: bool
    has_next: bool


@dataclass(frozen=True, slots=True)
class SharedReminderCard:
    entry: SharedReminderView
    occurrence: ReminderOccurrence | None


@dataclass(frozen=True, slots=True)
class SharedOccurrenceTarget:
    reminder_id: int
    occurrence_id: int
    occurrence_at_utc: datetime
    is_owner: bool


@dataclass(frozen=True, slots=True)
class SharedDeliveryTarget:
    delivery_id: int
    reminder_id: int
    occurrence_id: int
    recipient_user_id: int
    chat_id: int
    membership_id: int | None
    membership_revision: int
    action_revision: int
    is_owner: bool
    lease_token: str
    attempt_count: int


@dataclass(frozen=True, slots=True)
class SharedDeliveryStatus:
    complete: bool
    owner_message_id: int | None
    action_revision: int | None = None
    successful_delivery_count: int = 0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_token(raw_token: str | None) -> str | None:
    if not isinstance(raw_token, str):
        return None
    token = raw_token.strip()
    if token.startswith(SHARED_INVITE_PREFIX):
        token = token[len(SHARED_INVITE_PREFIX) :]
    if not _INVITE_TOKEN_RE.fullmatch(token):
        return None
    return token


def invite_start_payload(token: str) -> str:
    normalized = _normalize_token(token)
    if normalized is None:
        raise ValueError("Invalid shared invite token")
    return f"{SHARED_INVITE_PREFIX}{normalized}"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _owner_matches(reminder: Reminder, user: User) -> bool:
    return reminder.user_id == user.id and reminder.chat_id == user.chat_id


def _shareable(reminder: Reminder) -> bool:
    """Return whether the deliberately small v1 shared contract applies."""

    return (
        reminder.kind == ReminderKind.ORDINARY.value
        and reminder.mode == "normal"
        and reminder.recurrence_type == "none"
        and reminder.parent_reminder_id is None
        and is_private_chat_id(reminder.chat_id)
        and reminder.state in _ACTIVE_REMINDER_STATES
        and reminder.status != "processing"
    )


async def _load_owner_reminder(
    session: AsyncSession,
    owner: User,
    reminder_id: int,
) -> Reminder | None:
    result = await session.execute(
        select(Reminder)
        .where(
            Reminder.id == reminder_id,
            Reminder.user_id == owner.id,
            Reminder.chat_id == owner.chat_id,
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def _load_shared_reminder(
    session: AsyncSession,
    user: User,
    reminder_id: int,
    *,
    for_update: bool,
) -> tuple[Reminder | None, SharedReminderMembership | None, bool]:
    statement = select(Reminder).where(Reminder.id == reminder_id)
    if for_update:
        statement = statement.with_for_update()
    reminder = await session.scalar(statement)
    if reminder is None or not _shareable(reminder):
        return None, None, False
    if _owner_matches(reminder, user):
        return reminder, None, True

    membership_statement = select(SharedReminderMembership).where(
        SharedReminderMembership.reminder_id == reminder_id,
        SharedReminderMembership.user_id == user.id,
        SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
    )
    if for_update:
        membership_statement = membership_statement.with_for_update()
    membership = await session.scalar(membership_statement)
    if membership is None:
        return None, None, False
    return reminder, membership, False


async def _active_participant_count(session: AsyncSession, reminder_id: int) -> int:
    value = await session.scalar(
        select(func.count(SharedReminderMembership.id)).where(
            SharedReminderMembership.reminder_id == reminder_id,
            SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
        )
    )
    return int(value or 0)


async def _pending_invite_count(
    session: AsyncSession,
    reminder_id: int,
    now_utc: datetime,
) -> int:
    value = await session.scalar(
        select(func.count(SharedReminderInvite.id)).where(
            SharedReminderInvite.reminder_id == reminder_id,
            SharedReminderInvite.state == SharedInviteState.PENDING.value,
            SharedReminderInvite.expires_at > now_utc,
        )
    )
    return int(value or 0)


async def _build_shared_reminder_view(
    session: AsyncSession,
    user: User,
    reminder: Reminder,
    *,
    current_time: datetime,
    membership_id: int | None = None,
    membership_revision: int | None = None,
) -> SharedReminderView:
    is_owner = _owner_matches(reminder, user)
    member_count = await _active_participant_count(session, reminder.id)
    members: tuple[SharedMemberView, ...] = ()
    pending_invites: tuple[SharedInviteView, ...] = ()
    if is_owner:
        member_result = await session.execute(
            select(SharedReminderMembership.id, SharedReminderMembership.revision)
            .where(
                SharedReminderMembership.reminder_id == reminder.id,
                SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
            )
            .order_by(SharedReminderMembership.id.asc())
            .limit(MAX_SHARED_PARTICIPANTS)
        )
        members = tuple(SharedMemberView(int(row[0]), int(row[1])) for row in member_result)
        invite_result = await session.execute(
            select(
                SharedReminderInvite.id,
                SharedReminderInvite.revision,
                SharedReminderInvite.expires_at,
            )
            .where(
                SharedReminderInvite.reminder_id == reminder.id,
                SharedReminderInvite.state == SharedInviteState.PENDING.value,
                SharedReminderInvite.expires_at > current_time,
            )
            .order_by(SharedReminderInvite.id.asc())
            .limit(MAX_PENDING_SHARED_INVITES)
        )
        pending_invites = tuple(
            SharedInviteView(int(row[0]), int(row[1]), row[2]) for row in invite_result
        )
    return SharedReminderView(
        reminder=reminder,
        is_owner=is_owner,
        participant_count=member_count,
        members=members,
        pending_invites=pending_invites,
        membership_id=None if is_owner else membership_id,
        membership_revision=None if is_owner else membership_revision,
    )


def _is_current_delivered_occurrence(
    reminder: Reminder,
    occurrence: ReminderOccurrence,
) -> bool:
    return (
        occurrence.status == OccurrenceState.DELIVERED.value
        and reminder.last_delivery_occurrence_utc is not None
        and _as_utc(reminder.last_delivery_occurrence_utc) == _as_utc(occurrence.occurrence_at_utc)
    )


async def _load_current_delivered_occurrence(
    session: AsyncSession,
    reminder: Reminder,
) -> ReminderOccurrence | None:
    if reminder.last_delivery_occurrence_utc is None:
        return None
    occurrence = cast(
        ReminderOccurrence | None,
        await session.scalar(
            select(ReminderOccurrence)
            .where(
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.occurrence_at_utc == reminder.last_delivery_occurrence_utc,
                ReminderOccurrence.status == OccurrenceState.DELIVERED.value,
            )
            .limit(1)
        ),
    )
    return occurrence


async def create_invite(
    owner: User,
    reminder_id: int,
    *,
    now_utc: datetime | None = None,
) -> SharedInvite:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        reminder = await _load_owner_reminder(session, owner, reminder_id)
        if reminder is None:
            raise SharedReminderError("Напоминание не найдено")
        if not _shareable(reminder):
            raise SharedReminderError(
                "В v1 можно делиться только обычным одноразовым активным напоминанием"
            )
        if await _active_participant_count(session, reminder.id) >= MAX_SHARED_PARTICIPANTS:
            raise SharedReminderError("Достигнут лимит участников этого напоминания")
        if await _pending_invite_count(session, reminder.id, current_time) >= (
            MAX_PENDING_SHARED_INVITES
        ):
            raise SharedReminderError("Слишком много активных приглашений")

        token: str | None = None
        token_hash: str | None = None
        for _ in range(3):
            candidate = secrets.token_urlsafe(24)
            candidate_hash = _token_hash(candidate)
            if (
                await session.scalar(
                    select(SharedReminderInvite.id).where(
                        SharedReminderInvite.token_hash == candidate_hash
                    )
                )
                is None
            ):
                token = candidate
                token_hash = candidate_hash
                break
        if token is None or token_hash is None:
            raise SharedReminderError("Не удалось создать приглашение")

        invite = SharedReminderInvite(
            reminder_id=reminder.id,
            owner_user_id=owner.id,
            token_hash=token_hash,
            state=SharedInviteState.PENDING.value,
            revision=1,
            expires_at=current_time + SHARED_INVITE_TTL,
        )
        session.add(invite)
        await session.flush()
        return SharedInvite(
            reminder_id=reminder.id,
            token=token,
            expires_at=invite.expires_at,
        )


async def accept_invite(
    user: User,
    raw_token: str | None,
    *,
    now_utc: datetime | None = None,
) -> InviteAcceptance:
    token = _normalize_token(raw_token)
    if token is None:
        return InviteAcceptance(False, reason="invalid")

    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        invite = await session.scalar(
            select(SharedReminderInvite)
            .where(SharedReminderInvite.token_hash == _token_hash(token))
            .with_for_update()
        )
        if invite is None:
            return InviteAcceptance(False, reason="invalid")

        reminder = await session.scalar(
            select(Reminder).where(Reminder.id == invite.reminder_id).with_for_update()
        )
        if reminder is None or not _shareable(reminder):
            invite.state = SharedInviteState.REVOKED.value
            invite.revoked_at = current_time
            invite.revision += 1
            return InviteAcceptance(False, reason="unavailable")

        if _owner_matches(reminder, user):
            return InviteAcceptance(False, reason="owner")

        membership = await session.scalar(
            select(SharedReminderMembership)
            .where(
                SharedReminderMembership.reminder_id == reminder.id,
                SharedReminderMembership.user_id == user.id,
            )
            .with_for_update()
        )

        if invite.state == SharedInviteState.ACCEPTED.value:
            if invite.accepted_by_user_id != user.id:
                return InviteAcceptance(False, reason="used")
            if membership is None or membership.state != SharedMembershipState.ACTIVE.value:
                return InviteAcceptance(False, reason="unavailable")
            return InviteAcceptance(
                False,
                already_member=True,
                reason="already_member",
            )
        if invite.state != SharedInviteState.PENDING.value:
            return InviteAcceptance(False, reason="invalid")
        if _as_utc(invite.expires_at) <= current_time:
            invite.state = SharedInviteState.EXPIRED.value
            invite.revision += 1
            return InviteAcceptance(False, reason="expired")

        if membership is not None and membership.state == SharedMembershipState.ACTIVE.value:
            invite.state = SharedInviteState.ACCEPTED.value
            invite.accepted_by_user_id = user.id
            invite.accepted_at = current_time
            invite.revision += 1
            return InviteAcceptance(False, already_member=True, reason="already_member")

        if await _active_participant_count(session, reminder.id) >= MAX_SHARED_PARTICIPANTS:
            return InviteAcceptance(False, reason="full")

        if membership is None:
            membership = SharedReminderMembership(
                reminder_id=reminder.id,
                user_id=user.id,
                role="participant",
                state=SharedMembershipState.ACTIVE.value,
                revision=1,
                joined_at=current_time,
            )
            session.add(membership)
        else:
            membership.state = SharedMembershipState.ACTIVE.value
            membership.role = "participant"
            membership.revision += 1
            membership.joined_at = current_time
            membership.revoked_at = None

        invite.state = SharedInviteState.ACCEPTED.value
        invite.accepted_by_user_id = user.id
        invite.accepted_at = current_time
        invite.revision += 1
        return InviteAcceptance(True, reason="accepted")


async def revoke_membership(
    owner: User,
    membership_id: int,
    *,
    expected_revision: int,
    now_utc: datetime | None = None,
) -> bool:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        membership = await session.scalar(
            select(SharedReminderMembership)
            .join(Reminder, Reminder.id == SharedReminderMembership.reminder_id)
            .where(
                SharedReminderMembership.id == membership_id,
                SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
                SharedReminderMembership.revision == expected_revision,
                Reminder.user_id == owner.id,
                Reminder.chat_id == owner.chat_id,
            )
            .with_for_update()
        )
        if membership is None:
            return False

        await _revoke_membership_in_session(
            session,
            membership,
            now_utc=current_time,
        )
        return True


async def revoke_invite(
    owner: User,
    invite_id: int,
    *,
    expected_revision: int,
    now_utc: datetime | None = None,
) -> bool:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        invite = await session.scalar(
            select(SharedReminderInvite)
            .join(Reminder, Reminder.id == SharedReminderInvite.reminder_id)
            .where(
                SharedReminderInvite.id == invite_id,
                SharedReminderInvite.state == SharedInviteState.PENDING.value,
                SharedReminderInvite.revision == expected_revision,
                Reminder.user_id == owner.id,
                Reminder.chat_id == owner.chat_id,
            )
            .with_for_update()
        )
        if invite is None:
            return False
        if _as_utc(invite.expires_at) <= current_time:
            invite.state = SharedInviteState.EXPIRED.value
            invite.revision += 1
            return False
        invite.state = SharedInviteState.REVOKED.value
        invite.revoked_at = current_time
        invite.revision += 1
        return True


async def _revoke_membership_in_session(
    session: AsyncSession,
    membership: SharedReminderMembership,
    *,
    now_utc: datetime,
) -> None:
    """Revoke one locked membership and fence its unfinished deliveries."""

    old_revision = membership.revision
    membership.state = SharedMembershipState.REVOKED.value
    membership.revoked_at = _as_utc(now_utc)
    membership.revision += 1
    await session.execute(
        update(ReminderDelivery)
        .where(
            ReminderDelivery.reminder_id == membership.reminder_id,
            ReminderDelivery.recipient_user_id == membership.user_id,
            ReminderDelivery.membership_revision == old_revision,
            ReminderDelivery.state.in_(
                (
                    ReminderDeliveryState.PENDING.value,
                    ReminderDeliveryState.PROCESSING.value,
                )
            ),
        )
        .values(
            state=ReminderDeliveryState.CANCELLED.value,
            lease_until=None,
            lease_token=None,
        )
    )


async def expire_terminal_memberships_in_session(
    session: AsyncSession,
    reminder_id: int,
    *,
    now_utc: datetime,
) -> int:
    """Turn active participant memberships into retention-safe tombstones."""

    result = await session.execute(
        select(SharedReminderMembership)
        .where(
            SharedReminderMembership.reminder_id == reminder_id,
            SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
        )
        .with_for_update()
    )
    memberships = list(result.scalars())
    for membership in memberships:
        await _revoke_membership_in_session(session, membership, now_utc=now_utc)
    return len(memberships)


async def _list_shared_reminders(
    user: User,
    *,
    limit: int,
    offset: int,
) -> list[SharedReminderView]:
    async with SessionLocal() as session:
        current_time = _as_utc(utc_now())
        participant_exists = exists(
            select(SharedReminderMembership.id).where(
                SharedReminderMembership.reminder_id == Reminder.id,
                SharedReminderMembership.user_id == user.id,
                SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
            )
        )
        owner_shared_exists = exists(
            select(SharedReminderMembership.id).where(
                SharedReminderMembership.reminder_id == Reminder.id,
                SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
            )
        )
        owner_invite_exists = exists(
            select(SharedReminderInvite.id).where(
                SharedReminderInvite.reminder_id == Reminder.id,
                SharedReminderInvite.state == SharedInviteState.PENDING.value,
                SharedReminderInvite.expires_at > current_time,
            )
        )
        result = await session.execute(
            select(Reminder)
            .where(
                Reminder.state.in_(_ACTIVE_REMINDER_STATES),
                or_(
                    participant_exists,
                    (Reminder.user_id == user.id)
                    & (Reminder.chat_id == user.chat_id)
                    & (owner_shared_exists | owner_invite_exists),
                ),
            )
            .order_by(
                func.coalesce(Reminder.delivery_at_utc, Reminder.remind_at_utc).asc(),
                Reminder.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        reminders = list(result.scalars().all())
        membership_by_reminder: dict[int, tuple[int, int]] = {}
        if reminders:
            membership_result = await session.execute(
                select(
                    SharedReminderMembership.reminder_id,
                    SharedReminderMembership.id,
                    SharedReminderMembership.revision,
                ).where(
                    SharedReminderMembership.reminder_id.in_(
                        [reminder.id for reminder in reminders]
                    ),
                    SharedReminderMembership.user_id == user.id,
                    SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
                )
            )
            membership_by_reminder = {
                int(row[0]): (int(row[1]), int(row[2])) for row in membership_result
            }
        views = [
            await _build_shared_reminder_view(
                session,
                user,
                reminder,
                current_time=current_time,
                membership_id=membership_by_reminder.get(reminder.id, (None, None))[0],
                membership_revision=membership_by_reminder.get(reminder.id, (None, None))[1],
            )
            for reminder in reminders
        ]
        return views


async def list_shared_reminders(
    user: User,
    *,
    limit: int = SHARED_PAGE_SIZE,
    offset: int = 0,
) -> list[SharedReminderView]:
    """Return a bounded compatibility slice of shared reminders."""

    if limit < 1:
        return []
    return await _list_shared_reminders(
        user,
        limit=min(limit, SHARED_PAGE_SIZE),
        offset=max(offset, 0),
    )


async def list_shared_reminders_page(
    user: User,
    *,
    page: int = 1,
    page_size: int = SHARED_PAGE_SIZE,
) -> SharedReminderPage:
    """Load one deterministic, bounded page plus one probe row for navigation."""

    bounded_page = min(max(page, 1), MAX_SHARED_PAGE_NUMBER)
    bounded_page_size = min(max(page_size, 1), SHARED_PAGE_SIZE)
    views = await _list_shared_reminders(
        user,
        limit=bounded_page_size + 1,
        offset=(bounded_page - 1) * bounded_page_size,
    )
    return SharedReminderPage(
        items=tuple(views[:bounded_page_size]),
        page=bounded_page,
        page_size=bounded_page_size,
        has_previous=bounded_page > 1,
        has_next=len(views) > bounded_page_size,
    )


async def get_shared_reminder_card(
    user: User,
    reminder_id: int,
) -> SharedReminderCard | None:
    """Return a fresh authorized view and occurrence immediately before rendering."""

    current_time = _as_utc(utc_now())
    async with SessionLocal() as session:
        reminder, _membership, _is_owner = await _load_shared_reminder(
            session,
            user,
            reminder_id,
            for_update=False,
        )
        if reminder is None:
            return None
        entry = await _build_shared_reminder_view(
            session,
            user,
            reminder,
            current_time=current_time,
            membership_id=_membership.id if _membership is not None else None,
            membership_revision=_membership.revision if _membership is not None else None,
        )
        occurrence = await _load_current_delivered_occurrence(session, reminder)
        return SharedReminderCard(entry=entry, occurrence=occurrence)


async def get_shared_occurrence(
    user: User,
    reminder_id: int,
) -> ReminderOccurrence | None:
    async with SessionLocal() as session:
        reminder, _membership, _is_owner = await _load_shared_reminder(
            session,
            user,
            reminder_id,
            for_update=False,
        )
        if reminder is None:
            return None
        return await _load_current_delivered_occurrence(session, reminder)


async def resolve_shared_occurrence_target(
    user: User,
    occurrence_id: int,
) -> SharedOccurrenceTarget | None:
    async with SessionLocal() as session:
        occurrence = await session.scalar(
            select(ReminderOccurrence).where(ReminderOccurrence.id == occurrence_id)
        )
        if occurrence is None:
            return None
        reminder, _membership, is_owner = await _load_shared_reminder(
            session,
            user,
            occurrence.reminder_id,
            for_update=False,
        )
        if reminder is None or not _is_current_delivered_occurrence(reminder, occurrence):
            return None
        return SharedOccurrenceTarget(
            reminder_id=reminder.id,
            occurrence_id=occurrence.id,
            occurrence_at_utc=occurrence.occurrence_at_utc,
            is_owner=is_owner,
        )


async def _load_shared_action_target(
    session: AsyncSession,
    user: User,
    reminder_id: int,
    occurrence_id: int,
    *,
    expected_revision: int,
    expected_occurrence_at_utc: datetime | None,
    expected_message_id: int | None,
    expected_membership_id: int | None,
    expected_membership_revision: int | None,
) -> tuple[Reminder, ReminderOccurrence, SharedReminderMembership] | None:
    reminder, membership, is_owner = await _load_shared_reminder(
        session,
        user,
        reminder_id,
        for_update=True,
    )
    if reminder is None or membership is None or is_owner:
        return None
    if (expected_membership_id is None) != (expected_membership_revision is None):
        return None
    if expected_membership_id is not None and (
        membership.id != expected_membership_id
        or membership.revision != expected_membership_revision
    ):
        return None
    if expected_message_id is None and expected_membership_id is None:
        return None
    occurrence = await session.scalar(
        select(ReminderOccurrence)
        .where(
            ReminderOccurrence.id == occurrence_id,
            ReminderOccurrence.reminder_id == reminder.id,
        )
        .with_for_update()
    )
    if occurrence is None:
        return None
    if (
        occurrence.action_revision != expected_revision
        or not _is_current_delivered_occurrence(reminder, occurrence)
        or reminder.state
        in {
            ReminderState.COMPLETED.value,
            ReminderState.CANCELLED.value,
            ReminderState.FAILED.value,
        }
        or reminder.status == "processing"
    ):
        return None
    if expected_occurrence_at_utc is not None and _as_utc(expected_occurrence_at_utc) != _as_utc(
        occurrence.occurrence_at_utc
    ):
        return None
    if expected_message_id is not None:
        delivery = await session.scalar(
            select(ReminderDelivery).where(
                ReminderDelivery.reminder_id == reminder.id,
                ReminderDelivery.occurrence_id == occurrence.id,
                ReminderDelivery.recipient_user_id == user.id,
                ReminderDelivery.membership_revision == membership.revision,
                ReminderDelivery.action_revision == expected_revision,
                ReminderDelivery.state == ReminderDeliveryState.SENT.value,
                ReminderDelivery.message_id == expected_message_id,
            )
        )
        if delivery is None:
            return None
    return reminder, occurrence, membership


async def validate_shared_action_target(
    user: User,
    reminder_id: int,
    occurrence_id: int,
    *,
    expected_revision: int,
    expected_occurrence_at_utc: datetime | None = None,
    expected_message_id: int | None = None,
    expected_membership_id: int | None = None,
    expected_membership_revision: int | None = None,
) -> bool:
    async with SessionLocal() as session, session.begin():
        return (
            await _load_shared_action_target(
                session,
                user,
                reminder_id,
                occurrence_id,
                expected_revision=expected_revision,
                expected_occurrence_at_utc=expected_occurrence_at_utc,
                expected_message_id=expected_message_id,
                expected_membership_id=expected_membership_id,
                expected_membership_revision=expected_membership_revision,
            )
            is not None
        )


async def complete_shared_reminder(
    user: User,
    reminder_id: int,
    occurrence_id: int,
    *,
    expected_revision: int,
    expected_occurrence_at_utc: datetime | None = None,
    expected_message_id: int | None = None,
    expected_membership_id: int | None = None,
    expected_membership_revision: int | None = None,
    now_utc: datetime | None = None,
) -> bool:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        target = await _load_shared_action_target(
            session,
            user,
            reminder_id,
            occurrence_id,
            expected_revision=expected_revision,
            expected_occurrence_at_utc=expected_occurrence_at_utc,
            expected_message_id=expected_message_id,
            expected_membership_id=expected_membership_id,
            expected_membership_revision=expected_membership_revision,
        )
        if target is None:
            return False
        reminder, occurrence, _membership = target
        occurrence.status = OccurrenceState.COMPLETED.value
        occurrence.completed_at = current_time
        occurrence.action_revision += 1
        reminder.state = ReminderState.COMPLETED.value
        reminder.status = "sent"
        reminder.completed_at = current_time
        reminder.delivery_at_utc = None
        reminder.snoozed_until_utc = None
        reminder.last_message_id = None
        reminder.last_delivery_occurrence_utc = occurrence.occurrence_at_utc
        reminder.action_revision += 1
        reminder.retry_count = 0
        reminder.attempt_count = 0
        reminder.next_retry_at = None
        reminder.error_text = None
        await expire_terminal_memberships_in_session(
            session,
            reminder.id,
            now_utc=current_time,
        )
        await session.execute(
            update(ReminderDelivery)
            .where(
                ReminderDelivery.occurrence_id == occurrence.id,
                ReminderDelivery.state.in_(
                    (
                        ReminderDeliveryState.PENDING.value,
                        ReminderDeliveryState.PROCESSING.value,
                    )
                ),
            )
            .values(
                state=ReminderDeliveryState.CANCELLED.value,
                lease_until=None,
                lease_token=None,
            )
        )
        return True


async def snooze_shared_reminder(
    user: User,
    reminder_id: int,
    occurrence_id: int,
    target_at_utc: datetime,
    *,
    expected_revision: int,
    expected_occurrence_at_utc: datetime | None = None,
    expected_message_id: int | None = None,
    expected_membership_id: int | None = None,
    expected_membership_revision: int | None = None,
    now_utc: datetime | None = None,
) -> bool:
    current_time = _as_utc(now_utc or utc_now())
    target_time = _as_utc(target_at_utc)
    if target_time <= current_time:
        return False
    async with SessionLocal() as session, session.begin():
        target = await _load_shared_action_target(
            session,
            user,
            reminder_id,
            occurrence_id,
            expected_revision=expected_revision,
            expected_occurrence_at_utc=expected_occurrence_at_utc,
            expected_message_id=expected_message_id,
            expected_membership_id=expected_membership_id,
            expected_membership_revision=expected_membership_revision,
        )
        if target is None:
            return False
        reminder, occurrence, _membership = target
        occurrence.status = OccurrenceState.SNOOZED.value
        occurrence.snoozed_until_utc = target_time
        occurrence.action_revision += 1
        reminder.state = ReminderState.SNOOZED.value
        reminder.status = "pending"
        reminder.delivery_at_utc = target_time
        reminder.snoozed_until_utc = target_time
        reminder.action_revision += 1
        reminder.last_message_id = None
        reminder.last_delivery_occurrence_utc = None
        reminder.retry_count = 0
        reminder.attempt_count = 0
        reminder.next_retry_at = None
        reminder.error_text = None
        await session.execute(
            update(ReminderDelivery)
            .where(ReminderDelivery.occurrence_id == occurrence.id)
            .values(
                state=ReminderDeliveryState.PENDING.value,
                action_revision=0,
                attempt_count=0,
                lease_until=None,
                lease_token=None,
                message_id=None,
                sent_at=None,
                last_error=None,
                error_kind=None,
            )
        )
        return True


async def is_shared_reminder(
    reminder_id: int,
    *,
    session_factory: Any | None = None,
) -> bool:
    factory = session_factory or SessionLocal
    async with factory() as session:
        return bool(
            await session.scalar(
                select(
                    exists().where(
                        SharedReminderMembership.reminder_id == reminder_id,
                        SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
                    )
                )
            )
        )


async def _delivery_specs(
    session: AsyncSession,
    reminder: Reminder,
) -> list[tuple[int, int, int | None, int, bool]]:
    if not is_private_chat_id(reminder.chat_id):
        raise SharedReminderError("Общая доставка требует подтверждённого личного чата владельца")
    specs: list[tuple[int, int, int | None, int, bool]] = [
        (reminder.user_id, reminder.chat_id, None, 0, True)
    ]
    result = await session.execute(
        select(
            SharedReminderMembership.user_id,
            User.chat_id,
            SharedReminderMembership.id,
            SharedReminderMembership.revision,
        )
        .join(User, User.id == SharedReminderMembership.user_id)
        .where(
            SharedReminderMembership.reminder_id == reminder.id,
            SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
        )
        .order_by(SharedReminderMembership.id.asc())
        .limit(MAX_SHARED_PARTICIPANTS + 1)
    )
    rows = list(result)
    if len(rows) > MAX_SHARED_PARTICIPANTS:
        raise SharedReminderError("Превышен безопасный лимит участников")
    for row in rows:
        participant_chat_id = int(row[1])
        if not is_private_chat_id(participant_chat_id):
            raise SharedReminderError("Общая доставка требует подтверждённых личных чатов")
        specs.append((int(row[0]), participant_chat_id, int(row[2]), int(row[3]), False))
    return specs


def _clear_delivery_claim(delivery: ReminderDelivery) -> None:
    delivery.lease_until = None
    delivery.lease_token = None


async def reconcile_shared_delivery_after_recovery(
    session: AsyncSession,
    reminder: Reminder,
    *,
    now_utc: datetime,
) -> bool:
    """Finalize a recovered shared occurrence from durable recipient state.

    A worker can stop after Telegram accepted one recipient but before the
    parent reminder finalization commits.  When that claim is recovered at the
    attempt limit, SENT rows are authoritative: keep their callbacks and make
    only the unfinished recipient rows terminal.
    """

    if (
        reminder.status != "processing"
        or reminder.kind != ReminderKind.ORDINARY.value
        or reminder.mode != "normal"
        or reminder.recurrence_type != "none"
        or reminder.parent_reminder_id is not None
        or not is_private_chat_id(reminder.chat_id)
    ):
        return False

    occurrence = await session.scalar(
        select(ReminderOccurrence)
        .where(
            ReminderOccurrence.reminder_id == reminder.id,
            ReminderOccurrence.occurrence_at_utc == reminder.remind_at_utc,
        )
        .with_for_update()
    )
    if occurrence is None:
        return False

    deliveries = list(
        (
            await session.scalars(
                select(ReminderDelivery)
                .where(ReminderDelivery.occurrence_id == occurrence.id)
                .with_for_update()
            )
        ).all()
    )
    sent_deliveries = [
        delivery for delivery in deliveries if delivery.state == ReminderDeliveryState.SENT.value
    ]
    if not sent_deliveries:
        return False

    current_time = _as_utc(now_utc)
    owner_delivery = next(
        (
            delivery
            for delivery in sent_deliveries
            if delivery.recipient_user_id == reminder.user_id and delivery.membership_revision == 0
        ),
        None,
    )
    sent_times = [
        _as_utc(delivery.sent_at) for delivery in sent_deliveries if delivery.sent_at is not None
    ]
    delivered_at = min(sent_times, default=current_time)
    occurrence.status = OccurrenceState.DELIVERED.value
    occurrence.message_id = owner_delivery.message_id if owner_delivery else None
    occurrence.delivered_at = delivered_at
    occurrence.snoozed_until_utc = None

    reminder.status = "sent"
    reminder.state = ReminderState.DELIVERED.value
    reminder.sent_at = delivered_at
    reminder.error_text = None
    reminder.last_message_id = occurrence.message_id
    reminder.last_delivery_occurrence_utc = occurrence.occurrence_at_utc
    reminder.delivery_at_utc = None
    reminder.snoozed_until_utc = None
    reminder.next_retry_at = None
    reminder.processing_started_at = None
    reminder.lease_until = None
    reminder.lease_token = None

    terminal_error = "delivery attempt limit exhausted"
    for delivery in deliveries:
        if delivery.state == ReminderDeliveryState.SENT.value:
            continue
        if delivery.state in {
            ReminderDeliveryState.PENDING.value,
            ReminderDeliveryState.PROCESSING.value,
            ReminderDeliveryState.FAILED.value,
        }:
            delivery.state = ReminderDeliveryState.FAILED.value
            delivery.last_error = delivery.last_error or terminal_error
            delivery.error_kind = "terminal"
            _clear_delivery_claim(delivery)
    return True


def _reset_delivery_row(delivery: ReminderDelivery, action_revision: int) -> None:
    delivery.action_revision = action_revision
    delivery.state = ReminderDeliveryState.PENDING.value
    delivery.attempt_count = 0
    delivery.message_id = None
    delivery.sent_at = None
    delivery.last_error = None
    delivery.error_kind = None
    _clear_delivery_claim(delivery)


async def prepare_shared_delivery_recipients(
    reminder_id: int,
    occurrence_id: int,
    reminder_lease_token: str,
    *,
    now_utc: datetime | None = None,
) -> list[int]:
    if not reminder_lease_token:
        return []
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        reminder = await session.scalar(
            select(Reminder)
            .where(
                Reminder.id == reminder_id,
                Reminder.status == "processing",
                Reminder.lease_token == reminder_lease_token,
                Reminder.lease_until > current_time,
            )
            .with_for_update()
        )
        if reminder is None:
            return []
        occurrence = await session.scalar(
            select(ReminderOccurrence)
            .where(
                ReminderOccurrence.id == occurrence_id,
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.status == OccurrenceState.PROCESSING.value,
            )
            .with_for_update()
        )
        if occurrence is None:
            return []

        specs = await _delivery_specs(session, reminder)
        existing_result = await session.execute(
            select(ReminderDelivery)
            .where(ReminderDelivery.occurrence_id == occurrence.id)
            .with_for_update()
        )
        existing = {
            (row.recipient_user_id, row.membership_revision): row
            for row in existing_result.scalars()
        }
        sent_action_revisions = sorted(
            {
                row.action_revision
                for row in existing.values()
                if row.state == ReminderDeliveryState.SENT.value
            }
        )
        if len(sent_action_revisions) > 1:
            raise SharedReminderError("Несогласованные поколения доставки")
        shared_action_revision = occurrence.action_revision
        if sent_action_revisions:
            # A semantic edit may have advanced the occurrence generation
            # after an earlier recipient was sent.  A retry may only advance
            # this generation, never restore the older SENT-row revision.
            shared_action_revision = max(shared_action_revision, sent_action_revisions[0])
        # The parent revision identifies the worker claim. Once one recipient
        # has received Telegram controls, the occurrence revision becomes the
        # stable callback generation for every recipient of this occurrence.
        # A retry must not rewrite the generation persisted with an already
        # sent message.
        occurrence.action_revision = max(occurrence.action_revision, shared_action_revision)
        delivery_ids: list[int] = []
        for recipient_user_id, chat_id, _membership_id, membership_revision, _is_owner in specs:
            row = existing.get((recipient_user_id, membership_revision))
            if row is None:
                row = ReminderDelivery(
                    reminder_id=reminder.id,
                    occurrence_id=occurrence.id,
                    recipient_user_id=recipient_user_id,
                    membership_revision=membership_revision,
                    action_revision=shared_action_revision,
                    chat_id=chat_id,
                    state=ReminderDeliveryState.PENDING.value,
                )
                session.add(row)
                await session.flush()
            else:
                row.chat_id = chat_id
                if row.state == ReminderDeliveryState.SENT.value:
                    # Preserve the callback generation encoded in the
                    # recipient's already delivered Telegram message.
                    pass
                elif (
                    row.state == ReminderDeliveryState.FAILED.value and row.error_kind == "terminal"
                ):
                    # A terminal recipient failure must not be retried merely
                    # because another recipient caused a parent retry.
                    pass
                elif (
                    row.action_revision != shared_action_revision
                    or (
                        row.state == ReminderDeliveryState.PROCESSING.value
                        and (row.lease_until is None or row.lease_until <= current_time)
                    )
                    or (
                        row.state == ReminderDeliveryState.FAILED.value
                        and row.error_kind != "terminal"
                    )
                ):
                    _reset_delivery_row(row, shared_action_revision)
            if row.state != ReminderDeliveryState.SENT.value and row.state != (
                ReminderDeliveryState.FAILED.value
            ):
                delivery_ids.append(row.id)
        return delivery_ids


async def claim_shared_delivery(
    delivery_id: int,
    reminder_id: int,
    occurrence_id: int,
    reminder_lease_token: str,
    *,
    now_utc: datetime | None = None,
    lease_seconds: int = 60,
) -> SharedDeliveryTarget | None:
    if not reminder_lease_token:
        return None
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        reminder = await session.scalar(
            select(Reminder)
            .where(
                Reminder.id == reminder_id,
                Reminder.status == "processing",
                Reminder.lease_token == reminder_lease_token,
                Reminder.lease_until > current_time,
            )
            .with_for_update()
        )
        if reminder is None:
            return None
        occurrence = await session.scalar(
            select(ReminderOccurrence).where(
                ReminderOccurrence.id == occurrence_id,
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.status == OccurrenceState.PROCESSING.value,
            )
        )
        if occurrence is None:
            return None
        delivery = await session.scalar(
            select(ReminderDelivery)
            .where(
                ReminderDelivery.id == delivery_id,
                ReminderDelivery.reminder_id == reminder.id,
                ReminderDelivery.occurrence_id == occurrence.id,
            )
            .with_for_update()
        )
        if delivery is None:
            return None
        if delivery.state == ReminderDeliveryState.SENT.value or (
            delivery.state == ReminderDeliveryState.FAILED.value
            and delivery.error_kind == "terminal"
        ):
            return None
        if delivery.action_revision != occurrence.action_revision:
            _reset_delivery_row(delivery, occurrence.action_revision)
        if delivery.state == ReminderDeliveryState.PROCESSING.value and (
            delivery.lease_until is not None and _as_utc(delivery.lease_until) > current_time
        ):
            return None
        if delivery.recipient_user_id != reminder.user_id or delivery.membership_revision != 0:
            membership = await session.scalar(
                select(SharedReminderMembership).where(
                    SharedReminderMembership.reminder_id == reminder.id,
                    SharedReminderMembership.user_id == delivery.recipient_user_id,
                    SharedReminderMembership.revision == delivery.membership_revision,
                    SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
                )
            )
            if membership is None:
                delivery.state = ReminderDeliveryState.CANCELLED.value
                _clear_delivery_claim(delivery)
                return None
        else:
            membership = None

        row_token = secrets.token_hex(32)
        delivery.state = ReminderDeliveryState.PROCESSING.value
        delivery.attempt_count += 1
        delivery.lease_until = current_time + timedelta(seconds=max(1, lease_seconds))
        delivery.lease_token = row_token
        return SharedDeliveryTarget(
            delivery_id=delivery.id,
            reminder_id=reminder.id,
            occurrence_id=occurrence.id,
            recipient_user_id=delivery.recipient_user_id,
            chat_id=delivery.chat_id,
            membership_id=membership.id if membership is not None else None,
            membership_revision=delivery.membership_revision,
            action_revision=delivery.action_revision,
            is_owner=delivery.recipient_user_id == reminder.user_id
            and delivery.membership_revision == 0,
            lease_token=row_token,
            attempt_count=delivery.attempt_count,
        )


async def validate_shared_delivery_before_send(
    target: SharedDeliveryTarget,
    *,
    reminder_lease_token: str,
    now_utc: datetime | None = None,
) -> bool:
    """Revalidate a claimed row after other shared state may have changed."""

    if not reminder_lease_token or not target.lease_token:
        return False
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():

        async def fence_target() -> None:
            await session.execute(
                update(ReminderDelivery)
                .where(
                    ReminderDelivery.id == target.delivery_id,
                    ReminderDelivery.reminder_id == target.reminder_id,
                    ReminderDelivery.occurrence_id == target.occurrence_id,
                    ReminderDelivery.state == ReminderDeliveryState.PROCESSING.value,
                    ReminderDelivery.lease_token == target.lease_token,
                )
                .values(
                    state=ReminderDeliveryState.CANCELLED.value,
                    lease_until=None,
                    lease_token=None,
                )
            )

        reminder = await session.scalar(
            select(Reminder).where(Reminder.id == target.reminder_id).with_for_update()
        )
        if reminder is None:
            return False
        if (
            reminder.status != "processing"
            or reminder.state not in (ReminderState.SCHEDULED.value, ReminderState.SNOOZED.value)
            or reminder.lease_token != reminder_lease_token
            or reminder.lease_until is None
            or _as_utc(reminder.lease_until) <= current_time
        ):
            await fence_target()
            return False
        occurrence = await session.scalar(
            select(ReminderOccurrence).where(
                ReminderOccurrence.id == target.occurrence_id,
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.status == OccurrenceState.PROCESSING.value,
                ReminderOccurrence.action_revision == target.action_revision,
            )
        )
        if occurrence is None:
            await fence_target()
            return False
        delivery = await session.scalar(
            select(ReminderDelivery)
            .where(
                ReminderDelivery.id == target.delivery_id,
                ReminderDelivery.reminder_id == reminder.id,
                ReminderDelivery.occurrence_id == occurrence.id,
                ReminderDelivery.recipient_user_id == target.recipient_user_id,
                ReminderDelivery.membership_revision == target.membership_revision,
                ReminderDelivery.action_revision == target.action_revision,
                ReminderDelivery.chat_id == target.chat_id,
                ReminderDelivery.state == ReminderDeliveryState.PROCESSING.value,
                ReminderDelivery.lease_token == target.lease_token,
                ReminderDelivery.lease_until > current_time,
            )
            .with_for_update()
        )
        if delivery is None:
            await fence_target()
            return False

        is_owner = (
            delivery.recipient_user_id == reminder.user_id and delivery.membership_revision == 0
        )
        if is_owner != target.is_owner:
            await fence_target()
            return False
        if is_owner:
            return True

        membership = await session.scalar(
            select(SharedReminderMembership).where(
                SharedReminderMembership.reminder_id == reminder.id,
                SharedReminderMembership.user_id == delivery.recipient_user_id,
                SharedReminderMembership.revision == delivery.membership_revision,
                SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
            )
        )
        if membership is None:
            await fence_target()
            return False
        if target.membership_id is None or membership.id != target.membership_id:
            await fence_target()
            return False
        return True


async def record_shared_delivery_success(
    target: SharedDeliveryTarget,
    message_id: int,
    *,
    reminder_lease_token: str,
    now_utc: datetime | None = None,
) -> bool:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        reminder = await session.scalar(
            select(Reminder)
            .where(
                Reminder.id == target.reminder_id,
                Reminder.status == "processing",
                Reminder.lease_token == reminder_lease_token,
                Reminder.lease_until > current_time,
            )
            .with_for_update()
        )
        if reminder is None:
            return False
        delivery = await session.scalar(
            select(ReminderDelivery)
            .where(
                ReminderDelivery.id == target.delivery_id,
                ReminderDelivery.reminder_id == target.reminder_id,
                ReminderDelivery.occurrence_id == target.occurrence_id,
                ReminderDelivery.state == ReminderDeliveryState.PROCESSING.value,
                ReminderDelivery.lease_token == target.lease_token,
                ReminderDelivery.lease_until > current_time,
            )
            .with_for_update()
        )
        if delivery is None:
            return False
        delivery.state = ReminderDeliveryState.SENT.value
        delivery.message_id = message_id
        delivery.sent_at = current_time
        delivery.last_error = None
        delivery.error_kind = None
        _clear_delivery_claim(delivery)
        return True


async def record_shared_delivery_failure(
    target: SharedDeliveryTarget,
    *,
    error_kind: str,
    error_type: str,
    reminder_lease_token: str,
    now_utc: datetime | None = None,
) -> bool:
    current_time = _as_utc(now_utc or utc_now())
    bounded_kind = "terminal" if error_kind == "terminal" else "transient"
    async with SessionLocal() as session, session.begin():
        reminder = await session.scalar(
            select(Reminder).where(
                Reminder.id == target.reminder_id,
                Reminder.status == "processing",
                Reminder.lease_token == reminder_lease_token,
                Reminder.lease_until > current_time,
            )
        )
        if reminder is None:
            return False
        delivery = await session.scalar(
            select(ReminderDelivery)
            .where(
                ReminderDelivery.id == target.delivery_id,
                ReminderDelivery.state == ReminderDeliveryState.PROCESSING.value,
                ReminderDelivery.lease_token == target.lease_token,
                ReminderDelivery.lease_until > current_time,
            )
            .with_for_update()
        )
        if delivery is None:
            return False
        delivery.state = ReminderDeliveryState.FAILED.value
        delivery.last_error = error_type[:128]
        delivery.error_kind = bounded_kind
        _clear_delivery_claim(delivery)
        return True


async def get_shared_delivery_status(
    reminder_id: int,
    occurrence_id: int,
    *,
    action_revision: int,
) -> SharedDeliveryStatus:
    async with SessionLocal() as session:
        reminder = await session.get(Reminder, reminder_id)
        if reminder is None:
            return SharedDeliveryStatus(False, None)
        occurrence = await session.scalar(
            select(ReminderOccurrence).where(
                ReminderOccurrence.id == occurrence_id,
                ReminderOccurrence.reminder_id == reminder.id,
            )
        )
        if occurrence is None:
            return SharedDeliveryStatus(False, None)
        # ``reminder.action_revision`` is the current worker claim generation.
        # Shared callbacks use the occurrence generation, which remains stable
        # across a partial retry after any recipient has already been sent.
        effective_action_revision = occurrence.action_revision
        specs = await _delivery_specs(session, reminder)
        result = await session.execute(
            select(ReminderDelivery).where(ReminderDelivery.occurrence_id == occurrence_id)
        )
        rows = {(row.recipient_user_id, row.membership_revision): row for row in result.scalars()}
        owner_message_id: int | None = None
        successful_delivery_count = 0
        complete = True
        for recipient_user_id, _chat_id, _membership_id, membership_revision, is_owner in specs:
            row = rows.get((recipient_user_id, membership_revision))
            if row is None:
                complete = False
                continue
            if row.state == ReminderDeliveryState.SENT.value:
                if row.action_revision > effective_action_revision:
                    complete = False
                    continue
                # A semantic edit can advance the occurrence generation
                # without redelivering a recipient that already received the
                # old message.  The old message remains a successful delivery;
                # the newer occurrence revision fences its old callbacks.
                if is_owner:
                    owner_message_id = row.message_id
                successful_delivery_count += 1
                continue
            if row.state == ReminderDeliveryState.FAILED.value and row.error_kind == "terminal":
                continue
            if row.action_revision != effective_action_revision:
                complete = False
                continue
            complete = False
        return SharedDeliveryStatus(
            complete,
            owner_message_id,
            effective_action_revision,
            successful_delivery_count,
        )


async def _expire_terminal_memberships_set_based(
    session: AsyncSession,
    *,
    now_utc: datetime,
) -> None:
    """Revoke only memberships whose parent reminder is terminal."""

    terminal_reminder_exists = exists(
        select(Reminder.id).where(
            Reminder.id == SharedReminderMembership.reminder_id,
            Reminder.state.in_(_TERMINAL_REMINDER_STATES),
        )
    )
    terminal_at = (
        select(
            func.coalesce(
                case(
                    (
                        Reminder.state == ReminderState.COMPLETED.value,
                        Reminder.completed_at,
                    ),
                    (
                        Reminder.state == ReminderState.CANCELLED.value,
                        Reminder.cancelled_at,
                    ),
                    else_=None,
                ),
                now_utc,
            )
        )
        .where(
            Reminder.id == SharedReminderMembership.reminder_id,
            Reminder.state.in_(_TERMINAL_REMINDER_STATES),
        )
        .scalar_subquery()
    )
    await session.execute(
        update(SharedReminderMembership)
        .where(
            SharedReminderMembership.state == SharedMembershipState.ACTIVE.value,
            terminal_reminder_exists,
        )
        .values(
            state=SharedMembershipState.REVOKED.value,
            revoked_at=terminal_at,
            revision=SharedReminderMembership.revision + 1,
        )
    )

    terminal_membership_exists = exists(
        select(SharedReminderMembership.id)
        .join(Reminder, Reminder.id == SharedReminderMembership.reminder_id)
        .where(
            SharedReminderMembership.reminder_id == ReminderDelivery.reminder_id,
            SharedReminderMembership.user_id == ReminderDelivery.recipient_user_id,
            SharedReminderMembership.state == SharedMembershipState.REVOKED.value,
            SharedReminderMembership.revision == ReminderDelivery.membership_revision + 1,
            Reminder.state.in_(_TERMINAL_REMINDER_STATES),
        )
    )
    await session.execute(
        update(ReminderDelivery)
        .where(
            ReminderDelivery.state.in_(
                (
                    ReminderDeliveryState.PENDING.value,
                    ReminderDeliveryState.PROCESSING.value,
                )
            ),
            terminal_membership_exists,
        )
        .values(
            state=ReminderDeliveryState.CANCELLED.value,
            lease_until=None,
            lease_token=None,
        )
    )


async def cleanup_expired_shared_data(
    *,
    now_utc: datetime | None = None,
) -> dict[str, int]:
    current_time = _as_utc(now_utc or utc_now())
    async with SessionLocal() as session, session.begin():
        await _expire_terminal_memberships_set_based(session, now_utc=current_time)

        expired_result = cast(
            CursorResult[Any],
            await session.execute(
                update(SharedReminderInvite)
                .where(
                    SharedReminderInvite.state == SharedInviteState.PENDING.value,
                    SharedReminderInvite.expires_at <= current_time,
                )
                .values(
                    state=SharedInviteState.EXPIRED.value,
                    revision=SharedReminderInvite.revision + 1,
                )
            ),
        )
        invite_cutoff = current_time - SHARED_INVITE_RETENTION
        deleted_invites = cast(
            CursorResult[Any],
            await session.execute(
                delete(SharedReminderInvite).where(
                    SharedReminderInvite.expires_at <= invite_cutoff,
                    SharedReminderInvite.state != SharedInviteState.PENDING.value,
                )
            ),
        )
        membership_cutoff = current_time - SHARED_MEMBERSHIP_RETENTION
        retained_delivery = exists().where(
            ReminderDelivery.reminder_id == SharedReminderMembership.reminder_id,
            ReminderDelivery.recipient_user_id == SharedReminderMembership.user_id,
            ReminderDelivery.membership_revision <= SharedReminderMembership.revision,
        )
        deleted_memberships = cast(
            CursorResult[Any],
            await session.execute(
                delete(SharedReminderMembership).where(
                    SharedReminderMembership.state == SharedMembershipState.REVOKED.value,
                    SharedReminderMembership.revoked_at <= membership_cutoff,
                    ~retained_delivery,
                )
            ),
        )
        delivery_cutoff = current_time - SHARED_DELIVERY_RETENTION
        deleted_deliveries = cast(
            CursorResult[Any],
            await session.execute(
                delete(ReminderDelivery).where(
                    ReminderDelivery.created_at <= delivery_cutoff,
                    ReminderDelivery.occurrence_id.in_(
                        select(ReminderOccurrence.id).where(
                            ReminderOccurrence.status.in_(
                                (
                                    OccurrenceState.COMPLETED.value,
                                    OccurrenceState.CANCELLED.value,
                                    OccurrenceState.FAILED.value,
                                )
                            )
                        )
                    ),
                )
            ),
        )
        return {
            "expired_invites": int(expired_result.rowcount or 0),
            "deleted_invites": int(deleted_invites.rowcount or 0),
            "deleted_memberships": int(deleted_memberships.rowcount or 0),
            "deleted_deliveries": int(deleted_deliveries.rowcount or 0),
        }
