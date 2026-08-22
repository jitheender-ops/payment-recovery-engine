"""Widen the schema past the payment rail: promises, audit trail, scheduling.

Revision ID: 0002_revenue_recovery
Revises: 0001_recovery_cases
Create Date: 2026-08-22

`recovery_cases.risk_type` has named five sources of revenue at risk since 0001,
but only one of them could actually run: `retry_attempts.payment_failure_id` and
`.payment_id` were NOT NULL, so an abandoned cart, an overdue invoice, a halted
subscription and a failed mandate could not record a single attempt. The
nullability changes below are the load-bearing part of this migration — the two
new tables are additive, that one is what unblocks four fifths of the case table.

Same guard style as 0001: every step checks the inspector first, so this is a
no-op on a database `create_all` already brought up to date and applies the delta
on one that predates it. A migration that crashes half the time is a migration
nobody runs.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_revenue_recovery"
down_revision = "0001_recovery_cases"
branch_labels = None
depends_on = None

# Same variants as 0000. Postgres is what deploys; SQLite is what CI can reach
# without a service container, and CI is the only place the empty-database path
# gets tested — which is the path that shipped broken.
JSON_TYPE = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
UUID_TYPE = postgresql.UUID(as_uuid=True).with_variant(sa.Uuid(), "sqlite")


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _column(table: str, name: str) -> dict[str, object] | None:
    """The column's reflected definition, or None if table or column is absent."""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return None
    for col in inspector.get_columns(table):
        if col["name"] == name:
            return dict(col)
    return None


def _needs_column(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return _column(table, name) is None


def upgrade() -> None:
    # ── The unblocker ────────────────────────────────────────────────────
    # Dropping NOT NULL, not adding it, so this is safe on populated data: every
    # existing row keeps its payment id, and only the new risk types write NULL.
    to_relax = [
        (name, coltype)
        for name, coltype in (
            ("payment_failure_id", UUID_TYPE),
            ("payment_id", sa.String(255)),
        )
        if (col := _column("retry_attempts", name)) is not None
        and not col.get("nullable", True)
    ]
    if to_relax:
        # batch_alter_table rather than a bare alter_column: SQLite has no
        # ALTER COLUMN at all, so alembic recreates the table there and emits the
        # ordinary ALTER on Postgres. A bare alter_column made the whole chain
        # unrunnable on the only database CI can reach without a service
        # container — which is why the empty-database path shipped untested.
        with op.batch_alter_table("retry_attempts") as batch:
            for name, coltype in to_relax:
                batch.alter_column(name, existing_type=coltype, nullable=True)

    # ── Outreach channel, for per-channel contact limits ─────────────────
    if _needs_column("retry_attempts", "channel"):
        op.add_column("retry_attempts", sa.Column("channel", sa.String(20), nullable=True))
    if _needs_column("retry_attempts", "language"):
        op.add_column("retry_attempts", sa.Column("language", sa.String(20), nullable=True))

    # ── Case scheduling ──────────────────────────────────────────────────
    # Both nullable, and NULL is the pre-migration behaviour: no due date, no
    # wait. Nothing that worked before this migration changes because of it.
    if _needs_column("recovery_cases", "due_at"):
        op.add_column(
            "recovery_cases", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True)
        )
    if _needs_column("recovery_cases", "next_action_at"):
        op.add_column(
            "recovery_cases",
            sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_recovery_cases_due", "recovery_cases", ["state", "next_action_at"]
        )

    # ── Promises to pay ──────────────────────────────────────────────────
    if not _has_table("promises_to_pay"):
        op.create_table(
            "promises_to_pay",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("recovery_case_id", UUID_TYPE, nullable=False),
            sa.Column("customer_id", sa.String(255), nullable=True),
            sa.Column("amount_promised", sa.Integer(), nullable=False),
            sa.Column(
                "promised_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("channel", sa.String(20), nullable=True),
            sa.Column("language", sa.String(20), nullable=True),
            sa.Column("source_ref", sa.String(255), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_ref", sa.String(255), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_promises_to_pay_recovery_case_id", "promises_to_pay", ["recovery_case_id"]
        )
        op.create_index("ix_promises_to_pay_customer_id", "promises_to_pay", ["customer_id"])
        # The tracker's sweep: pending promises whose date has passed.
        op.create_index("ix_promises_status_due", "promises_to_pay", ["status", "due_at"])

    # ── Case audit trail ─────────────────────────────────────────────────
    if not _has_table("case_events"):
        op.create_table(
            "case_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("recovery_case_id", UUID_TYPE, nullable=False),
            sa.Column("event_type", sa.String(40), nullable=False),
            sa.Column("actor", sa.String(40), nullable=False, server_default="system"),
            sa.Column("detail", JSON_TYPE, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_case_events_case", "case_events", ["recovery_case_id", "id"])
        op.create_index(
            "ix_case_events_type_created", "case_events", ["event_type", "created_at"]
        )


def downgrade() -> None:
    # The audit trail and the promise ledger go first: both are records of what
    # was done to customers, so dropping them is the destructive half of this
    # and it should be the part someone has to read before running it.
    if _has_table("case_events"):
        op.drop_table("case_events")
    if _has_table("promises_to_pay"):
        op.drop_table("promises_to_pay")

    for column in ("next_action_at", "due_at"):
        if _column("recovery_cases", column) is not None:
            if column == "next_action_at":
                op.drop_index("ix_recovery_cases_due", table_name="recovery_cases")
            op.drop_column("recovery_cases", column)

    for column in ("language", "channel"):
        if _column("retry_attempts", column) is not None:
            op.drop_column("retry_attempts", column)

    # Restoring NOT NULL fails outright if any non-payment attempt was written.
    # Left as a hard error rather than deleting those rows: they are attempts
    # against real money, and a downgrade must not quietly destroy them.
    to_restore = [
        (name, coltype)
        for name, coltype in (
            ("payment_id", sa.String(255)),
            ("payment_failure_id", UUID_TYPE),
        )
        if (col := _column("retry_attempts", name)) is not None
        and col.get("nullable", False)
    ]
    if to_restore:
        # Batch mode for the same reason as upgrade(): SQLite has no ALTER COLUMN.
        with op.batch_alter_table("retry_attempts") as batch:
            for name, coltype in to_restore:
                batch.alter_column(name, existing_type=coltype, nullable=False)
