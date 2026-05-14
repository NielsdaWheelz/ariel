"""add agency sandbox metadata and PR receipts

Revision ID: 20260513_0025
Revises: 20260512_0024
Create Date: 2026-05-13 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260513_0025"
down_revision = "20260512_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "agency_sandbox_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "agency_egress_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_jobs_agency_sandbox_policy_object",
        "jobs",
        "jsonb_typeof(agency_sandbox_policy) = 'object'",
    )
    op.create_check_constraint(
        "ck_jobs_agency_egress_policy_object",
        "jobs",
        "jsonb_typeof(agency_egress_policy) = 'object'",
    )

    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
        "'memory_extraction', 'continuity_compaction', 'feedback_learning', "
        "'ambient_interpretation', 'proactive_deliberation', 'model_output', "
        "'workspace_commitment_extraction', 'tool_strategy')",
    )

    op.drop_constraint(
        "ck_provider_write_receipt_provider", "provider_write_receipts", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_write_receipt_provider",
        "provider_write_receipts",
        "provider IN ('google', 'agency')",
    )
    op.drop_constraint(
        "ck_provider_write_receipt_capability",
        "provider_write_receipts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_provider_write_receipt_capability",
        "provider_write_receipts",
        "capability_id IN ('cap.email.draft', 'cap.email.send', "
        "'cap.email.archive', 'cap.email.trash', 'cap.email.labels.modify', "
        "'cap.email.undo', 'cap.calendar.create_event', 'cap.calendar.update_event', "
        "'cap.calendar.respond_to_event', 'cap.drive.share', 'cap.agency.request_pr')",
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM background_tasks
        WHERE task_type = 'provider_write_reconcile_due'
        AND payload->>'provider_write_receipt_id' IN (
            SELECT id
            FROM provider_write_receipts
            WHERE provider = 'agency'
            OR capability_id = 'cap.agency.request_pr'
        )
        """
    )
    op.execute(
        """
        DELETE FROM provider_write_receipts
        WHERE provider = 'agency'
        OR capability_id = 'cap.agency.request_pr'
        """
    )
    op.execute("DELETE FROM ai_judgments WHERE judgment_type = 'tool_strategy'")
    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
        "'memory_extraction', 'continuity_compaction', 'feedback_learning', "
        "'ambient_interpretation', 'proactive_deliberation', 'model_output', "
        "'workspace_commitment_extraction')",
    )
    op.drop_constraint(
        "ck_provider_write_receipt_capability",
        "provider_write_receipts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_provider_write_receipt_capability",
        "provider_write_receipts",
        "capability_id IN ('cap.email.draft', 'cap.email.send', "
        "'cap.email.archive', 'cap.email.trash', 'cap.email.labels.modify', "
        "'cap.email.undo', 'cap.calendar.create_event', 'cap.calendar.update_event', "
        "'cap.calendar.respond_to_event', 'cap.drive.share')",
    )
    op.drop_constraint(
        "ck_provider_write_receipt_provider", "provider_write_receipts", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_write_receipt_provider",
        "provider_write_receipts",
        "provider IN ('google')",
    )
    op.drop_constraint("ck_jobs_agency_egress_policy_object", "jobs", type_="check")
    op.drop_constraint("ck_jobs_agency_sandbox_policy_object", "jobs", type_="check")
    op.drop_column("jobs", "agency_egress_policy")
    op.drop_column("jobs", "agency_sandbox_policy")
