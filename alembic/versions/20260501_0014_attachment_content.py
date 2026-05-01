"""add attachment content records

Revision ID: 20260501_0014
Revises: 20260430_0013
Create Date: 2026-05-01 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260501_0014"
down_revision = "20260430_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachment_blobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sniffed_mime_type", sa.String(length=256), nullable=False),
        sa.Column("scan_status", sa.String(length=32), nullable=False),
        sa.Column("scanner_version", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("size_bytes >= 0", name="ck_attachment_blob_size_nonnegative"),
        sa.CheckConstraint(
            "scan_status IN ('clean', 'unsafe', 'scan_failed')",
            name="ck_attachment_blob_scan_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash"),
    )
    op.create_index("ix_attachment_blobs_created_at", "attachment_blobs", ["created_at"])

    op.create_table(
        "attachment_sources",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("turn_id", sa.String(length=32), nullable=False),
        sa.Column("source_transport", sa.String(length=32), nullable=False),
        sa.Column("source_message_id", sa.String(length=64), nullable=False),
        sa.Column("source_channel_id", sa.String(length=64), nullable=False),
        sa.Column("source_guild_id", sa.String(length=64), nullable=True),
        sa.Column("source_author_id", sa.String(length=64), nullable=False),
        sa.Column("source_attachment_id", sa.String(length=64), nullable=False),
        sa.Column("attachment_ref", sa.String(length=256), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("declared_content_type", sa.String(length=256), nullable=True),
        sa.Column("declared_size_bytes", sa.Integer(), nullable=True),
        sa.Column("acquisition_url_enc", sa.Text(), nullable=True),
        sa.Column("acquisition_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blob_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_transport IN ('discord')",
            name="ck_attachment_source_transport",
        ),
        sa.CheckConstraint(
            "(declared_size_bytes IS NULL) OR (declared_size_bytes >= 0)",
            name="ck_attachment_source_declared_size_nonnegative",
        ),
        sa.ForeignKeyConstraint(["blob_id"], ["attachment_blobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attachment_sources_blob_id", "attachment_sources", ["blob_id"])
    op.create_index("ix_attachment_sources_created_at", "attachment_sources", ["created_at"])
    op.create_index("ix_attachment_sources_session_id", "attachment_sources", ["session_id"])
    op.create_index(
        "ix_attachment_sources_session_turn_ref",
        "attachment_sources",
        ["session_id", "turn_id", "attachment_ref"],
        unique=True,
    )
    op.create_index("ix_attachment_sources_turn_id", "attachment_sources", ["turn_id"])

    op.create_table(
        "attachment_extractions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=32), nullable=False),
        sa.Column("blob_id", sa.String(length=32), nullable=False),
        sa.Column("modality", sa.String(length=32), nullable=False),
        sa.Column("extractor", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("blocks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "modality IN ('text', 'document', 'image', 'audio', 'unknown')",
            name="ck_attachment_extraction_modality",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_attachment_extraction_status",
        ),
        sa.CheckConstraint(
            (
                "outcome IN ('ok', 'unsupported_type', 'too_large', 'expired', "
                "'unavailable', 'unsafe', 'scan_failed', 'extract_failed', "
                "'provider_timeout', 'provider_unavailable', 'resource_limit')"
            ),
            name="ck_attachment_extraction_outcome",
        ),
        sa.ForeignKeyConstraint(["blob_id"], ["attachment_blobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["attachment_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attachment_extractions_blob_id", "attachment_extractions", ["blob_id"])
    op.create_index(
        "ix_attachment_extractions_created_at", "attachment_extractions", ["created_at"]
    )
    op.create_index("ix_attachment_extractions_source_id", "attachment_extractions", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_attachment_extractions_source_id", table_name="attachment_extractions")
    op.drop_index("ix_attachment_extractions_created_at", table_name="attachment_extractions")
    op.drop_index("ix_attachment_extractions_blob_id", table_name="attachment_extractions")
    op.drop_table("attachment_extractions")
    op.drop_index("ix_attachment_sources_turn_id", table_name="attachment_sources")
    op.drop_index("ix_attachment_sources_session_turn_ref", table_name="attachment_sources")
    op.drop_index("ix_attachment_sources_session_id", table_name="attachment_sources")
    op.drop_index("ix_attachment_sources_created_at", table_name="attachment_sources")
    op.drop_index("ix_attachment_sources_blob_id", table_name="attachment_sources")
    op.drop_table("attachment_sources")
    op.drop_index("ix_attachment_blobs_created_at", table_name="attachment_blobs")
    op.drop_table("attachment_blobs")
