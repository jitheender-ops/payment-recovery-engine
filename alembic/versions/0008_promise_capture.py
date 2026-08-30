"""Promise-capture columns on promises_to_pay.

Revision ID: 0008_promise_capture
Revises: 0007_audit_hash_chain
Create Date: 2026-08-30

The promise LEDGER was already best-in-class (own table per promise, clock-
driven breakage, per-promise audit); what was missing was capture — no
production entry point ever called record_promise, and the workflow around
a promise (confirm it, remind before the date, keep honest metrics) had
nowhere to live. This revision adds the columns that capture and workflow
need, all nullable and additive:

* is_partial, confidence, condition_note, promised_rail — promise QUALITY.
  A promise for less than the dues, made tentatively vs explicitly, with a
  stated condition ("after salary credit") and an intended rail, are the
  fields research (Oracle Banking Collections, Monk, AInora) says kept-rate
  analysis segments on. condition_note rides through sanitize_free_text
  at write time, never verbatim into a prompt.
* reminded_at — the pre-due reminder sweep's idempotency marker. One
  reminder per promise, ever; the sweep reads it exactly the way the
  expire sweep reads status + due_at.
* kept_late_days — the Monk "delta": days between due_at and the payment
  that kept it. The kept-rate metrics are dishonest without it, because a
  promise kept five days late is not the same product as one kept on time.

Nullable, no backfill, no index changes: capture queries go by
recovery_case_id (already indexed) and the reminder sweep reads
(status='pending', reminded_at IS NULL) ordered by due_at — small on top
of the existing ix_promises_status_due, and the sweep is batch-bounded.

Inspector-guarded and idempotent like every revision here.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_promise_capture"
down_revision = "0007_audit_hash_chain"
branch_labels = None
depends_on = None


def _has_column(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return name in (c["name"] for c in inspector.get_columns(table))


def upgrade() -> None:
    cols = (
        ("is_partial", sa.Column("is_partial", sa.Boolean(), nullable=True)),
        ("confidence", sa.Column("confidence", sa.String(length=20), nullable=True)),
        ("condition_note", sa.Column("condition_note", sa.Text(), nullable=True)),
        ("promised_rail", sa.Column("promised_rail", sa.String(length=20), nullable=True)),
        ("reminded_at", sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True)),
        ("kept_late_days", sa.Column("kept_late_days", sa.Integer(), nullable=True)),
    )
    for name, col in cols:
        if not _has_column("promises_to_pay", name):
            op.add_column("promises_to_pay", col)


def downgrade() -> None:
    for name in ("kept_late_days", "reminded_at", "promised_rail",
                 "condition_note", "confidence", "is_partial"):
        if _has_column("promises_to_pay", name):
            op.drop_column("promises_to_pay", name)
