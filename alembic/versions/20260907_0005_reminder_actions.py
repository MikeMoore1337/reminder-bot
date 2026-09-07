# mypy: ignore-errors
"""add persisted reminder states, occurrences, and action drafts

Revision ID: 20260907_0005
Revises: 20260907_0004
Create Date: 2026-09-07 16:00:00
"""

import sqlalchemy as sa

from alembic import context, op

revision = "20260907_0005"
down_revision = "20260907_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders",
        sa.Column("state", sa.String(length=20), nullable=True, server_default="scheduled"),
    )
    op.add_column(
        "reminders",
        sa.Column("action_revision", sa.Integer(), nullable=True, server_default="0"),
    )
    op.add_column(
        "reminders",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column(
            "parent_reminder_id",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column(
        "reminders",
        sa.Column("source_occurrence_at_utc", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        sa.text(
            """
            UPDATE reminders
            SET state = CASE
                WHEN status = 'failed' THEN 'failed'
                WHEN snoozed_until_utc IS NOT NULL THEN 'snoozed'
                WHEN status = 'sent' THEN 'delivered'
                ELSE 'scheduled'
            END,
                action_revision = 0
            """
        )
    )
    op.alter_column("reminders", "state", nullable=False, server_default=None)
    op.alter_column("reminders", "action_revision", nullable=False, server_default=None)
    op.create_index(
        "ix_reminders_parent_reminder_id",
        "reminders",
        ["parent_reminder_id"],
        unique=False,
    )
    op.create_index(
        "ix_reminders_state_delivery_at_utc",
        "reminders",
        ["state", "delivery_at_utc"],
        unique=False,
    )
    # SQLite cannot ALTER TABLE to add a self-referencing constraint. The
    # application metadata still enforces it for create_all/test databases;
    # production PostgreSQL receives the real foreign key below.
    if context.get_context().dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_reminders_parent_reminder_id",
            "reminders",
            "reminders",
            ["parent_reminder_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "uq_reminders_parent_source_occurrence",
        "reminders",
        ["parent_reminder_id", "source_occurrence_at_utc"],
        unique=True,
    )

    op.create_table(
        "reminder_occurrences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reminder_id", sa.Integer(), nullable=False),
        sa.Column("occurrence_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivery_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("action_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snoozed_until_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "reminder_id",
            "occurrence_at_utc",
            name="uq_reminder_occurrences_reminder_occurrence",
        ),
    )
    op.create_index(
        "ix_reminder_occurrences_reminder_id",
        "reminder_occurrences",
        ["reminder_id"],
        unique=False,
    )
    op.create_index(
        "ix_reminder_occurrences_reminder_status",
        "reminder_occurrences",
        ["reminder_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_reminder_occurrences_status_delivery_at",
        "reminder_occurrences",
        ["status", "delivery_at_utc"],
        unique=False,
    )

    # Existing sent rows have one persisted delivery identity from Issue #9.
    # Recreate it as a delivered occurrence without changing the reminder rows.
    op.execute(
        sa.text(
            """
            INSERT INTO reminder_occurrences (
                reminder_id,
                occurrence_at_utc,
                delivery_at_utc,
                status,
                action_revision,
                message_id,
                delivered_at
            )
            SELECT
                id,
                last_delivery_occurrence_utc,
                last_delivery_occurrence_utc,
                'delivered',
                action_revision,
                last_message_id,
                sent_at
            FROM reminders
            WHERE last_delivery_occurrence_utc IS NOT NULL
            """
        )
    )

    op.create_table(
        "action_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("reminder_id", sa.Integer(), nullable=False),
        sa.Column("expected_action_revision", sa.Integer(), nullable=False),
        sa.Column("expected_occurrence_id", sa.Integer(), nullable=True),
        sa.Column("expected_occurrence_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_message_id", sa.BigInteger(), nullable=True),
        sa.Column("current_step", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["expected_occurrence_id"],
            ["reminder_occurrences.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_action_drafts_user_id",
        "action_drafts",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_action_drafts_reminder_id",
        "action_drafts",
        ["reminder_id"],
        unique=False,
    )
    op.create_index(
        "ix_action_drafts_expected_occurrence_id",
        "action_drafts",
        ["expected_occurrence_id"],
        unique=False,
    )
    op.create_index(
        "ix_action_drafts_owner_chat_type",
        "action_drafts",
        ["user_id", "chat_id", "action_type"],
        unique=False,
    )
    op.create_index(
        "ix_action_drafts_expires_at",
        "action_drafts",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_action_drafts_expires_at", table_name="action_drafts")
    op.drop_index("ix_action_drafts_owner_chat_type", table_name="action_drafts")
    op.drop_index("ix_action_drafts_expected_occurrence_id", table_name="action_drafts")
    op.drop_index("ix_action_drafts_reminder_id", table_name="action_drafts")
    op.drop_index("ix_action_drafts_user_id", table_name="action_drafts")
    op.drop_table("action_drafts")

    op.drop_index(
        "ix_reminder_occurrences_status_delivery_at",
        table_name="reminder_occurrences",
    )
    op.drop_index(
        "ix_reminder_occurrences_reminder_status",
        table_name="reminder_occurrences",
    )
    op.drop_index("ix_reminder_occurrences_reminder_id", table_name="reminder_occurrences")
    op.drop_table("reminder_occurrences")

    op.drop_index(
        "uq_reminders_parent_source_occurrence",
        "reminders",
    )
    if context.get_context().dialect.name != "sqlite":
        op.drop_constraint(
            "fk_reminders_parent_reminder_id",
            "reminders",
            type_="foreignkey",
        )
    op.drop_index("ix_reminders_state_delivery_at_utc", table_name="reminders")
    op.drop_index("ix_reminders_parent_reminder_id", table_name="reminders")
    op.drop_column("reminders", "source_occurrence_at_utc")
    op.drop_column("reminders", "parent_reminder_id")
    op.drop_column("reminders", "paused_at")
    op.drop_column("reminders", "cancelled_at")
    op.drop_column("reminders", "completed_at")
    op.drop_column("reminders", "action_revision")
    op.drop_column("reminders", "state")
