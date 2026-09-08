# mypy: ignore-errors
"""add opt-in adaptive suggestions and idempotent reminder digests

Revision ID: 20260908_0012
Revises: 20260908_0011
Create Date: 2026-09-08 12:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260908_0012"
down_revision = "20260908_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("suggestions_enabled", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("digests_enabled", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column(
            "digest_morning_time",
            sa.String(length=5),
            nullable=True,
            server_default="09:00",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "digest_evening_time",
            sa.String(length=5),
            nullable=True,
            server_default="20:00",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "digest_quiet_hours_start",
            sa.String(length=5),
            nullable=True,
            server_default="22:00",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "digest_quiet_hours_end",
            sa.String(length=5),
            nullable=True,
            server_default="08:00",
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE users
            SET suggestions_enabled = false,
                digests_enabled = false,
                digest_morning_time = '09:00',
                digest_evening_time = '20:00',
                digest_quiet_hours_start = '22:00',
                digest_quiet_hours_end = '08:00'
            """
        )
    )
    op.alter_column("users", "suggestions_enabled", nullable=False, server_default=None)
    op.alter_column("users", "digests_enabled", nullable=False, server_default=None)
    op.alter_column("users", "digest_morning_time", nullable=False, server_default=None)
    op.alter_column("users", "digest_evening_time", nullable=False, server_default=None)
    op.alter_column("users", "digest_quiet_hours_start", nullable=False, server_default=None)
    op.alter_column("users", "digest_quiet_hours_end", nullable=False, server_default=None)

    op.create_table(
        "reminder_snooze_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("reminder_id", sa.Integer(), nullable=False),
        sa.Column("occurrence_id", sa.Integer(), nullable=True),
        sa.Column("occurrence_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snoozed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_local_minutes", sa.Integer(), nullable=False),
        sa.Column("schedule_timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["occurrence_id"],
            ["reminder_occurrences.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reminder_snooze_events_reminder_snoozed_at",
        "reminder_snooze_events",
        ["reminder_id", "snoozed_at_utc"],
    )
    op.create_index(
        "ix_reminder_snooze_events_user_snoozed_at",
        "reminder_snooze_events",
        ["user_id", "snoozed_at_utc"],
    )

    op.create_table(
        "reminder_suggestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("reminder_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("expected_reminder_revision", sa.Integer(), nullable=False),
        sa.Column("current_local_minutes", sa.Integer(), nullable=False),
        sa.Column("proposed_local_minutes", sa.Integer(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("evidence_window_start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_window_end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("resolution", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_reminder_suggestions_dedupe_key"),
    )
    op.create_index(
        "ix_reminder_suggestions_user_chat_status",
        "reminder_suggestions",
        ["user_id", "chat_id", "status"],
    )
    op.create_index(
        "ix_reminder_suggestions_reminder_status",
        "reminder_suggestions",
        ["reminder_id", "status"],
    )

    op.create_table(
        "reminder_digest_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=8), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("scheduled_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suppressed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suppression_reason", sa.String(length=32), nullable=True),
        sa.Column("error_text", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "period",
            "local_date",
            name="uq_reminder_digest_deliveries_user_period_date",
        ),
    )
    op.create_index(
        "ix_reminder_digest_deliveries_state_scheduled_at",
        "reminder_digest_deliveries",
        ["state", "scheduled_at_utc"],
    )
    op.create_index(
        "ix_reminder_digest_deliveries_user_date",
        "reminder_digest_deliveries",
        ["user_id", "local_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reminder_digest_deliveries_user_date",
        table_name="reminder_digest_deliveries",
    )
    op.drop_index(
        "ix_reminder_digest_deliveries_state_scheduled_at",
        table_name="reminder_digest_deliveries",
    )
    op.drop_table("reminder_digest_deliveries")

    op.drop_index("ix_reminder_suggestions_reminder_status", table_name="reminder_suggestions")
    op.drop_index("ix_reminder_suggestions_user_chat_status", table_name="reminder_suggestions")
    op.drop_table("reminder_suggestions")

    op.drop_index(
        "ix_reminder_snooze_events_user_snoozed_at",
        table_name="reminder_snooze_events",
    )
    op.drop_index(
        "ix_reminder_snooze_events_reminder_snoozed_at",
        table_name="reminder_snooze_events",
    )
    op.drop_table("reminder_snooze_events")

    op.drop_column("users", "digest_quiet_hours_end")
    op.drop_column("users", "digest_quiet_hours_start")
    op.drop_column("users", "digest_evening_time")
    op.drop_column("users", "digest_morning_time")
    op.drop_column("users", "digests_enabled")
    op.drop_column("users", "suggestions_enabled")
