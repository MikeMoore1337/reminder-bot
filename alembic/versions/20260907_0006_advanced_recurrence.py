# mypy: ignore-errors
"""add canonical recurrence rules and persisted parser clarifications

Revision ID: 20260907_0006
Revises: 20260907_0005
Create Date: 2026-09-07 23:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260907_0006"
down_revision = "20260907_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders",
        sa.Column("recurrence_rule", sa.Text(), nullable=True),
    )

    # Keep every pre-0006 row reproducible while retaining the old scalar
    # columns for compatibility with older workers and rollback inspection.
    # Both PostgreSQL and SQLite support || string concatenation and CAST.
    op.execute(
        sa.text(
            """
            UPDATE reminders
            SET recurrence_rule =
                :rule_prefix
                || recurrence_type
                || :interval_prefix
                || CAST(recurrence_interval AS TEXT)
                || :day_prefix
                || COALESCE(CAST(recurrence_day_of_month AS TEXT), 'null')
                || :rule_suffix
            WHERE recurrence_rule IS NULL
            """
        ).bindparams(
            rule_prefix='{"version":1,"kind":"legacy","recurrence_type":"',
            interval_prefix='","interval":',
            day_prefix=',"day_of_month":',
            rule_suffix="}",
        )
    )

    op.create_table(
        "reminder_clarifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("clarification_type", sa.String(length=32), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
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
    )
    op.create_index(
        "ix_reminder_clarifications_user_id",
        "reminder_clarifications",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "uq_reminder_clarifications_user_chat",
        "reminder_clarifications",
        ["user_id", "chat_id"],
        unique=True,
    )
    op.create_index(
        "ix_reminder_clarifications_expires_at",
        "reminder_clarifications",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reminder_clarifications_expires_at", table_name="reminder_clarifications")
    op.drop_index("uq_reminder_clarifications_user_chat", table_name="reminder_clarifications")
    op.drop_index("ix_reminder_clarifications_user_id", table_name="reminder_clarifications")
    op.drop_table("reminder_clarifications")
    op.drop_column("reminders", "recurrence_rule")
