# mypy: ignore-errors
"""persist clarification origin for voice confirmation gating

Revision ID: 20260908_0008
Revises: 20260908_0007
Create Date: 2026-09-08 06:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260908_0008"
down_revision = "20260908_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminder_clarifications",
        sa.Column("origin", sa.String(length=16), server_default="text", nullable=False),
    )
    op.alter_column("reminder_clarifications", "origin", server_default=None)
    op.add_column(
        "reminder_clarifications",
        sa.Column("voice_transcript", sa.Text(), nullable=True),
    )
    op.add_column(
        "reminder_clarifications",
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("reminder_clarifications", "source_message_id")
    op.drop_column("reminder_clarifications", "voice_transcript")
    op.drop_column("reminder_clarifications", "origin")
