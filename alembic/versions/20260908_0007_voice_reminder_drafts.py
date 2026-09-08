# mypy: ignore-errors
"""add persistent voice reminder drafts

Revision ID: 20260908_0007
Revises: 20260907_0006
Create Date: 2026-09-08 04:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260908_0007"
down_revision = "20260907_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_reminder_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=False),
        sa.Column("reminder_text", sa.Text(), nullable=False),
        sa.Column("remind_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_timezone", sa.String(length=64), nullable=False),
        sa.Column("datetime_semantics", sa.String(length=16), nullable=False),
        sa.Column("recurrence_type", sa.String(length=16), nullable=False),
        sa.Column("recurrence_interval", sa.Integer(), nullable=False),
        sa.Column("recurrence_day_of_month", sa.Integer(), nullable=True),
        sa.Column("recurrence_rule", sa.Text(), nullable=True),
        sa.Column("action_revision", sa.Integer(), nullable=False, server_default="1"),
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
    )
    op.create_index(
        "uq_voice_reminder_drafts_user_chat",
        "voice_reminder_drafts",
        ["user_id", "chat_id"],
        unique=True,
    )
    op.create_index(
        "ix_voice_reminder_drafts_user_id",
        "voice_reminder_drafts",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_voice_reminder_drafts_expires_at",
        "voice_reminder_drafts",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_voice_reminder_drafts_expires_at", table_name="voice_reminder_drafts")
    op.drop_index("ix_voice_reminder_drafts_user_id", table_name="voice_reminder_drafts")
    op.drop_index("uq_voice_reminder_drafts_user_chat", table_name="voice_reminder_drafts")
    op.drop_table("voice_reminder_drafts")
