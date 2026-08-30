"""Hash-chain columns for the case_events audit trail.

Revision ID: 0007_audit_hash_chain
Revises: 0006_canonical_customer_key
Create Date: 2026-08-30

case_events is already append-only by convention (never updated, never
deleted — see the model's own docstring), but nothing made tampering
detectable: an operator with database access could edit a row's `detail` and
nothing downstream would notice. Two nullable columns, filled in by
src/audit_chain.py rather than by this migration or by cases.log_event()
itself — chaining requires knowing the previous row's hash, and reading it
before every insert would add a query to a function deliberately kept
synchronous and I/O-free (see that function's own docstring: the audit row
must land in the same transaction as the change it describes, with no extra
round-trip in between).

Nullable and backfill-free on purpose: existing rows stay NULL until the
stamping pass runs, which is safe — src/audit_chain.py treats NULL as "not
yet chained", never as "tampered".

Additive, inspector-guarded, idempotent like every revision here.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007_audit_hash_chain"
down_revision = "0006_canonical_customer_key"
branch_labels = None
depends_on = None


def _has_column(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return name in (c["name"] for c in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("case_events", "event_hash"):
        op.add_column(
            "case_events", sa.Column("event_hash", sa.String(length=64), nullable=True)
        )
    if not _has_column("case_events", "prev_event_hash"):
        op.add_column(
            "case_events", sa.Column("prev_event_hash", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    if _has_column("case_events", "prev_event_hash"):
        op.drop_column("case_events", "prev_event_hash")
    if _has_column("case_events", "event_hash"):
        op.drop_column("case_events", "event_hash")
