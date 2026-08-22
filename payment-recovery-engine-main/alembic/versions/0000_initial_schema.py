"""Baseline schema — the tables every later migration assumes already exist.

Revision ID: 0000_initial_schema
Revises: None
Create Date: 2026-08-22

Why this exists: 0001 and 0002 only CREATE the tables they introduce and ALTER
the rest. That is correct for the machine they were written on, where the schema
had already been built by `init_db()`'s create_all — but it left the chain with
no way to build a database from nothing. `alembic upgrade head` against an empty
Postgres ran to completion, reported success, and produced a database with
`recovery_cases` and no `retry_attempts`.

That was invisible locally (a dev box always has a create_all schema) and fatal
on first deploy, which is the one place the database is always empty. It
surfaced as the scheduler crash-looping on:

    UndefinedTableError: relation "retry_attempts" does not exist

while /health cheerfully returned 200.

The tables are created in their PRE-0001 shape on purpose — `retry_ledger`
without the consent columns, `retry_attempts` with its payment columns still
NOT NULL. Each later migration then describes a real transition, and a database
built from scratch converges on exactly the same schema as one that grew
through create_all. Every step is guarded, so this is a no-op on any database
that already has these tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0000_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

# JSONB on Postgres, JSON everywhere else — the test harness runs SQLite, and
# CI applies this chain over a SQLite database to catch model/migration drift.
JSON_TYPE = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
UUID_TYPE = postgresql.UUID(as_uuid=True).with_variant(sa.Uuid(), "sqlite")


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("webhook_events"):
        op.create_table(
            "webhook_events",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("razorpay_event_id", sa.String(255), nullable=False),
            sa.Column("event_type", sa.String(100), nullable=False),
            sa.Column("payload", JSON_TYPE, nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("processed", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("processing_error", sa.Text(), nullable=True),
        )
        op.create_index(
            "ix_webhook_events_razorpay_event_id", "webhook_events", ["razorpay_event_id"]
        )
        op.create_index(
            "ix_webhook_events_type_received", "webhook_events", ["event_type", "received_at"]
        )

    if not _has_table("processed_events"):
        op.create_table(
            "processed_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("razorpay_event_id", sa.String(255), nullable=False),
            sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            # The webhook dedup guarantee. The fast-path SELECT cannot close the
            # window between "not found" and "inserted"; this constraint is what
            # actually decides a race between two deliveries.
            sa.UniqueConstraint("razorpay_event_id", name="uq_processed_event_id"),
        )

    if not _has_table("payment_failures"):
        op.create_table(
            "payment_failures",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("payment_id", sa.String(255), nullable=False),
            sa.Column("order_id", sa.String(255), nullable=True),
            sa.Column("amount", sa.Integer(), nullable=False),  # paise
            sa.Column("currency", sa.String(10), server_default="INR", nullable=False),
            sa.Column("method", sa.String(50), nullable=False),
            sa.Column("bank", sa.String(100), nullable=True),
            sa.Column("wallet", sa.String(100), nullable=True),
            sa.Column("vpa", sa.String(255), nullable=True),
            sa.Column("card_network", sa.String(50), nullable=True),
            sa.Column("card_type", sa.String(20), nullable=True),
            sa.Column("card_issuer", sa.String(100), nullable=True),
            sa.Column("error_code", sa.String(100), nullable=False),
            sa.Column("error_description", sa.Text(), nullable=True),
            sa.Column("error_source", sa.String(50), nullable=True),
            sa.Column("error_step", sa.String(100), nullable=True),
            sa.Column("error_reason", sa.String(100), nullable=True),
            sa.Column("failure_class", sa.String(50), nullable=False),
            sa.Column("is_retryable", sa.Boolean(), nullable=False),
            sa.Column("customer_email", sa.String(255), nullable=True),
            sa.Column("customer_contact", sa.String(20), nullable=True),
            sa.Column("webhook_event_id", UUID_TYPE, nullable=False),
            sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_payment_failures_payment_id", "payment_failures", ["payment_id"])
        op.create_index("ix_payment_failures_order_id", "payment_failures", ["order_id"])
        op.create_index("ix_payment_failures_class", "payment_failures", ["failure_class"])
        op.create_index(
            "ix_payment_failures_method_bank", "payment_failures", ["method", "bank"]
        )
        op.create_index("ix_payment_failures_failed_at", "payment_failures", ["failed_at"])

    if not _has_table("retry_attempts"):
        op.create_table(
            "retry_attempts",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            # NOT NULL here is the historical shape. 0002 drops it — that is the
            # constraint which confined this table to the payment rail.
            sa.Column("payment_failure_id", UUID_TYPE, nullable=False),
            sa.Column("payment_id", sa.String(255), nullable=False),
            # The double-charge guard. razorpay-python has no idempotency header,
            # so this constraint is where the guarantee actually lives.
            sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("action_type", sa.String(50), nullable=False),
            sa.Column("target_rail", sa.String(50), nullable=True),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("agent_reasoning", sa.Text(), nullable=True),
            sa.Column("agent_type", sa.String(20), server_default="llm", nullable=False),
            sa.Column("agent_confidence", sa.Float(), nullable=True),
            sa.Column("guardrail_passed", sa.Boolean(), nullable=False),
            sa.Column("guardrail_rejection_reason", sa.Text(), nullable=True),
            sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("result", sa.String(50), nullable=True),
            sa.Column("result_details", JSON_TYPE, nullable=True),
            sa.Column("nudge_message", sa.Text(), nullable=True),
            sa.Column("nudge_sent", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_retry_attempts_payment_id", "retry_attempts", ["payment_id"])
        op.create_index(
            "ix_retry_attempts_payment_failure", "retry_attempts", ["payment_failure_id"]
        )
        op.create_index("ix_retry_attempts_created_at", "retry_attempts", ["created_at"])

    if not _has_table("retry_ledger"):
        op.create_table(
            "retry_ledger",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            # One row per customer is the invariant the rate limits rest on, and
            # it is enforced by the unique INDEX below, not a table constraint —
            # the model says `unique=True, index=True`, which SQLAlchemy renders
            # as a single unique index. Declaring both here produced an extra
            # unnamed UNIQUE that a create_all database does not have.
            sa.Column("customer_id", sa.String(255), nullable=False),
            sa.Column("total_retries_24h", sa.Integer(), server_default="0", nullable=False),
            sa.Column("total_nudges_24h", sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_nudge_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
            # consent_status / opted_out_at / updated_at arrive in 0001.
        )
        op.create_index("ix_retry_ledger_customer_id", "retry_ledger", ["customer_id"])


def downgrade() -> None:
    # Reverse dependency order. This drops the entire event store and every
    # payment record with it — there is no scenario where running it on a real
    # database is the right move, and it exists only because a one-way
    # migration is not a migration.
    for table in (
        "retry_ledger",
        "retry_attempts",
        "payment_failures",
        "processed_events",
        "webhook_events",
    ):
        if _has_table(table):
            op.drop_table(table)
