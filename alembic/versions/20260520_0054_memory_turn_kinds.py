"""widen turns.kind to include memory_encode and memory_dream

The rememberer runs as a background task with no calling turn, so it creates
its own ``TurnRecord``. The honest kind for those rows is ``memory_encode`` or
``memory_dream`` — not ``research``.

Revision ID: 20260520_0054
Revises: 20260520_0053
Create Date: 2026-05-20 00:54:00
"""

from __future__ import annotations

from alembic import op


revision = "20260520_0054"
down_revision = "20260520_0053"
branch_labels = None
depends_on = None


_BEFORE = "kind IN ('agent_turn', 'research')"
_AFTER = "kind IN ('agent_turn', 'research', 'memory_encode', 'memory_dream')"


def upgrade() -> None:
    op.drop_constraint("ck_turn_kind", "turns", type_="check")
    op.create_check_constraint("ck_turn_kind", "turns", _AFTER)


def downgrade() -> None:
    op.drop_constraint("ck_turn_kind", "turns", type_="check")
    op.execute("DELETE FROM turns WHERE kind IN ('memory_encode', 'memory_dream')")
    op.create_check_constraint("ck_turn_kind", "turns", _BEFORE)
