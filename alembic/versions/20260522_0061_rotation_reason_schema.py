"""narrow session rotation reason schema

Revision ID: 20260522_0061
Revises: 20260522_0060
Create Date: 2026-05-22 01:01:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260522_0061"
down_revision = "20260522_0060"
branch_labels = None
depends_on = None


_SESSION_REASON_BEFORE = (
    "(rotation_reason IS NULL) OR "
    "(rotation_reason IN ('user_initiated', 'threshold_turn_count', "
    "'threshold_age', 'threshold_context_pressure'))"
)
_SESSION_REASON_AFTER = (
    "(rotation_reason IS NULL) OR "
    "(rotation_reason IN ('user_initiated', 'threshold_turn_count', 'threshold_age'))"
)
_ROTATION_REASON_BEFORE = (
    "reason IN ('user_initiated', 'threshold_turn_count', "
    "'threshold_age', 'threshold_context_pressure')"
)
_ROTATION_REASON_AFTER = "reason IN ('user_initiated', 'threshold_turn_count', 'threshold_age')"


def upgrade() -> None:
    bind = op.get_bind()
    session_count = bind.scalar(
        sa.text(
            "SELECT count(*) FROM sessions WHERE rotation_reason = 'threshold_context_pressure'"
        )
    )
    rotation_count = bind.scalar(
        sa.text(
            "SELECT count(*) FROM session_rotations WHERE reason = 'threshold_context_pressure'"
        )
    )
    if session_count or rotation_count:
        raise RuntimeError("threshold_context_pressure rotation rows must be repaired first")

    op.drop_constraint("ck_session_rotation_reason", "sessions", type_="check")
    op.create_check_constraint(
        "ck_session_rotation_reason",
        "sessions",
        _SESSION_REASON_AFTER,
    )
    op.drop_constraint(
        "ck_session_rotation_reason_type",
        "session_rotations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_session_rotation_reason_type",
        "session_rotations",
        _ROTATION_REASON_AFTER,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_session_rotation_reason_type",
        "session_rotations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_session_rotation_reason_type",
        "session_rotations",
        _ROTATION_REASON_BEFORE,
    )
    op.drop_constraint("ck_session_rotation_reason", "sessions", type_="check")
    op.create_check_constraint(
        "ck_session_rotation_reason",
        "sessions",
        _SESSION_REASON_BEFORE,
    )
