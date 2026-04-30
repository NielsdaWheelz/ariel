"""add google connector oauth state and audit tables

Revision ID: 20260303_0006
Revises: 20260303_0005
Create Date: 2026-03-03 21:25:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260303_0006"
down_revision = "20260303_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_connectors",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("account_subject", sa.String(length=255), nullable=True),
        sa.Column("account_email", sa.String(length=320), nullable=True),
        sa.Column("granted_scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=True),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_obtained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("encryption_key_version", sa.String(length=32), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google')", name="ck_google_connector_provider"),
        sa.CheckConstraint(
            "status IN ('not_connected', 'connected', 'error', 'revoked')",
            name="ck_google_connector_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_google_connectors_access_token_expires_at",
        "google_connectors",
        ["access_token_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_google_connectors_created_at", "google_connectors", ["created_at"], unique=False
    )
    op.create_index(
        "ix_google_connectors_last_error_at", "google_connectors", ["last_error_at"], unique=False
    )
    op.create_index(
        "ix_google_connectors_updated_at", "google_connectors", ["updated_at"], unique=False
    )

    op.create_table(
        "google_oauth_states",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("state_handle", sa.String(length=128), nullable=False),
        sa.Column("flow", sa.String(length=16), nullable=False),
        sa.Column("requested_scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("pkce_verifier_enc", sa.Text(), nullable=False),
        sa.Column("pkce_challenge", sa.String(length=128), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("flow IN ('connect', 'reconnect')", name="ck_google_oauth_state_flow"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_google_oauth_states_state_handle", "google_oauth_states", ["state_handle"], unique=True
    )
    op.create_index(
        "ix_google_oauth_states_expires_at", "google_oauth_states", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_google_oauth_states_consumed_at", "google_oauth_states", ["consumed_at"], unique=False
    )
    op.create_index(
        "ix_google_oauth_states_created_at", "google_oauth_states", ["created_at"], unique=False
    )

    op.create_table(
        "google_connector_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("connector_id", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connector_id"], ["google_connectors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_google_connector_events_connector_id",
        "google_connector_events",
        ["connector_id"],
        unique=False,
    )
    op.create_index(
        "ix_google_connector_events_created_at",
        "google_connector_events",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_google_connector_events_created_at", table_name="google_connector_events")
    op.drop_index("ix_google_connector_events_connector_id", table_name="google_connector_events")
    op.drop_table("google_connector_events")

    op.drop_index("ix_google_oauth_states_created_at", table_name="google_oauth_states")
    op.drop_index("ix_google_oauth_states_consumed_at", table_name="google_oauth_states")
    op.drop_index("ix_google_oauth_states_expires_at", table_name="google_oauth_states")
    op.drop_index("ix_google_oauth_states_state_handle", table_name="google_oauth_states")
    op.drop_table("google_oauth_states")

    op.drop_index("ix_google_connectors_updated_at", table_name="google_connectors")
    op.drop_index("ix_google_connectors_last_error_at", table_name="google_connectors")
    op.drop_index("ix_google_connectors_created_at", table_name="google_connectors")
    op.drop_index("ix_google_connectors_access_token_expires_at", table_name="google_connectors")
    op.drop_table("google_connectors")
