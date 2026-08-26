"""Hardening round 2: window anchors, reconcile retries, scheduler heartbeat.

Revision ID: 0004_hardening_round2
Revises: 0003_scheduler_indexes
Create Date: 2026-08-26

Three additive changes, each inspector-guarded like every revision here:

1. retry_ledger.retries_window_started_at / nudges_window_started_at — anchor
   the rate-limit windows at their first contact. The old reset rule keyed off
   last_retry_at alone, which let contacts spaced just inside the window keep
   the tally alive indefinitely.

2. webhook_events.processing_attempts — the reconciler now retries transient
   failures instead of consuming the event on the first exception; this column
   counts the attempts so a deterministically-broken payload still gives up.

3. scheduler_heartbeat — a single-row dead-man's-switch the scheduler stamps
   every tick, so a silently-dead loop is distinguishable from an idle one.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_hardening_round2"
down_revision = "0003_scheduler_indexes"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in (c["name"] for c in inspector.get_columns(table))


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_column("retry_ledger", "retries_window_started_at"):
        op.add_column(
            "retry_ledger",
            sa.Column("retries_window_started_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("retry_ledger", "nudges_window_started_at"):
        op.add_column(
            "retry_ledger",
            sa.Column("nudges_window_started_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("webhook_events", "processing_attempts"):
        op.add_column(
            "webhook_events",
            sa.Column("processing_attempts", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_table("scheduler_heartbeat"):
        op.create_table(
            "scheduler_heartbeat",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "last_tick_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.Column("last_tick_counts", sa.JSON(), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    if _has_table("scheduler_heartbeat"):
        op.drop_table("scheduler_heartbeat")
    if _has_column("webhook_events", "processing_attempts"):
        op.drop_column("webhook_events", "processing_attempts")
    if _has_column("retry_ledger", "nudges_window_started_at"):
        op.drop_column("retry_ledger", "nudges_window_started_at")
    if _has_column("retry_ledger", "retries_window_started_at"):
        op.drop_column("retry_ledger", "retries_window_started_at")
