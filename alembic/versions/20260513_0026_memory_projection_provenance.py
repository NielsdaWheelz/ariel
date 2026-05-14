"""add memory projection provenance

Revision ID: 20260513_0026
Revises: 20260513_0025
Create Date: 2026-05-13 17:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260513_0026"
down_revision = "20260513_0025"
branch_labels = None
depends_on = None


def _add_source_memory_version(table_name: str, constraint_name: str) -> None:
    op.add_column(
        table_name,
        sa.Column("source_memory_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        constraint_name,
        table_name,
        "source_memory_version > 0",
    )
    op.alter_column(table_name, "source_memory_version", server_default=None)


def _backfill_source_memory_version(
    table_name: str,
    *,
    table_expr: str,
    id_expr: str,
) -> None:
    op.execute(
        sa.text(
            f"""
            UPDATE {table_name} AS projection
            SET source_memory_version = latest.version
            FROM (
                SELECT canonical_table, canonical_id, max(version) AS version
                FROM memory_versions
                GROUP BY canonical_table, canonical_id
            ) AS latest
            WHERE latest.canonical_table = {table_expr}
            AND latest.canonical_id = {id_expr}
            """
        )
    )


def upgrade() -> None:
    op.alter_column(
        "memory_projection_jobs",
        "target_id",
        existing_type=sa.String(length=32),
        type_=sa.Text(),
        existing_nullable=False,
    )
    op.add_column(
        "memory_projection_jobs",
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "memory_projection_jobs",
        sa.Column("attempt_token", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "memory_projection_jobs",
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_memory_projection_jobs_last_heartbeat",
        "memory_projection_jobs",
        ["last_heartbeat"],
    )

    _add_source_memory_version(
        "memory_embedding_projections",
        "ck_memory_embedding_projection_source_memory_version",
    )
    _backfill_source_memory_version(
        "memory_embedding_projections",
        table_expr="'memory_assertions'",
        id_expr="projection.assertion_id",
    )

    _add_source_memory_version(
        "memory_keyword_projections",
        "ck_memory_keyword_projection_source_memory_version",
    )
    _backfill_source_memory_version(
        "memory_keyword_projections",
        table_expr="projection.canonical_table",
        id_expr="projection.canonical_id",
    )

    _add_source_memory_version(
        "memory_entity_projections",
        "ck_memory_entity_projection_source_memory_version",
    )
    _backfill_source_memory_version(
        "memory_entity_projections",
        table_expr="projection.canonical_table",
        id_expr="projection.canonical_id",
    )

    _add_source_memory_version(
        "memory_temporal_projections",
        "ck_memory_temporal_projection_source_memory_version",
    )
    _backfill_source_memory_version(
        "memory_temporal_projections",
        table_expr="projection.canonical_table",
        id_expr="projection.canonical_id",
    )

    _add_source_memory_version(
        "memory_symbol_projections",
        "ck_memory_symbol_projection_source_memory_version",
    )
    _backfill_source_memory_version(
        "memory_symbol_projections",
        table_expr="projection.canonical_table",
        id_expr="projection.canonical_id",
    )

    op.add_column(
        "memory_graph_projections",
        sa.Column(
            "source_memory_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "memory_graph_projections",
        sa.Column(
            "source_projection_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_memory_graph_projection_source_memory_versions_object",
        "memory_graph_projections",
        "jsonb_typeof(source_memory_versions) = 'object'",
    )
    op.create_check_constraint(
        "ck_memory_graph_projection_source_projection_versions_object",
        "memory_graph_projections",
        "jsonb_typeof(source_projection_versions) = 'object'",
    )
    op.execute(
        sa.text(
            """
            DELETE FROM memory_graph_projections
            WHERE source_memory_versions = '{}'::jsonb
            """
        )
    )
    op.alter_column(
        "memory_graph_projections",
        "source_memory_versions",
        server_default=None,
    )
    op.alter_column(
        "memory_graph_projections",
        "source_projection_versions",
        server_default=None,
    )

    op.add_column(
        "memory_context_blocks",
        sa.Column(
            "source_projection_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_memory_context_block_source_memory_versions_object",
        "memory_context_blocks",
        "jsonb_typeof(source_memory_versions) = 'object'",
    )
    op.create_check_constraint(
        "ck_memory_context_block_source_projection_versions_object",
        "memory_context_blocks",
        "jsonb_typeof(source_projection_versions) = 'object'",
    )
    op.execute(
        sa.text(
            """
            UPDATE memory_context_blocks
            SET source_projection_versions = jsonb_build_object(
                'memory_context_blocks',
                projection_version
            )
            WHERE source_memory_versions != '{}'::jsonb
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM memory_context_blocks
            WHERE source_memory_versions = '{}'::jsonb
            OR source_memory_versions = jsonb_build_object(
                'memory_assertions', '{}'::jsonb,
                'memory_action_traces', '{}'::jsonb,
                'memory_procedures', '{}'::jsonb,
                'project_state_snapshots', '{}'::jsonb
            )
            """
        )
    )
    op.alter_column(
        "memory_context_blocks",
        "source_projection_versions",
        server_default=None,
    )

    op.add_column(
        "memory_export_artifacts",
        sa.Column(
            "projection_version",
            sa.String(length=32),
            nullable=False,
            server_default="embedding-v1",
        ),
    )
    op.add_column(
        "memory_export_artifacts",
        sa.Column(
            "source_memory_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "memory_export_artifacts",
        sa.Column(
            "source_projection_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_memory_export_artifact_source_memory_versions_object",
        "memory_export_artifacts",
        "jsonb_typeof(source_memory_versions) = 'object'",
    )
    op.create_check_constraint(
        "ck_memory_export_artifact_source_projection_versions_object",
        "memory_export_artifacts",
        "jsonb_typeof(source_projection_versions) = 'object'",
    )
    op.execute(
        sa.text(
            """
            UPDATE memory_export_artifacts
            SET status = 'failed',
                content = '{}'::jsonb,
                source_counts = source_counts || jsonb_build_object(
                    'migration_failed_reason',
                    'projection provenance could not be reconstructed'
                ),
                source_projection_versions = jsonb_build_object(
                    'memory_export_artifacts',
                    projection_version
                )
            WHERE source_memory_versions = '{}'::jsonb
            """
        )
    )
    op.alter_column("memory_export_artifacts", "projection_version", server_default=None)
    op.alter_column("memory_export_artifacts", "source_memory_versions", server_default=None)
    op.alter_column(
        "memory_export_artifacts",
        "source_projection_versions",
        server_default=None,
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM memory_projection_jobs WHERE length(target_id) > 32"))
    op.alter_column(
        "memory_projection_jobs",
        "target_id",
        existing_type=sa.Text(),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.drop_index("ix_memory_projection_jobs_last_heartbeat", table_name="memory_projection_jobs")
    op.drop_column("memory_projection_jobs", "last_heartbeat")
    op.drop_column("memory_projection_jobs", "attempt_token")
    op.drop_column("memory_projection_jobs", "claimed_by")

    op.drop_constraint(
        "ck_memory_export_artifact_source_projection_versions_object",
        "memory_export_artifacts",
        type_="check",
    )
    op.drop_constraint(
        "ck_memory_export_artifact_source_memory_versions_object",
        "memory_export_artifacts",
        type_="check",
    )
    op.drop_column("memory_export_artifacts", "source_projection_versions")
    op.drop_column("memory_export_artifacts", "source_memory_versions")
    op.drop_column("memory_export_artifacts", "projection_version")

    op.drop_constraint(
        "ck_memory_context_block_source_projection_versions_object",
        "memory_context_blocks",
        type_="check",
    )
    op.drop_constraint(
        "ck_memory_context_block_source_memory_versions_object",
        "memory_context_blocks",
        type_="check",
    )
    op.drop_column("memory_context_blocks", "source_projection_versions")

    op.drop_constraint(
        "ck_memory_graph_projection_source_projection_versions_object",
        "memory_graph_projections",
        type_="check",
    )
    op.drop_constraint(
        "ck_memory_graph_projection_source_memory_versions_object",
        "memory_graph_projections",
        type_="check",
    )
    op.drop_column("memory_graph_projections", "source_projection_versions")
    op.drop_column("memory_graph_projections", "source_memory_versions")

    op.drop_constraint(
        "ck_memory_symbol_projection_source_memory_version",
        "memory_symbol_projections",
        type_="check",
    )
    op.drop_column("memory_symbol_projections", "source_memory_version")

    op.drop_constraint(
        "ck_memory_temporal_projection_source_memory_version",
        "memory_temporal_projections",
        type_="check",
    )
    op.drop_column("memory_temporal_projections", "source_memory_version")

    op.drop_constraint(
        "ck_memory_entity_projection_source_memory_version",
        "memory_entity_projections",
        type_="check",
    )
    op.drop_column("memory_entity_projections", "source_memory_version")

    op.drop_constraint(
        "ck_memory_keyword_projection_source_memory_version",
        "memory_keyword_projections",
        type_="check",
    )
    op.drop_column("memory_keyword_projections", "source_memory_version")

    op.drop_constraint(
        "ck_memory_embedding_projection_source_memory_version",
        "memory_embedding_projections",
        type_="check",
    )
    op.drop_column("memory_embedding_projections", "source_memory_version")
