"""rename evt.turn.started research rows to evt.research.started

Research turns historically wrote ``evt.turn.started`` with a research-shape
payload (``{"research_question": ..., "research_mode": ...}``), but the surface
contract for ``evt.turn.started`` is the main-turn shape
(``{"message": ..., "discord": ...}``). The read-side projector rejected those
rows with ``E_RESPONSE_CONTRACT`` on any session containing a research turn.

Producer is now ``evt.research.started`` (see ``research_runtime.run_research``
and ``response_contracts.SurfaceEventResearchStartedPayloadContract``). This
backfills the rows already on disk so the projector accepts them.

Idempotent: scoped by event type and the exact research-start payload domain, so
re-runs and rows already migrated to ``evt.research.started`` are untouched.

Revision ID: 20260521_0057
Revises: 20260520_0056
Create Date: 2026-05-21 00:57:00
"""

from __future__ import annotations

from alembic import op


revision = "20260521_0057"
down_revision = "20260520_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE events SET event_type = 'evt.research.started' "
        "WHERE event_type = 'evt.turn.started' "
        "AND payload ? 'research_question' "
        "AND payload ? 'research_mode' "
        "AND payload->>'research_mode' IN ('web', 'personal', 'memories')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE events SET event_type = 'evt.turn.started' "
        "WHERE event_type = 'evt.research.started' "
        "AND payload ? 'research_question' "
        "AND payload ? 'research_mode' "
        "AND payload->>'research_mode' IN ('web', 'personal', 'memories')"
    )
