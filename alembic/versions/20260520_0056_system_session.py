"""seed the singleton system session row

Background-only work — currently the scheduled ``memory_dream`` rememberer —
runs without a calling user session, but ``turns.session_id`` and other
session-FK columns are NOT NULL with ``ondelete='RESTRICT'``. Without a row to
point at, every dream task fails with ``ForeignKeyViolation`` on
``turns_session_id_fkey`` and the worker loops on retries (see
``src/ariel/memory.py``::``run_rememberer``).

This migration seeds the stable-id ``ses_system`` row (mirroring the
``con_google`` singleton pattern). The row is ``is_active=FALSE,
lifecycle_state='closed'`` so it never collides with the partial unique index
``ix_single_active_session`` and is invisible to ``_get_or_create_active_session``.

Idempotent: ``INSERT ... ON CONFLICT DO NOTHING`` so re-running the migration
or running it on a DB that already has the row is safe.

Revision ID: 20260520_0056
Revises: 20260520_0055
Create Date: 2026-05-20 00:56:00
"""

from __future__ import annotations

from alembic import op


revision = "20260520_0056"
down_revision = "20260520_0055"
branch_labels = None
depends_on = None


SYSTEM_SESSION_ID = "ses_system"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO sessions (
            id, is_active, lifecycle_state,
            rotated_from_session_id, rotation_reason,
            created_at, updated_at
        ) VALUES (
            '{SYSTEM_SESSION_ID}', FALSE, 'closed',
            NULL, NULL,
            NOW(), NOW()
        )
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    # A downgrade only removes the singleton row when no rows still reference
    # it; if any background-owned turn still exists, RESTRICT will block the
    # delete and an operator can manually decide. We deliberately do not
    # cascade — the data is real.
    op.execute(f"DELETE FROM sessions WHERE id = '{SYSTEM_SESSION_ID}'")
