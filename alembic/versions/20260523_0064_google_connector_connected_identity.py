"""require account identity for connected Google connectors

Revision ID: 20260523_0064
Revises: 20260522_0063
Create Date: 2026-05-23 14:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260523_0064"
down_revision = "20260522_0063"
branch_labels = None
depends_on = None


_CONNECTED_ACCOUNT_SUBJECT_CHECK = (
    "status <> 'connected' OR ("
    "account_subject IS NOT NULL "
    "AND btrim(account_subject) <> '' "
    "AND account_subject !~ '[[:space:]]'"
    ")"
)
_CONNECTED_ACCOUNT_EMAIL_CHECK = (
    "status <> 'connected' OR ("
    "account_email IS NOT NULL "
    "AND btrim(account_email) <> '' "
    "AND account_email !~ '[[:space:]]' "
    "AND length(account_email) - length(replace(account_email, '@', '')) = 1 "
    "AND position('@' in account_email) > 1 "
    "AND position('@' in account_email) < length(account_email)"
    ")"
)


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE google_connectors
            SET status = 'error',
                account_subject = NULL,
                account_email = NULL,
                last_error_code = 'account_identity_missing',
                last_error_at = now(),
                updated_at = now()
            WHERE status = 'connected'
              AND (
                  account_subject IS NULL
                  OR btrim(account_subject) = ''
                  OR account_subject ~ '[[:space:]]'
                  OR account_email IS NULL
                  OR btrim(account_email) = ''
                  OR account_email ~ '[[:space:]]'
                  OR length(account_email) - length(replace(account_email, '@', '')) <> 1
                  OR position('@' in account_email) <= 1
                  OR position('@' in account_email) >= length(account_email)
              )
            """
        )
    )
    op.create_check_constraint(
        "ck_google_connector_connected_account_subject",
        "google_connectors",
        _CONNECTED_ACCOUNT_SUBJECT_CHECK,
    )
    op.create_check_constraint(
        "ck_google_connector_connected_account_email",
        "google_connectors",
        _CONNECTED_ACCOUNT_EMAIL_CHECK,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_google_connector_connected_account_email",
        "google_connectors",
        type_="check",
    )
    op.drop_constraint(
        "ck_google_connector_connected_account_subject",
        "google_connectors",
        type_="check",
    )
