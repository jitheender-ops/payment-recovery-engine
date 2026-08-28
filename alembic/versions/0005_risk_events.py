"""Risk events: the merchant-pushed half of revenue recovery.

Revision ID: 0005_risk_events
Revises: 0004_hardening_round2
Create Date: 2026-08-27

Two changes, each inspector-guarded like every revision here:

1. risk_events — the durable store for the four chaser-driven risk types
   (abandoned carts, halted subscriptions, overdue invoices, failed mandate
   debits). A card decline announces itself through Razorpay's webhook; these
   only exist in the merchant's own systems, so the merchant POSTs them to
   /risks. Mirrors webhook_events' discipline: committed before the
   background task runs, deduped on event_id, re-armed on transient
   processing failure up to the shared cap.

2. scheduler_heartbeat.updated_at NOT NULL — 0004 created the column
   nullable while the model declares it non-optional (server_default and
   onupdate both guarantee a value). Aligns the chain with the ORM.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005_risk_events"
down_revision = "0004_hardening_round2"
branch_labels = None
depends_on = None

# JSONB on Postgres, JSON everywhere else — the test harness runs SQLite, and
# the chain is exercised on it (see scripts/check_migrations.py).
JSON_TYPE = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
UUID_TYPE = postgresql.UUID(as_uuid=True).with_variant(sa.Uuid(), "sqlite")


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _nullable(table: str, column: str) -> bool | None:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return None
    for c in inspector.get_columns(table):
        if c["name"] == column:
            return bool(c["nullable"])
    return None


def upgrade() -> None:
    if not _has_table("risk_events"):
        op.create_table(
            "risk_events",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("event_id", sa.String(255), nullable=False),
            sa.Column("risk_type", sa.String(40), nullable=False),
            sa.Column("reference_id", sa.String(255), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("currency", sa.String(10), nullable=False, server_default="INR"),
            sa.Column("customer_id", sa.String(255), nullable=True),
            sa.Column("customer_email", sa.String(255), nullable=True),
            sa.Column("customer_contact", sa.String(20), nullable=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("meta", JSON_TYPE, nullable=False),
            sa.Column("payload", JSON_TYPE, nullable=False),
            sa.Column(
                "received_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("processed", sa.Boolean(), nullable=False),
            sa.Column("processing_error", sa.Text(), nullable=True),
            sa.Column(
                "processing_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            # Inline, not op.create_unique_constraint: SQLite has no ALTER for
            # constraints, and the chain is exercised on it.
            sa.UniqueConstraint("event_id", name="uq_risk_event_id"),
        )
        op.create_index(
            "ix_risk_events_type_received", "risk_events", ["risk_type", "received_at"]
        )
        op.create_index(
            "ix_risk_events_reference", "risk_events", ["risk_type", "reference_id"]
        )

    # The single heartbeat row always carries a value (server_default fills it
    # on insert, onupdate on every stamp), so tightening is safe. Batch mode:
    # SQLite has no ALTER for constraints, and the chain is exercised on it.
    if _nullable("scheduler_heartbeat", "updated_at"):
        with op.batch_alter_table("scheduler_heartbeat") as batch_op:
            batch_op.alter_column(
                "updated_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
            )


def downgrade() -> None:
    if _nullable("scheduler_heartbeat", "updated_at") is False:
        with op.batch_alter_table("scheduler_heartbeat") as batch_op:
            batch_op.alter_column(
                "updated_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=True,
            )
    if _has_table("risk_events"):
        op.drop_index("ix_risk_events_reference", table_name="risk_events")
        op.drop_index("ix_risk_events_type_received", table_name="risk_events")
        op.drop_table("risk_events")
