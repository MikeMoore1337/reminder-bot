from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
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


class User(Base):
    __tablename__ = "users"

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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    reminder: Mapped[Reminder] = relationship(back_populates="occurrences")


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
