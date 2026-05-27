"""cut over action success events to execution_output payloads

Revision ID: 20260527_0071
Revises: 20260526_0070
Create Date: 2026-05-27 05:45:00
"""

from __future__ import annotations

from alembic import op


revision = "20260527_0071"
down_revision = "20260526_0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE events AS e
        SET payload =
            jsonb_build_object(
                'action_attempt_id',
                COALESCE(e.payload ->> 'action_attempt_id', 'unknown_action_attempt'),
                'capability_id',
                COALESCE(
                    (
                        SELECT a.capability_id
                        FROM action_attempts AS a
                        WHERE a.id = e.payload ->> 'action_attempt_id'
                        LIMIT 1
                    ),
                    e.payload ->> 'capability_id',
                    'unknown.capability'
                ),
                'status',
                'succeeded',
                'execution_output',
                COALESCE(
                    e.payload -> 'output',
                    e.payload -> 'execution_output',
                    'null'::jsonb
                )
            )
            ||
            jsonb_strip_nulls(
                jsonb_build_object(
                    'provider_write_receipt_id',
                    e.payload -> 'provider_write_receipt_id',
                    'replayed_provider_write_receipt_id',
                    e.payload -> 'replayed_provider_write_receipt_id',
                    'reconciled',
                    e.payload -> 'reconciled'
                )
            )
        WHERE e.event_type = 'evt.action.execution.succeeded'
          AND e.payload ? 'output'
        """
    )


def downgrade() -> None:
    raise NotImplementedError("action success event payload cutover is forward-only")
