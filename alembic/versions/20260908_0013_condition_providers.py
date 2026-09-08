# mypy: ignore-errors
"""add bounded condition provider state and transition outbox

Revision ID: 20260908_0013
Revises: 20260908_0012
Create Date: 2026-09-08 13:30:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260908_0013"
down_revision = "20260908_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "condition_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("target_hash", sa.String(length=64), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("message_template", sa.Text(), nullable=False),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("trigger_on_initial", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("next_poll_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_state", sa.String(length=64), nullable=True),
        sa.Column("last_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("last_observed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("lease_until_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("transition_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_condition_subscriptions_state_next_poll_lease",
        "condition_subscriptions",
        ["state", "next_poll_at_utc", "lease_until_utc"],
    )
    op.create_index(
        "ix_condition_subscriptions_user_state",
        "condition_subscriptions",
        ["user_id", "state"],
    )
    op.create_index(
        "ix_condition_subscriptions_provider_target_hash",
        "condition_subscriptions",
        ["provider_type", "target_hash"],
    )

    op.create_table(
        "condition_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("observed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=True),
        sa.Column("fingerprint", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("retry_after_seconds", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["condition_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_condition_observations_subscription_observed_at",
        "condition_observations",
        ["subscription_id", "observed_at_utc"],
    )
    op.create_index(
        "ix_condition_observations_success_observed_at",
        "condition_observations",
        ["success", "observed_at_utc"],
    )

    op.create_table(
        "condition_transitions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_state", sa.String(length=64), nullable=True),
        sa.Column("current_state", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=True),
        sa.Column("observed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["condition_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "sequence",
            name="uq_condition_transitions_subscription_sequence",
        ),
    )
    op.create_index(
        "ix_condition_transitions_subscription_observed_at",
        "condition_transitions",
        ["subscription_id", "observed_at_utc"],
    )

    op.create_table(
        "condition_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("transition_id", sa.Integer(), nullable=False),
        sa.Column("transition_sequence", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("previous_state", sa.String(length=64), nullable=True),
        sa.Column("current_state", sa.String(length=64), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["condition_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["transition_id"], ["condition_transitions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "transition_sequence",
            name="uq_condition_deliveries_subscription_transition",
        ),
    )
    op.create_index(
        "ix_condition_deliveries_state_created_at",
        "condition_deliveries",
        ["state", "created_at"],
    )
    op.create_index(
        "ix_condition_deliveries_subscription_state",
        "condition_deliveries",
        ["subscription_id", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_condition_deliveries_subscription_state",
        table_name="condition_deliveries",
    )
    op.drop_index(
        "ix_condition_deliveries_state_created_at",
        table_name="condition_deliveries",
    )
    op.drop_table("condition_deliveries")

    op.drop_index(
        "ix_condition_transitions_subscription_observed_at",
        table_name="condition_transitions",
    )
    op.drop_table("condition_transitions")

    op.drop_index(
        "ix_condition_observations_success_observed_at",
        table_name="condition_observations",
    )
    op.drop_index(
        "ix_condition_observations_subscription_observed_at",
        table_name="condition_observations",
    )
    op.drop_table("condition_observations")

    op.drop_index(
        "ix_condition_subscriptions_provider_target_hash",
        table_name="condition_subscriptions",
    )
    op.drop_index(
        "ix_condition_subscriptions_user_state",
        table_name="condition_subscriptions",
    )
    op.drop_index(
        "ix_condition_subscriptions_state_next_poll_lease",
        table_name="condition_subscriptions",
    )
    op.drop_table("condition_subscriptions")
