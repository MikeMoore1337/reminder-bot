from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class RecurrenceType(StrEnum):
    NONE = "none"
    MINUTES = "minutes"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ADVANCED = "advanced"


class ReminderState(StrEnum):
    """User-facing state; ``Reminder.status`` remains the worker lifecycle."""

    SCHEDULED = "scheduled"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    SNOOZED = "snoozed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"


class OccurrenceState(StrEnum):
    PROCESSING = "processing"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    SNOOZED = "snoozed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ReminderMode(StrEnum):
    NORMAL = "normal"
    PERSISTENT = "persistent"
    # ``important`` is the product language for the same bounded persistent
    # mode.  The database stores the canonical ``persistent`` value.
    IMPORTANT = "persistent"


class ReminderKind(StrEnum):
    ORDINARY = "ordinary"
    DEADLINE = "deadline"


class DeadlinePlanState(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    DISABLED = "disabled"
    CANCELLED = "cancelled"
    EXHAUSTED = "exhausted"


class DeadlineStepState(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    SKIPPED = "skipped"
    FAILED = "failed"


class SuggestionState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DISMISSED = "dismissed"
    EXPIRED = "expired"


class DigestPeriod(StrEnum):
    MORNING = "morning"
    EVENING = "evening"


class DigestDeliveryState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    SUPPRESSED = "suppressed"
    FAILED = "failed"


class ConditionSubscriptionState(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ConditionDeliveryState(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index(
            "ix_users_digests_enabled_schedule_seeded", "digests_enabled", "digest_schedule_seeded"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Moscow")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    next_persistent_delivery_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    suggestions_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    digests_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    digest_schedule_seeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    digest_morning_time: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00")
    digest_evening_time: Mapped[str] = mapped_column(String(5), nullable=False, default="20:00")
    digest_quiet_hours_start: Mapped[str] = mapped_column(
        String(5), nullable=False, default="22:00"
    )
    digest_quiet_hours_end: Mapped[str] = mapped_column(String(5), nullable=False, default="08:00")

    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    action_drafts: Mapped[list[ActionDraft]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    reminder_clarifications: Mapped[list[ReminderClarification]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    voice_reminder_drafts: Mapped[list[VoiceReminderDraft]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    deadline_reminder_drafts: Mapped[list[DeadlineReminderDraft]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    snooze_events: Mapped[list[ReminderSnoozeEvent]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    suggestions: Mapped[list[ReminderSuggestion]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    digest_deliveries: Mapped[list[ReminderDigestDelivery]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    condition_subscriptions: Mapped[list[ConditionSubscription]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        Index("ix_reminders_status_remind_at_utc", "status", "remind_at_utc"),
        Index("ix_reminders_status_delivery_at_utc", "status", "delivery_at_utc"),
        Index("ix_reminders_status_next_retry_at", "status", "next_retry_at"),
        Index("ix_reminders_status_lease_until", "status", "lease_until"),
        Index(
            "ix_reminders_status_processing_started_at",
            "status",
            "processing_started_at",
        ),
        Index("ix_reminders_state_delivery_at_utc", "state", "delivery_at_utc"),
        Index(
            "uq_reminders_parent_source_occurrence",
            "parent_reminder_id",
            "source_occurrence_at_utc",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    remind_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    schedule_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snoozed_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), index=True, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recurrence_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RecurrenceType.NONE.value
    )
    recurrence_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    recurrence_day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recurrence_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_delivery_occurrence_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ReminderState.SCHEDULED.value
    )
    action_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parent_reminder_id: Mapped[int | None] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), index=True, nullable=True
    )
    source_occurrence_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    context_kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ReminderKind.ORDINARY.value
    )
    deadline_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_plan_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    deadline_plan_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_current_step_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deadline_current_step_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deadline_current_step_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deadline_total_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_overdue_after_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=ReminderMode.NORMAL.value)
    persistent_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    persistent_max_deliveries: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    persistent_max_escalations: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    persistent_quiet_hours_start: Mapped[str] = mapped_column(
        String(5), nullable=False, default="22:00"
    )
    persistent_quiet_hours_end: Mapped[str] = mapped_column(
        String(5), nullable=False, default="08:00"
    )
    persistent_delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persistent_escalation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persistent_deferred_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persistent_exhausted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    persistent_disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    persistent_stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    user: Mapped[User] = relationship(back_populates="reminders")
    parent_reminder: Mapped[Reminder | None] = relationship(
        "Reminder",
        remote_side="Reminder.id",
        back_populates="snooze_children",
        foreign_keys=[parent_reminder_id],
    )
    snooze_children: Mapped[list[Reminder]] = relationship(
        "Reminder",
        back_populates="parent_reminder",
        foreign_keys=[parent_reminder_id],
    )
    occurrences: Mapped[list[ReminderOccurrence]] = relationship(
        back_populates="reminder", cascade="all, delete-orphan"
    )
    context: Mapped[ReminderContext | None] = relationship(
        "ReminderContext",
        back_populates="reminder",
        uselist=False,
        cascade="all, delete-orphan",
    )
    deadline_plan: Mapped[ReminderDeadlinePlan | None] = relationship(
        "ReminderDeadlinePlan",
        back_populates="reminder",
        uselist=False,
        cascade="all, delete-orphan",
    )
    snooze_events: Mapped[list[ReminderSnoozeEvent]] = relationship(
        back_populates="reminder", cascade="all, delete-orphan"
    )
    suggestions: Mapped[list[ReminderSuggestion]] = relationship(
        back_populates="reminder", cascade="all, delete-orphan"
    )


class ReminderOccurrence(Base):
    """Persisted identity for one canonical occurrence and its deliveries."""

    __tablename__ = "reminder_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "reminder_id",
            "occurrence_at_utc",
            name="uq_reminder_occurrences_reminder_occurrence",
        ),
        Index("ix_reminder_occurrences_reminder_status", "reminder_id", "status"),
        Index("ix_reminder_occurrences_status_delivery_at", "status", "delivery_at_utc"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reminder_id: Mapped[int] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    occurrence_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivery_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OccurrenceState.PROCESSING.value
    )
    action_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snoozed_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deadline_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("reminder_deadline_steps.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    reminder: Mapped[Reminder] = relationship(back_populates="occurrences")


class ReminderDeadlinePlan(Base):
    """Persisted, bounded escalation plan attached to one deadline reminder."""

    __tablename__ = "reminder_deadline_plans"
    __table_args__ = (
        UniqueConstraint("reminder_id", name="uq_reminder_deadline_plans_reminder_id"),
        Index("ix_reminder_deadline_plans_user_chat_state", "user_id", "chat_id", "state"),
        Index("ix_reminder_deadline_plans_deadline_at_utc", "deadline_at_utc"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reminder_id: Mapped[int] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deadline_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DeadlinePlanState.ACTIVE.value
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_step_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overdue_after_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    reminder: Mapped[Reminder] = relationship(back_populates="deadline_plan")
    steps: Mapped[list[ReminderDeadlineStep]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class ReminderDeadlineStep(Base):
    """One idempotently persisted delivery point in a deadline plan revision."""

    __tablename__ = "reminder_deadline_steps"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "revision",
            "sequence",
            name="uq_reminder_deadline_steps_plan_revision_sequence",
        ),
        Index(
            "ix_reminder_deadline_steps_plan_revision_state_at",
            "plan_id",
            "revision",
            "state",
            "scheduled_at_utc",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("reminder_deadline_plans.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    scheduled_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DeadlineStepState.PENDING.value
    )
    skip_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    plan: Mapped[ReminderDeadlinePlan] = relationship(back_populates="steps")


class ReminderContext(Base):
    """Bounded source snapshot/reference attached to one reminder."""

    __tablename__ = "reminder_contexts"
    __table_args__ = (
        UniqueConstraint("reminder_id", name="uq_reminder_contexts_reminder_id"),
        Index("ix_reminder_contexts_user_chat", "user_id", "chat_id"),
        Index("ix_reminder_contexts_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reminder_id: Mapped[int] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_chat_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_sender_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    media_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_file_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    media_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_date_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    reminder: Mapped[Reminder] = relationship(back_populates="context")


class ActionDraft(Base):
    """Small restart-safe state holder for Edit and Custom Snooze flows."""

    __tablename__ = "action_drafts"
    __table_args__ = (
        Index("uq_action_drafts_user_chat", "user_id", "chat_id", unique=True),
        Index("ix_action_drafts_owner_chat_type", "user_id", "chat_id", "action_type"),
        Index("ix_action_drafts_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reminder_id: Mapped[int] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    expected_action_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_occurrence_id: Mapped[int | None] = mapped_column(
        ForeignKey("reminder_occurrences.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    expected_occurrence_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expected_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    current_step: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship(back_populates="action_drafts")
    reminder: Mapped[Reminder] = relationship()


class VoiceReminderDraft(Base):
    """Restart-safe, confirmation-gated draft created from a voice message."""

    __tablename__ = "voice_reminder_drafts"
    __table_args__ = (
        Index("uq_voice_reminder_drafts_user_chat", "user_id", "chat_id", unique=True),
        Index("ix_voice_reminder_drafts_user_id", "user_id"),
        Index("ix_voice_reminder_drafts_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    transcript: Mapped[str] = mapped_column(Text, nullable=False)
    reminder_text: Mapped[str] = mapped_column(Text, nullable=False)
    remind_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    datetime_semantics: Mapped[str] = mapped_column(String(16), nullable=False)
    recurrence_type: Mapped[str] = mapped_column(String(16), nullable=False)
    recurrence_interval: Mapped[int] = mapped_column(Integer, nullable=False)
    recurrence_day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recurrence_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=ReminderMode.NORMAL.value)
    action_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    preview_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship(back_populates="voice_reminder_drafts")


class ReminderClarification(Base):
    """Bounded, restart-safe state for an ambiguous reminder input."""

    __tablename__ = "reminder_clarifications"
    __table_args__ = (
        Index("uq_reminder_clarifications_user_chat", "user_id", "chat_id", unique=True),
        Index("ix_reminder_clarifications_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    voice_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    context_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=ReminderMode.NORMAL.value)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    clarification_type: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship(back_populates="reminder_clarifications")


class DeadlineReminderDraft(Base):
    """Restart-safe confirmation draft for a deadline reminder and its plan."""

    __tablename__ = "deadline_reminder_drafts"
    __table_args__ = (
        Index("uq_deadline_reminder_drafts_user_chat", "user_id", "chat_id", unique=True),
        Index("ix_deadline_reminder_drafts_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    reminder_text: Mapped[str] = mapped_column(Text, nullable=False)
    deadline_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    point_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    overdue_after_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    context_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    preview_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship(back_populates="deadline_reminder_drafts")


class ReminderSnoozeEvent(Base):
    """Minimal, bounded history used for transparent snooze suggestions."""

    __tablename__ = "reminder_snooze_events"
    __table_args__ = (
        Index(
            "ix_reminder_snooze_events_reminder_snoozed_at",
            "reminder_id",
            "snoozed_at_utc",
        ),
        Index("ix_reminder_snooze_events_user_snoozed_at", "user_id", "snoozed_at_utc"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reminder_id: Mapped[int] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False
    )
    occurrence_id: Mapped[int | None] = mapped_column(
        ForeignKey("reminder_occurrences.id", ondelete="SET NULL"), nullable=True
    )
    occurrence_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snoozed_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_local_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    schedule_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="snooze_events")
    reminder: Mapped[Reminder] = relationship(back_populates="snooze_events")


class ReminderSuggestion(Base):
    """Explicit, user-scoped schedule suggestion awaiting a decision."""

    __tablename__ = "reminder_suggestions"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_reminder_suggestions_dedupe_key"),
        Index("ix_reminder_suggestions_user_chat_status", "user_id", "chat_id", "status"),
        Index("ix_reminder_suggestions_reminder_status", "reminder_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reminder_id: Mapped[int] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SuggestionState.PENDING.value
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expected_reminder_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    current_local_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_local_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_window_start_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    evidence_window_end_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="suggestions")
    reminder: Mapped[Reminder] = relationship(back_populates="suggestions")


class ReminderDigestDelivery(Base):
    """One idempotent morning/evening digest slot for one user and local date."""

    __tablename__ = "reminder_digest_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "period",
            "local_date",
            name="uq_reminder_digest_deliveries_user_period_date",
        ),
        Index("ix_reminder_digest_deliveries_state_scheduled_at", "state", "scheduled_at_utc"),
        Index("ix_reminder_digest_deliveries_user_date", "user_id", "local_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period: Mapped[str] = mapped_column(String(8), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    scheduled_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DigestDeliveryState.PENDING.value
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suppression_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_text: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="digest_deliveries")


class ConditionSubscription(Base):
    """Restart-safe definition of one external condition poll."""

    __tablename__ = "condition_subscriptions"
    __table_args__ = (
        Index(
            "ix_condition_subscriptions_state_next_poll_lease",
            "state",
            "next_poll_at_utc",
            "lease_until_utc",
        ),
        Index("ix_condition_subscriptions_user_state", "user_id", "state"),
        Index("ix_condition_subscriptions_provider_target_hash", "provider_type", "target_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    message_template: Mapped[str] = mapped_column(Text, nullable=False)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    trigger_on_initial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ConditionSubscriptionState.ACTIVE.value
    )
    next_poll_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_observed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transition_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="condition_subscriptions")
    observations: Mapped[list[ConditionObservation]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )
    transitions: Mapped[list[ConditionTransition]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )
    deliveries: Mapped[list[ConditionDelivery]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )


class ConditionObservation(Base):
    """Bounded success/failure history; provider payloads are never persisted."""

    __tablename__ = "condition_observations"
    __table_args__ = (
        Index(
            "ix_condition_observations_subscription_observed_at",
            "subscription_id",
            "observed_at_utc",
        ),
        Index("ix_condition_observations_success_observed_at", "success", "observed_at_utc"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("condition_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    observed_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    subscription: Mapped[ConditionSubscription] = relationship(back_populates="observations")


class ConditionTransition(Base):
    """One serialized state transition for a subscription."""

    __tablename__ = "condition_transitions"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "sequence",
            name="uq_condition_transitions_subscription_sequence",
        ),
        Index(
            "ix_condition_transitions_subscription_observed_at",
            "subscription_id",
            "observed_at_utc",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("condition_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_state: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    subscription: Mapped[ConditionSubscription] = relationship(back_populates="transitions")
    deliveries: Mapped[list[ConditionDelivery]] = relationship(
        back_populates="transition", cascade="all, delete-orphan"
    )


class ConditionDelivery(Base):
    """Durable, idempotent outbox item emitted once per transition."""

    __tablename__ = "condition_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "transition_sequence",
            name="uq_condition_deliveries_subscription_transition",
        ),
        Index("ix_condition_deliveries_state_created_at", "state", "created_at"),
        Index("ix_condition_deliveries_subscription_state", "subscription_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("condition_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    transition_id: Mapped[int] = mapped_column(
        ForeignKey("condition_transitions.id", ondelete="CASCADE"), nullable=False
    )
    transition_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_state: Mapped[str] = mapped_column(String(64), nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ConditionDeliveryState.PENDING.value
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    subscription: Mapped[ConditionSubscription] = relationship(back_populates="deliveries")
    transition: Mapped[ConditionTransition] = relationship(back_populates="deliveries")

    @property
    def message(self) -> str:
        """Compatibility-friendly name for outbox consumers."""

        return self.message_text


# Short aliases keep the persistence vocabulary convenient for service and test callers.
SnoozeEvent = ReminderSnoozeEvent
DigestDelivery = ReminderDigestDelivery
