# mypy: ignore-errors
"""add persisted bounded deadline plans and confirmation drafts

Revision ID: 20260908_0011
Revises: 20260908_0010
Create Date: 2026-09-08 10:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260908_0011"
down_revision = "20260908_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders",
        sa.Column("kind", sa.String(length=16), nullable=True, server_default="ordinary"),
    )
    op.add_column(
        "reminders",
        sa.Column("deadline_at_utc", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("deadline_plan_state", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("deadline_plan_revision", sa.Integer(), nullable=True, server_default="0"),
    )
    op.add_column(
        "reminders",
        sa.Column("deadline_current_step_sequence", sa.Integer(), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("deadline_current_step_code", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("deadline_current_step_label", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("deadline_total_steps", sa.Integer(), nullable=True, server_default="0"),
    )
    op.add_column(
        "reminders",
        sa.Column("deadline_overdue_after_minutes", sa.Integer(), nullable=True),
    )
    op.execute(sa.text("UPDATE reminders SET kind = 'ordinary' WHERE kind IS NULL"))
    op.execute(
        sa.text(
            "UPDATE reminders SET deadline_plan_revision = 0 WHERE deadline_plan_revision IS NULL"
        )
    )
    op.execute(
        sa.text("UPDATE reminders SET deadline_total_steps = 0 WHERE deadline_total_steps IS NULL")
    )
    op.alter_column("reminders", "kind", nullable=False, server_default=None)
    op.alter_column("reminders", "deadline_plan_revision", nullable=False, server_default=None)
    op.alter_column("reminders", "deadline_total_steps", nullable=False, server_default=None)

    op.add_column(
        "reminder_occurrences",
        sa.Column("deadline_step_id", sa.Integer(), nullable=True),
    )

    op.create_table(
        "reminder_deadline_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reminder_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("deadline_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_timezone", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("current_step_sequence", sa.Integer(), nullable=True),
        sa.Column("total_steps", sa.Integer(), nullable=False),
        sa.Column("overdue_after_minutes", sa.Integer(), nullable=True),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("reminder_id", name="uq_reminder_deadline_plans_reminder_id"),
    )
    op.create_index(
        "ix_reminder_deadline_plans_user_chat_state",
        "reminder_deadline_plans",
        ["user_id", "chat_id", "state"],
        unique=False,
    )
    op.create_index(
        "ix_reminder_deadline_plans_deadline_at_utc",
        "reminder_deadline_plans",
        ["deadline_at_utc"],
        unique=False,
    )

    op.create_table(
        "reminder_deadline_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("scheduled_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("skip_reason", sa.String(length=32), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["reminder_deadline_plans.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "plan_id",
            "revision",
            "sequence",
            name="uq_reminder_deadline_steps_plan_revision_sequence",
        ),
    )
    op.create_index(
        "ix_reminder_deadline_steps_plan_revision_state_at",
        "reminder_deadline_steps",
        ["plan_id", "revision", "state", "scheduled_at_utc"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_reminder_occurrences_deadline_step_id",
        "reminder_occurrences",
        "reminder_deadline_steps",
        ["deadline_step_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_reminder_occurrences_deadline_step_id",
        "reminder_occurrences",
        ["deadline_step_id"],
        unique=False,
    )

    op.create_table(
        "deadline_reminder_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("reminder_text", sa.Text(), nullable=False),
        sa.Column("deadline_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_timezone", sa.String(length=64), nullable=False),
        sa.Column("point_codes_json", sa.Text(), nullable=False),
        sa.Column("overdue_after_minutes", sa.Integer(), nullable=True),
        sa.Column("plan_json", sa.Text(), nullable=False),
        sa.Column("context_snapshot", sa.Text(), nullable=True),
        sa.Column("action_revision", sa.Integer(), nullable=False),
        sa.Column("preview_message_id", sa.BigInteger(), nullable=True),
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
        sa.UniqueConstraint("user_id", "chat_id", name="uq_deadline_reminder_drafts_user_chat"),
    )
    op.create_index(
        "ix_deadline_reminder_drafts_expires_at",
        "deadline_reminder_drafts",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_deadline_reminder_drafts_expires_at", table_name="deadline_reminder_drafts")
    op.drop_table("deadline_reminder_drafts")
    op.drop_index(
        "ix_reminder_occurrences_deadline_step_id",
        table_name="reminder_occurrences",
    )
    op.drop_constraint(
        "fk_reminder_occurrences_deadline_step_id",
        "reminder_occurrences",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_reminder_deadline_steps_plan_revision_state_at",
        table_name="reminder_deadline_steps",
    )
    op.drop_table("reminder_deadline_steps")
    op.drop_index(
        "ix_reminder_deadline_plans_deadline_at_utc",
        table_name="reminder_deadline_plans",
    )
    op.drop_index(
        "ix_reminder_deadline_plans_user_chat_state",
        table_name="reminder_deadline_plans",
    )
    op.drop_table("reminder_deadline_plans")
    op.drop_column("reminder_occurrences", "deadline_step_id")
    op.drop_column("reminders", "deadline_overdue_after_minutes")
    op.drop_column("reminders", "deadline_total_steps")
    op.drop_column("reminders", "deadline_current_step_label")
    op.drop_column("reminders", "deadline_current_step_code")
    op.drop_column("reminders", "deadline_current_step_sequence")
    op.drop_column("reminders", "deadline_plan_revision")
    op.drop_column("reminders", "deadline_plan_state")
    op.drop_column("reminders", "deadline_at_utc")
    op.drop_column("reminders", "kind")
