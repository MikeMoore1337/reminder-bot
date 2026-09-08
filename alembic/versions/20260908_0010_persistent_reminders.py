# mypy: ignore-errors
"""add bounded persistent reminder policy and delivery state

Revision ID: 20260908_0010
Revises: 20260908_0009
Create Date: 2026-09-08 09:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260908_0010"
down_revision = "20260908_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("next_persistent_delivery_at_utc", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "reminders",
        sa.Column("mode", sa.String(length=16), nullable=True, server_default="normal"),
    )
    op.add_column(
        "reminders",
        sa.Column(
            "persistent_interval_minutes",
            sa.Integer(),
            nullable=True,
            server_default="60",
        ),
    )
    op.add_column(
        "reminders",
        sa.Column("persistent_max_deliveries", sa.Integer(), nullable=True, server_default="6"),
    )
    op.add_column(
        "reminders",
        sa.Column("persistent_max_escalations", sa.Integer(), nullable=True, server_default="5"),
    )
    op.add_column(
        "reminders",
        sa.Column(
            "persistent_quiet_hours_start",
            sa.String(length=5),
            nullable=True,
            server_default="22:00",
        ),
    )
    op.add_column(
        "reminders",
        sa.Column(
            "persistent_quiet_hours_end",
            sa.String(length=5),
            nullable=True,
            server_default="08:00",
        ),
    )
    op.add_column(
        "reminders",
        sa.Column("persistent_delivery_count", sa.Integer(), nullable=True, server_default="0"),
    )
    op.add_column(
        "reminders",
        sa.Column("persistent_escalation_count", sa.Integer(), nullable=True, server_default="0"),
    )
    op.add_column(
        "reminders",
        sa.Column("persistent_deferred_count", sa.Integer(), nullable=True, server_default="0"),
    )
    op.add_column(
        "reminders",
        sa.Column("persistent_exhausted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("persistent_disabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("persistent_stop_reason", sa.String(length=32), nullable=True),
    )

    # Existing rows remain ordinary one-shot/recurring reminders.  The
    # defaults are removed after the deterministic backfill so future writes
    # still come through the application policy.
    op.execute(
        sa.text(
            """
            UPDATE reminders
            SET mode = 'normal',
                persistent_interval_minutes = 60,
                persistent_max_deliveries = 6,
                persistent_max_escalations = 5,
                persistent_quiet_hours_start = '22:00',
                persistent_quiet_hours_end = '08:00',
                persistent_delivery_count = 0,
                persistent_escalation_count = 0,
                persistent_deferred_count = 0
            """
        )
    )
    op.alter_column("reminders", "mode", nullable=False, server_default=None)
    op.alter_column("reminders", "persistent_interval_minutes", nullable=False, server_default=None)
    op.alter_column("reminders", "persistent_max_deliveries", nullable=False, server_default=None)
    op.alter_column("reminders", "persistent_max_escalations", nullable=False, server_default=None)
    op.alter_column(
        "reminders", "persistent_quiet_hours_start", nullable=False, server_default=None
    )
    op.alter_column("reminders", "persistent_quiet_hours_end", nullable=False, server_default=None)
    op.alter_column("reminders", "persistent_delivery_count", nullable=False, server_default=None)
    op.alter_column("reminders", "persistent_escalation_count", nullable=False, server_default=None)
    op.alter_column("reminders", "persistent_deferred_count", nullable=False, server_default=None)

    op.add_column(
        "voice_reminder_drafts",
        sa.Column("mode", sa.String(length=16), nullable=True, server_default="normal"),
    )
    op.execute(sa.text("UPDATE voice_reminder_drafts SET mode = 'normal' WHERE mode IS NULL"))
    op.alter_column("voice_reminder_drafts", "mode", nullable=False, server_default=None)


def downgrade() -> None:
    op.drop_column("voice_reminder_drafts", "mode")
    op.drop_column("reminders", "persistent_stop_reason")
    op.drop_column("reminders", "persistent_disabled_at")
    op.drop_column("reminders", "persistent_exhausted_at")
    op.drop_column("reminders", "persistent_deferred_count")
    op.drop_column("reminders", "persistent_escalation_count")
    op.drop_column("reminders", "persistent_delivery_count")
    op.drop_column("reminders", "persistent_quiet_hours_end")
    op.drop_column("reminders", "persistent_quiet_hours_start")
    op.drop_column("reminders", "persistent_max_escalations")
    op.drop_column("reminders", "persistent_max_deliveries")
    op.drop_column("reminders", "persistent_interval_minutes")
    op.drop_column("reminders", "mode")
    op.drop_column("users", "next_persistent_delivery_at_utc")
