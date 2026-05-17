"""drop terminal command records

Revision ID: 20260517_0034
Revises: 20260516_0033
Create Date: 2026-05-17 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260517_0034"
down_revision = "20260516_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_terminal_commands_session_command_unique", table_name="terminal_commands")
    op.drop_index("ix_terminal_commands_created_at", table_name="terminal_commands")
    op.drop_index("ix_terminal_commands_action_attempt_id", table_name="terminal_commands")
    op.drop_index("ix_terminal_commands_turn_id", table_name="terminal_commands")
    op.drop_index("ix_terminal_commands_session_id", table_name="terminal_commands")
    op.drop_table("terminal_commands")


def downgrade() -> None:
    op.create_table(
        "terminal_commands",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("turn_id", sa.String(length=32), nullable=False),
        sa.Column("action_attempt_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("policy_decision", sa.String(length=32), nullable=False),
        sa.Column("policy_reason", sa.Text(), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("process_group_id", sa.Integer(), nullable=True),
        sa.Column("process_start_token", sa.Text(), nullable=True),
        sa.Column("terminal_dir", sa.Text(), nullable=False),
        sa.Column("stdout_path", sa.Text(), nullable=False),
        sa.Column("stderr_path", sa.Text(), nullable=False),
        sa.Column("exit_path", sa.Text(), nullable=True),
        sa.Column("stdout_bytes", sa.Integer(), nullable=False),
        sa.Column("stderr_bytes", sa.Integer(), nullable=False),
        sa.Column("output_limit_bytes", sa.Integer(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('foreground', 'background')",
            name="ck_terminal_command_kind",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'timeout', 'cancelled', 'denied', "
            "'unknown')",
            name="ck_terminal_command_status",
        ),
        sa.CheckConstraint(
            "policy_decision IN ('allow_inline', 'requires_approval', 'deny')",
            name="ck_terminal_command_policy_decision",
        ),
        sa.CheckConstraint(
            "stdout_bytes >= 0",
            name="ck_terminal_command_stdout_bytes_nonnegative",
        ),
        sa.CheckConstraint(
            "stderr_bytes >= 0",
            name="ck_terminal_command_stderr_bytes_nonnegative",
        ),
        sa.CheckConstraint(
            "output_limit_bytes > 0",
            name="ck_terminal_command_output_limit_bytes_positive",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_terminal_command_duration_ms_nonnegative",
        ),
        sa.CheckConstraint(
            (
                "(status = 'running' AND completed_at IS NULL AND exit_code IS NULL) OR "
                "(status = 'unknown' AND completed_at IS NOT NULL) OR "
                "(status IN ('completed', 'failed', 'timeout', 'cancelled', 'denied') "
                "AND completed_at IS NOT NULL AND exit_code IS NOT NULL)"
            ),
            name="ck_terminal_command_status_fields",
        ),
        sa.ForeignKeyConstraint(["action_attempt_id"], ["action_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_terminal_commands_session_id", "terminal_commands", ["session_id"])
    op.create_index("ix_terminal_commands_turn_id", "terminal_commands", ["turn_id"])
    op.create_index(
        "ix_terminal_commands_action_attempt_id",
        "terminal_commands",
        ["action_attempt_id"],
    )
    op.create_index("ix_terminal_commands_created_at", "terminal_commands", ["created_at"])
    op.create_index(
        "ix_terminal_commands_session_command_unique",
        "terminal_commands",
        ["session_id", "command_id"],
        unique=True,
    )
