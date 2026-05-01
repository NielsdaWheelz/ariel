"""cut memory embeddings over to pgvector

Revision ID: 20260501_0017
Revises: 20260501_0016
Create Date: 2026-05-01 02:00:00
"""

from __future__ import annotations

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260501_0017"
down_revision = "20260501_0016"
branch_labels = None
depends_on = None


EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("DELETE FROM memory_embedding_projections")

    op.drop_column("memory_embedding_projections", "embedding")
    op.add_column(
        "memory_embedding_projections",
        sa.Column(
            "embedding_provider",
            sa.String(length=32),
            nullable=False,
            server_default="openai",
        ),
    )
    op.add_column(
        "memory_embedding_projections",
        sa.Column(
            "embedding_model",
            sa.String(length=128),
            nullable=False,
            server_default="text-embedding-3-small",
        ),
    )
    op.add_column(
        "memory_embedding_projections",
        sa.Column(
            "embedding_dimensions",
            sa.Integer(),
            nullable=False,
            server_default=str(EMBEDDING_DIMENSIONS),
        ),
    )
    op.add_column(
        "memory_embedding_projections",
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
    )
    op.create_check_constraint(
        "ck_memory_embedding_projection_dimensions",
        "memory_embedding_projections",
        f"embedding_dimensions = {EMBEDDING_DIMENSIONS}",
    )
    op.execute(
        "CREATE INDEX ix_memory_embedding_projections_embedding_hnsw "
        "ON memory_embedding_projections "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.alter_column("memory_embedding_projections", "embedding_provider", server_default=None)
    op.alter_column("memory_embedding_projections", "embedding_model", server_default=None)
    op.alter_column("memory_embedding_projections", "embedding_dimensions", server_default=None)


def downgrade() -> None:
    op.execute("DELETE FROM memory_embedding_projections")
    op.execute("DROP INDEX IF EXISTS ix_memory_embedding_projections_embedding_hnsw")
    op.drop_constraint(
        "ck_memory_embedding_projection_dimensions",
        "memory_embedding_projections",
        type_="check",
    )
    op.drop_column("memory_embedding_projections", "embedding")
    op.drop_column("memory_embedding_projections", "embedding_dimensions")
    op.drop_column("memory_embedding_projections", "embedding_model")
    op.drop_column("memory_embedding_projections", "embedding_provider")
    op.add_column(
        "memory_embedding_projections",
        sa.Column(
            "embedding",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("memory_embedding_projections", "embedding", server_default=None)
