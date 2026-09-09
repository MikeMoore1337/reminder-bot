"""Add bounded shared reminders and durable per-recipient delivery state.

Revision ID: 20260908_0014
Revises: 20260908_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260908_0014"
down_revision: str | None = "20260908_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shared_reminder_memberships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reminder_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="participant"),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reminder_id",
            "user_id",
            name="uq_shared_reminder_memberships_reminder_user",
        ),
    )
    op.create_index(
        "ix_shared_reminder_memberships_reminder_state",
        "shared_reminder_memberships",
        ["reminder_id", "state"],
    )
    op.create_index(
        "ix_shared_reminder_memberships_user_state",
        "shared_reminder_memberships",
        ["user_id", "state"],
    )

    op.create_table(
        "shared_reminder_invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reminder_id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["accepted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_shared_reminder_invites_token_hash"),
    )
    op.create_index(
        "ix_shared_reminder_invites_reminder_state",
        "shared_reminder_invites",
        ["reminder_id", "state"],
    )
    op.create_index(
        "ix_shared_reminder_invites_expires_at",
        "shared_reminder_invites",
        ["expires_at"],
    )

    op.create_table(
        "reminder_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reminder_id", sa.Integer(), nullable=False),
        sa.Column("occurrence_id", sa.Integer(), nullable=False),
        sa.Column("recipient_user_id", sa.Integer(), nullable=False),
        sa.Column("membership_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("action_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=128), nullable=True),
        sa.Column("error_kind", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["occurrence_id"], ["reminder_occurrences.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "occurrence_id",
            "recipient_user_id",
            "membership_revision",
            name="uq_reminder_deliveries_occurrence_recipient_generation",
        ),
    )
    op.create_index(
        "ix_reminder_deliveries_reminder_occurrence",
        "reminder_deliveries",
        ["reminder_id", "occurrence_id"],
    )
    op.create_index(
        "ix_reminder_deliveries_state_lease",
        "reminder_deliveries",
        ["state", "lease_until"],
    )
    op.create_index(
        "ix_reminder_deliveries_recipient_state",
        "reminder_deliveries",
        ["recipient_user_id", "state"],
    )


def downgrade() -> None:
    op.drop_index("ix_reminder_deliveries_recipient_state", table_name="reminder_deliveries")
    op.drop_index("ix_reminder_deliveries_state_lease", table_name="reminder_deliveries")
    op.drop_index(
        "ix_reminder_deliveries_reminder_occurrence",
        table_name="reminder_deliveries",
    )
    op.drop_table("reminder_deliveries")

    op.drop_index("ix_shared_reminder_invites_expires_at", table_name="shared_reminder_invites")
    op.drop_index(
        "ix_shared_reminder_invites_reminder_state",
        table_name="shared_reminder_invites",
    )
    op.drop_table("shared_reminder_invites")

    op.drop_index(
        "ix_shared_reminder_memberships_user_state",
        table_name="shared_reminder_memberships",
    )
    op.drop_index(
        "ix_shared_reminder_memberships_reminder_state",
        table_name="shared_reminder_memberships",
    )
    op.drop_table("shared_reminder_memberships")
