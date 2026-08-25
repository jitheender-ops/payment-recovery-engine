"""Indexes for the Layer 6 scheduler sweeps.

Revision ID: 0003_scheduler_indexes
Revises: 0002_revenue_recovery
Create Date: 2026-08-25

The scheduler polls every tick (60s by default) with two filters that no index
covered: due `retry_at` rows on (result='scheduled', scheduled_at <= now) and
write-ahead rows whose outcome never landed on (result='pending',
created_at <= cutoff). On a database with real history each poll was a scan over
every attempt ever written — cheap only while the table is young, and exactly
the kind of thing you find when the tick starts timing out instead of before.

Additive indexes only, inspector-guarded like every revision here: a no-op on a
database `create_all` already brought up to date, applies the delta otherwise.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_scheduler_indexes"
down_revision = "0002_revenue_recovery"
branch_labels = None
depends_on = None


def _has_index(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return name in (ix["name"] for ix in inspector.get_indexes(table))


def upgrade() -> None:
    if not _has_index("retry_attempts", "ix_retry_attempts_scheduled"):
        op.create_index(
            "ix_retry_attempts_scheduled",
            "retry_attempts",
            ["result", "scheduled_at"],
        )
    if not _has_index("retry_attempts", "ix_retry_attempts_pending"):
        op.create_index(
            "ix_retry_attempts_pending",
            "retry_attempts",
            ["result", "created_at"],
        )


def downgrade() -> None:
    if _has_index("retry_attempts", "ix_retry_attempts_pending"):
        op.drop_index("ix_retry_attempts_pending", table_name="retry_attempts")
    if _has_index("retry_attempts", "ix_retry_attempts_scheduled"):
        op.drop_index("ix_retry_attempts_scheduled", table_name="retry_attempts")
