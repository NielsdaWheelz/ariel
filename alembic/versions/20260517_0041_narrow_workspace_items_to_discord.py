"""narrow workspace_items to discord-only

Revision ID: 20260517_0041
Revises: 20260517_0040
Create Date: 2026-05-18 00:41:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260517_0041"
down_revision = "20260517_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_workspace_items_provider_type", table_name="workspace_items")
    op.drop_constraint("uq_workspace_item_external", "workspace_items", type_="unique")
    op.drop_constraint("ck_workspace_item_provider", "workspace_items", type_="check")
    op.drop_constraint("ck_workspace_item_type", "workspace_items", type_="check")
    op.drop_column("workspace_items", "provider")
    op.drop_column("workspace_items", "item_type")
    op.alter_column("workspace_items", "external_id", new_column_name="message_id")

    op.rename_table("workspace_items", "discord_messages")
    op.execute("ALTER INDEX workspace_items_pkey RENAME TO discord_messages_pkey")
    op.execute("ALTER INDEX ix_workspace_items_created_at RENAME TO ix_discord_messages_created_at")
    op.execute("ALTER INDEX ix_workspace_items_deleted_at RENAME TO ix_discord_messages_deleted_at")
    op.execute("ALTER INDEX ix_workspace_items_updated_at RENAME TO ix_discord_messages_updated_at")
    op.execute(
        "ALTER TABLE discord_messages "
        "RENAME CONSTRAINT ck_workspace_item_status TO ck_discord_message_status"
    )
    op.create_unique_constraint(
        "discord_messages_message_id_key", "discord_messages", ["message_id"]
    )

    op.drop_constraint("ck_workspace_item_event_type", "workspace_item_events", type_="check")
    op.alter_column(
        "workspace_item_events", "workspace_item_id", new_column_name="discord_message_id"
    )
    op.rename_table("workspace_item_events", "discord_message_events")
    op.execute("ALTER INDEX workspace_item_events_pkey RENAME TO discord_message_events_pkey")
    op.execute(
        "ALTER INDEX workspace_item_events_dedupe_key_key "
        "RENAME TO discord_message_events_dedupe_key_key"
    )
    op.execute(
        "ALTER INDEX ix_workspace_item_events_created_at "
        "RENAME TO ix_discord_message_events_created_at"
    )
    op.execute(
        "ALTER INDEX ix_workspace_item_events_provider_event_id "
        "RENAME TO ix_discord_message_events_provider_event_id"
    )
    op.execute(
        "ALTER INDEX ix_workspace_item_events_workspace_item_id "
        "RENAME TO ix_discord_message_events_discord_message_id"
    )
    op.execute(
        "ALTER TABLE discord_message_events RENAME CONSTRAINT "
        "workspace_item_events_provider_event_id_fkey "
        "TO discord_message_events_provider_event_id_fkey"
    )
    op.execute(
        "ALTER TABLE discord_message_events RENAME CONSTRAINT "
        "workspace_item_events_workspace_item_id_fkey "
        "TO discord_message_events_discord_message_id_fkey"
    )
    op.create_check_constraint(
        "ck_discord_message_event_type",
        "discord_message_events",
        "event_type IN ('created')",
    )

    op.alter_column(
        "proactive_observations", "workspace_item_id", new_column_name="discord_message_id"
    )
    op.execute(
        "ALTER INDEX ix_proactive_observations_workspace_item_id "
        "RENAME TO ix_proactive_observations_discord_message_id"
    )
    op.execute(
        "ALTER TABLE proactive_observations RENAME CONSTRAINT "
        "proactive_observations_workspace_item_id_fkey "
        "TO proactive_observations_discord_message_id_fkey"
    )
    op.drop_constraint(
        "ck_proactive_observation_source_type", "proactive_observations", type_="check"
    )
    op.create_check_constraint(
        "ck_proactive_observation_source_type",
        "proactive_observations",
        (
            "source_type IN ('discord_message', 'job', 'approval_request', "
            "'memory_assertion', 'google_connector', 'capture')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_proactive_observation_source_type", "proactive_observations", type_="check"
    )
    op.create_check_constraint(
        "ck_proactive_observation_source_type",
        "proactive_observations",
        (
            "source_type IN ('workspace_item', 'job', 'approval_request', "
            "'memory_assertion', 'google_connector', 'capture')"
        ),
    )
    op.execute(
        "ALTER TABLE proactive_observations RENAME CONSTRAINT "
        "proactive_observations_discord_message_id_fkey "
        "TO proactive_observations_workspace_item_id_fkey"
    )
    op.execute(
        "ALTER INDEX ix_proactive_observations_discord_message_id "
        "RENAME TO ix_proactive_observations_workspace_item_id"
    )
    op.alter_column(
        "proactive_observations", "discord_message_id", new_column_name="workspace_item_id"
    )

    op.drop_constraint("ck_discord_message_event_type", "discord_message_events", type_="check")
    op.execute(
        "ALTER TABLE discord_message_events RENAME CONSTRAINT "
        "discord_message_events_discord_message_id_fkey "
        "TO workspace_item_events_workspace_item_id_fkey"
    )
    op.execute(
        "ALTER TABLE discord_message_events RENAME CONSTRAINT "
        "discord_message_events_provider_event_id_fkey "
        "TO workspace_item_events_provider_event_id_fkey"
    )
    op.execute(
        "ALTER INDEX ix_discord_message_events_discord_message_id "
        "RENAME TO ix_workspace_item_events_workspace_item_id"
    )
    op.execute(
        "ALTER INDEX ix_discord_message_events_provider_event_id "
        "RENAME TO ix_workspace_item_events_provider_event_id"
    )
    op.execute(
        "ALTER INDEX ix_discord_message_events_created_at "
        "RENAME TO ix_workspace_item_events_created_at"
    )
    op.execute(
        "ALTER INDEX discord_message_events_dedupe_key_key "
        "RENAME TO workspace_item_events_dedupe_key_key"
    )
    op.execute("ALTER INDEX discord_message_events_pkey RENAME TO workspace_item_events_pkey")
    op.rename_table("discord_message_events", "workspace_item_events")
    op.alter_column(
        "workspace_item_events", "discord_message_id", new_column_name="workspace_item_id"
    )
    op.create_check_constraint(
        "ck_workspace_item_event_type",
        "workspace_item_events",
        "event_type IN ('created', 'updated', 'deleted', 'restored')",
    )

    op.drop_constraint("discord_messages_message_id_key", "discord_messages", type_="unique")
    op.execute(
        "ALTER TABLE discord_messages "
        "RENAME CONSTRAINT ck_discord_message_status TO ck_workspace_item_status"
    )
    op.execute("ALTER INDEX ix_discord_messages_updated_at RENAME TO ix_workspace_items_updated_at")
    op.execute("ALTER INDEX ix_discord_messages_deleted_at RENAME TO ix_workspace_items_deleted_at")
    op.execute("ALTER INDEX ix_discord_messages_created_at RENAME TO ix_workspace_items_created_at")
    op.execute("ALTER INDEX discord_messages_pkey RENAME TO workspace_items_pkey")
    op.rename_table("discord_messages", "workspace_items")

    op.alter_column("workspace_items", "message_id", new_column_name="external_id")
    op.add_column(
        "workspace_items",
        sa.Column("item_type", sa.String(length=32), nullable=False),
    )
    op.add_column(
        "workspace_items",
        sa.Column("provider", sa.String(length=32), nullable=False),
    )
    op.create_check_constraint(
        "ck_workspace_item_type",
        "workspace_items",
        (
            "item_type IN ('calendar_event', 'email_message', 'drive_file', "
            "'internal_state', 'discord_message')"
        ),
    )
    op.create_check_constraint(
        "ck_workspace_item_provider",
        "workspace_items",
        "provider IN ('google', 'ariel', 'discord')",
    )
    op.create_unique_constraint(
        "uq_workspace_item_external",
        "workspace_items",
        ["provider", "item_type", "external_id"],
    )
    op.create_index(
        "ix_workspace_items_provider_type",
        "workspace_items",
        ["provider", "item_type", "updated_at"],
    )
