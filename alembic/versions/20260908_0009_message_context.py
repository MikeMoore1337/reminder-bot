# mypy: ignore-errors
"""persist bounded Telegram message context for reminders

Revision ID: 20260908_0009
Revises: 20260908_0008
Create Date: 2026-09-08 07:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260908_0009"
down_revision = "20260908_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders",
        sa.Column("context_kind", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "reminder_clarifications",
        sa.Column("context_snapshot", sa.Text(), nullable=True),
    )
    op.create_table(
        "reminder_contexts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reminder_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("source_chat_username", sa.String(length=64), nullable=True),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("source_thread_id", sa.BigInteger(), nullable=True),
        sa.Column("source_sender_label", sa.String(length=128), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.Column("source_caption", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("media_kind", sa.String(length=32), nullable=True),
        sa.Column("media_file_id", sa.String(length=256), nullable=True),
        sa.Column("media_file_name", sa.String(length=256), nullable=True),
        sa.Column("media_mime_type", sa.String(length=128), nullable=True),
        sa.Column("media_size", sa.BigInteger(), nullable=True),
        sa.Column("source_date_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reminder_id", name="uq_reminder_contexts_reminder_id"),
    )
    op.create_index(
        "ix_reminder_contexts_user_chat",
        "reminder_contexts",
        ["user_id", "chat_id"],
    )
    op.create_index(
        "ix_reminder_contexts_expires_at",
        "reminder_contexts",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_reminder_contexts_expires_at", table_name="reminder_contexts")
    op.drop_index("ix_reminder_contexts_user_chat", table_name="reminder_contexts")
    op.drop_table("reminder_contexts")
    op.drop_column("reminder_clarifications", "context_snapshot")
    op.drop_column("reminders", "context_kind")
