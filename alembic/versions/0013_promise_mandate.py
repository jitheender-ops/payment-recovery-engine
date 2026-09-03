"""Promise mandate — the UPI Autopay authorisation a promise may carry.

Revision ID: 0013_promise_mandate
Revises: 0012_case_credited_refs
Create Date: 2026-09-03

A promise to pay collected nothing: it deferred chasing, sent a reminder 48h
before the date, and waited for the customer to pull the money. These columns
let a promise carry an authorised UPI Autopay mandate, so the scheduler can
debit it on the promised date instead of hoping.

mandate_status is NOT NULL with server_default 'none' from the start — no
post-add ALTER (SQLite cannot ALTER COLUMN) and no backfill gap. Every existing
promise reads as 'none', which is exactly the pre-existing behaviour: no
mandate, reminder plus link, customer pulls.

Additive, inspector-guarded, idempotent like every revision here.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_promise_mandate"
down_revision = "0012_case_credited_refs"
branch_labels = None
depends_on = None

_TABLE = "promises_to_pay"


def _columns(table: str) -> list[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return []
    return [c["name"] for c in inspector.get_columns(table)]


def _indexes(table: str) -> list[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return []
    return [i["name"] for i in inspector.get_indexes(table)]


def upgrade() -> None:
    cols = _columns(_TABLE)
    if not cols:
        return

    if "mandate_status" not in cols:
        op.add_column(
            _TABLE,
            sa.Column(
                "mandate_status",
                sa.String(length=20),
                nullable=False,
                server_default="none",
            ),
        )
    if "mandate_token" not in cols:
        op.add_column(_TABLE, sa.Column("mandate_token", sa.String(length=255)))
    if "mandate_authorization_ref" not in cols:
        op.add_column(
            _TABLE, sa.Column("mandate_authorization_ref", sa.String(length=255))
        )
    if "mandate_customer_ref" not in cols:
        op.add_column(_TABLE, sa.Column("mandate_customer_ref", sa.String(length=255)))
    if "mandate_registered_at" not in cols:
        op.add_column(
            _TABLE,
            sa.Column("mandate_registered_at", sa.DateTime(timezone=True)),
        )
    if "mandate_charged_at" not in cols:
        op.add_column(
            _TABLE, sa.Column("mandate_charged_at", sa.DateTime(timezone=True))
        )

    # The debit sweep's index: mandate_status first because it is the selective
    # half — most promises carry no mandate at all.
    if "ix_promises_mandate_due" not in _indexes(_TABLE):
        op.create_index(
            "ix_promises_mandate_due", _TABLE, ["mandate_status", "due_at"]
        )


def downgrade() -> None:
    if "ix_promises_mandate_due" in _indexes(_TABLE):
        op.drop_index("ix_promises_mandate_due", table_name=_TABLE)
    cols = _columns(_TABLE)
    for name in (
        "mandate_charged_at",
        "mandate_registered_at",
        "mandate_customer_ref",
        "mandate_authorization_ref",
        "mandate_token",
        "mandate_status",
    ):
        if name in cols:
            op.drop_column(_TABLE, name)
