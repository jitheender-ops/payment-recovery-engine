"""Add recovery_cases plus the attribution and consent columns.

Revision ID: 0001_recovery_cases
Revises: None
Create Date: 2026-08-22

The trap this exists to close: schema normally comes from `init_db()`, which is
`create_all(checkfirst=True)`. That creates missing *tables* and silently ignores
missing *columns*. So on any database that predates this change, `recovery_cases`
appears but `retry_attempts.recovery_case_id`, `retry_attempts.external_ref` and
the three `retry_ledger` consent columns do not — and the first write to the money
path fails with UndefinedColumn.

Every step is guarded by an inspector check, so `alembic upgrade head` is safe on
a fresh `create_all` database (all no-ops) and on a pre-case one (applies the
delta). A migration that crashes half the time is a migration nobody runs.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_recovery_cases"
# 0000 creates the tables this migration alters. Before it existed, the chain
# had no baseline and `upgrade head` on an empty database produced a schema with
# recovery_cases and no retry_attempts.
down_revision = "0000_initial_schema"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _needs_column(table: str, column: str) -> bool:
    """True only when the table is there and the column is not.

    Absent table means there is nothing to alter — `create_all` has not run yet,
    and adding a column to a table that does not exist is a crash, not a no-op.
    """
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column not in {c["name"] for c in inspector.get_columns(table)}


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_table("recovery_cases"):
        op.create_table(
            "recovery_cases",
            sa.Column(
                "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
            ),
            sa.Column("risk_type", sa.String(40), nullable=False),
            sa.Column("subject_ref", sa.String(255), nullable=False),
            sa.Column("customer_id", sa.String(255), nullable=True),
            sa.Column("amount_at_risk", sa.Integer(), nullable=False),
            sa.Column("currency", sa.String(10), nullable=True, server_default="INR"),
            sa.Column(
                "amount_recovered", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("recovered_ref", sa.String(255), nullable=True),
            sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "recovered_via_attempt_id", postgresql.UUID(as_uuid=True), nullable=True
            ),
            sa.Column("state", sa.String(20), nullable=False, server_default="open"),
            sa.Column("close_reason", sa.Text(), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "attempts_used", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("max_attempts", sa.Integer(), nullable=False),
            sa.Column(
                "escalation_level", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("batch_id", sa.String(100), nullable=True),
            sa.Column(
                "opened_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            ),
            # Re-ingesting one failure must find the existing case. Two cases for
            # one payment would double both the attempt budget and the recovered
            # amount, which is the one thing the headline number cannot survive.
            sa.UniqueConstraint(
                "risk_type", "subject_ref", name="uq_recovery_case_subject"
            ),
        )
        op.create_index("ix_recovery_cases_customer_id", "recovery_cases", ["customer_id"])
        op.create_index("ix_recovery_cases_batch_id", "recovery_cases", ["batch_id"])
        op.create_index("ix_recovery_cases_state", "recovery_cases", ["state"])
        op.create_index(
            "ix_recovery_cases_type_state", "recovery_cases", ["risk_type", "state"]
        )

    # The attribution join keys. Nullable because rows written before this
    # migration have no case to point at and no link we can reconstruct.
    if _needs_column("retry_attempts", "recovery_case_id"):
        op.add_column(
            "retry_attempts",
            sa.Column(
                "recovery_case_id", postgresql.UUID(as_uuid=True), nullable=True
            ),
        )
        op.create_index(
            "ix_retry_attempts_case", "retry_attempts", ["recovery_case_id"]
        )
    if _needs_column("retry_attempts", "external_ref"):
        op.add_column(
            "retry_attempts", sa.Column("external_ref", sa.String(255), nullable=True)
        )
        op.create_index(
            "ix_retry_attempts_external_ref", "retry_attempts", ["external_ref"]
        )

    # Consent. NOT NULL on a populated table needs the server_default, and
    # "granted" is the right backfill: everyone already in the ledger was
    # contactable under the rules in force when their row was written.
    if _needs_column("retry_ledger", "consent_status"):
        op.add_column(
            "retry_ledger",
            sa.Column(
                "consent_status",
                sa.String(20),
                nullable=False,
                server_default="granted",
            ),
        )
    if _needs_column("retry_ledger", "opted_out_at"):
        op.add_column(
            "retry_ledger",
            sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=True),
        )
    if _needs_column("retry_ledger", "updated_at"):
        op.add_column(
            "retry_ledger",
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    for table, column in (
        ("retry_ledger", "updated_at"),
        ("retry_ledger", "opted_out_at"),
        ("retry_ledger", "consent_status"),
        ("retry_attempts", "external_ref"),
        ("retry_attempts", "recovery_case_id"),
    ):
        if _has_column(table, column):
            op.drop_column(table, column)
    if _has_table("recovery_cases"):
        # Dropping this loses every recovered-amount attribution. Kept only
        # because a one-way migration is not a migration.
        op.drop_table("recovery_cases")
