# mypy: ignore-errors
"""preserve calendar schedule and delivery overrides

Revision ID: 20260907_0003
Revises: 20260331_0002
Create Date: 2026-09-07 09:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260907_0003"
down_revision = "20260331_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders",
        sa.Column("schedule_timezone", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("delivery_at_utc", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("snoozed_until_utc", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("recurrence_day_of_month", sa.Integer(), nullable=True),
    )

    op.execute(
        sa.text(
            """
            UPDATE reminders AS r
            SET schedule_timezone = u.timezone
            FROM users AS u
            WHERE r.user_id = u.id
              AND r.schedule_timezone IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE reminders
            SET delivery_at_utc = remind_at_utc
            WHERE delivery_at_utc IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE reminders AS r
            SET recurrence_day_of_month = EXTRACT(
                DAY FROM (r.remind_at_utc AT TIME ZONE u.timezone)
            )::integer
            FROM users AS u
            WHERE r.user_id = u.id
              AND r.recurrence_type = 'monthly'
              AND r.recurrence_day_of_month IS NULL
            """
        )
    )
    op.alter_column("reminders", "schedule_timezone", nullable=False)
    op.create_index(
        "ix_reminders_status_delivery_at_utc",
        "reminders",
        ["status", "delivery_at_utc"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reminders_status_delivery_at_utc", table_name="reminders")
    op.drop_column("reminders", "recurrence_day_of_month")
    op.drop_column("reminders", "snoozed_until_utc")
    op.drop_column("reminders", "delivery_at_utc")
    op.drop_column("reminders", "schedule_timezone")
