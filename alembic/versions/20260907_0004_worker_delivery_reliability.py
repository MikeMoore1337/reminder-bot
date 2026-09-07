# mypy: ignore-errors
"""add worker delivery leases and bounded retries

Revision ID: 20260907_0004
Revises: 20260907_0003
Create Date: 2026-09-07 12:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260907_0004"
down_revision = "20260907_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "reminders",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("lease_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("last_delivery_occurrence_utc", sa.DateTime(timezone=True), nullable=True),
    )

    # The pre-migration worker had no ownership token or lease. Resetting those
    # rows to pending is the deterministic deployment policy; the old worker
    # must be stopped while migrations run, so no pre-lease owner can finalize.
    op.execute(
        sa.text(
            """
            UPDATE reminders
            SET status = 'pending',
                processing_started_at = NULL,
                lease_until = NULL,
                lease_token = NULL,
                next_retry_at = NULL
            WHERE status = 'processing'
            """
        )
    )
    op.alter_column("reminders", "attempt_count", server_default=None)

    op.create_index(
        "ix_reminders_status_next_retry_at",
        "reminders",
        ["status", "next_retry_at"],
        unique=False,
    )
    op.create_index(
        "ix_reminders_status_lease_until",
        "reminders",
        ["status", "lease_until"],
        unique=False,
    )
    op.create_index(
        "ix_reminders_status_processing_started_at",
        "reminders",
        ["status", "processing_started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reminders_status_processing_started_at",
        table_name="reminders",
    )
    op.drop_index("ix_reminders_status_lease_until", table_name="reminders")
    op.drop_index("ix_reminders_status_next_retry_at", table_name="reminders")
    op.drop_column("reminders", "last_delivery_occurrence_utc")
    op.drop_column("reminders", "next_retry_at")
    op.drop_column("reminders", "lease_token")
    op.drop_column("reminders", "lease_until")
    op.drop_column("reminders", "processing_started_at")
    op.drop_column("reminders", "attempt_count")
