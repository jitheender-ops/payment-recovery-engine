"""The B2B receivables chaser: accounts, staged dunning, dialogs, writeback.

Revision ID: 0010_receivables
Revises: 0009_risk_event_offer
Create Date: 2026-08-30

The tables src/receivables/models.py declares, plus the account link on
recovery_cases. All additive: no existing column changes, no data rewrite.
The account_id column is nullable and backfilled where an account can be
derived (merchant-supplied account_ref in risk_events.meta, else the
canonical customer key) — a case with no derivable buyer stays NULL and
chases exactly as it did before this revision.

Consolidation is the point of the whole migration: one buyer with four
overdue invoices was four independent chases to the same desk. ar_accounts
is the unit B2B collection actually runs on; everything else in this
revision hangs off it.

Inspector-guarded and idempotent like every revision here.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0010_receivables"
down_revision = "0009_risk_event_offer"
branch_labels = None
depends_on = None

JSON_TYPE = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
UUID_TYPE = postgresql.UUID(as_uuid=True).with_variant(sa.Uuid(), "sqlite")


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return name in (c["name"] for c in inspector.get_columns(table))


def _create_receivables_tables() -> None:
    if not _has_table("ar_accounts"):
        op.create_table(
            "ar_accounts",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("account_ref", sa.String(255), nullable=False),
            sa.Column("display_name", sa.String(255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("account_ref", name="uq_ar_account_ref"),
        )
        # No separate lookup index: the UNIQUE constraint on account_ref
        # serves every lookup, and a second index would be pure write cost.

    if not _has_table("ar_contacts"):
        op.create_table(
            "ar_contacts",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("account_id", UUID_TYPE, nullable=False),
            sa.Column("role", sa.String(40), nullable=False),
            sa.Column("name", sa.String(255), nullable=True),
            sa.Column("email", sa.String(255), nullable=False),
            sa.Column("phone", sa.String(20), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_ar_contacts_account_role", "ar_contacts", ["account_id", "role", "active"]
        )

    if not _has_table("ar_contact_log"):
        op.create_table(
            "ar_contact_log",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("account_id", UUID_TYPE, nullable=False),
            sa.Column("stage_level", sa.Integer(), nullable=False),
            sa.Column("case_refs", JSON_TYPE, nullable=False),
            sa.Column("channels", JSON_TYPE, nullable=False),
            sa.Column("sms_copy", sa.Text(), nullable=False),
            sa.Column("email_subject", sa.String(255), nullable=False),
            sa.Column("planned_for", sa.DateTime(timezone=True), nullable=False),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_ar_contact_log_account", "ar_contact_log", ["account_id", "created_at"]
        )

    if not _has_table("payment_plans"):
        op.create_table(
            "payment_plans",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("case_id", UUID_TYPE, nullable=False),
            sa.Column("account_id", UUID_TYPE, nullable=True),
            sa.Column("principal_paise", sa.Integer(), nullable=False),
            sa.Column("settlement_paise", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_payment_plans_case", "payment_plans", ["case_id", "status"])

    if not _has_table("plan_instalments"):
        op.create_table(
            "plan_instalments",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("plan_id", UUID_TYPE, nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("amount_paise", sa.Integer(), nullable=False),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("promise_id", UUID_TYPE, nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_plan_instalments_plan", "plan_instalments", ["plan_id", "seq"])

    if not _has_table("case_disputes"):
        op.create_table(
            "case_disputes",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("case_id", UUID_TYPE, nullable=False),
            sa.Column("account_id", UUID_TYPE, nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("resolution_note", sa.Text(), nullable=True),
            sa.Column(
                "opened_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_case_disputes_status_case", "case_disputes", ["status", "case_id"]
        )

    if not _has_table("merchant_alerts"):
        op.create_table(
            "merchant_alerts",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("event_type", sa.String(40), nullable=False),
            sa.Column("account_ref", sa.String(255), nullable=True),
            sa.Column("case_ref", sa.String(255), nullable=True),
            sa.Column("detail", JSON_TYPE, nullable=False),
            sa.Column("delivered", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_merchant_alerts_delivered", "merchant_alerts", ["delivered", "created_at"]
        )

    if not _has_table("account_tasks"):
        op.create_table(
            "account_tasks",
            sa.Column("id", UUID_TYPE, primary_key=True, nullable=False),
            sa.Column("account_id", UUID_TYPE, nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("detail", JSON_TYPE, nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_account_tasks_status", "account_tasks", ["status", "created_at"])


def upgrade() -> None:
    _create_receivables_tables()

    # The merchant's buyer-organisation code on risk_events — the explicit
    # consolidation key, read back days after arrival.
    if not _has_column("risk_events", "account_ref"):
        with op.batch_alter_table("risk_events") as batch_op:
            batch_op.add_column(
                sa.Column("account_ref", sa.String(255), nullable=True)
            )

    # The account link on recovery_cases: nullable UUID, additive. Batch
    # mode for SQLite (no ALTER), same as every revision here.
    if not _has_column("recovery_cases", "account_id"):
        with op.batch_alter_table("recovery_cases") as batch_op:
            batch_op.add_column(sa.Column("account_id", UUID_TYPE, nullable=True))
        op.create_index(
            "ix_recovery_cases_account", "recovery_cases", ["account_id", "state"]
        )

    # Backfill: link existing invoice cases to accounts. Two sources, in
    # precedence order — the merchant's explicit account_ref in
    # risk_events.meta, else the canonical customer key the case already
    # carries. Rows that derive to nothing stay NULL and chase per-case,
    # exactly as before this revision.
    bind = op.get_bind()
    accounts_created = 0
    cases_linked = 0
    rows = bind.execute(
        sa.text(
            "SELECT rc.id, rc.customer_id, rc.risk_type FROM recovery_cases rc "
            "WHERE rc.account_id IS NULL AND rc.customer_id IS NOT NULL"
        )
    ).fetchall()
    for case_id, customer_id, _risk_type in rows:
        account_ref = None
        # Explicit merchant ref first: the dedicated column on the newest
        # risk event for this case's subject, when the merchant supplied one.
        ref_row = bind.execute(
            sa.text(
                "SELECT re.account_ref FROM risk_events re "
                "JOIN recovery_cases rc2 ON rc2.risk_type = re.risk_type "
                "  AND rc2.subject_ref = re.reference_id "
                "WHERE rc2.id = :cid AND re.account_ref IS NOT NULL "
                "ORDER BY re.received_at DESC LIMIT 1"
            ),
            {"cid": str(case_id)},
        ).fetchone()
        if ref_row is not None and isinstance(ref_row[0], str) and ref_row[0].strip():
            account_ref = f"ref:{ref_row[0].strip()}"
        if account_ref is None:
            # Derived from the canonical key — same identity the ledger and
            # opt-out already run on.
            key = customer_id
            if key.startswith(("email:", "phone:", "id:")):
                account_ref = f"derived:{key}"
            elif "@" in (key or ""):
                account_ref = f"derived:email:{key.strip().lower()}"
            else:
                account_ref = f"derived:id:{(key or '').strip()}"
        if account_ref is None:
            continue

        acct = bind.execute(
            sa.text("SELECT id FROM ar_accounts WHERE account_ref = :ref"),
            {"ref": account_ref},
        ).fetchone()
        if acct is None:
            import uuid as _uuid

            new_id = _uuid.uuid4()
            bind.execute(
                sa.text(
                    "INSERT INTO ar_accounts (id, account_ref) VALUES (:id, :ref)"
                ),
                {"id": str(new_id), "ref": account_ref},
            )
            accounts_created += 1
            account_id = new_id
        else:
            account_id = acct[0]
        bind.execute(
            sa.text("UPDATE recovery_cases SET account_id = :aid WHERE id = :cid"),
            {"aid": str(account_id), "cid": str(case_id)},
        )
        cases_linked += 1

    if cases_linked:
        print(  # noqa: T201 — migrations here print their summary
            f"Receivables backfill: {accounts_created} account(s) created, "
            f"{cases_linked} case(s) linked"
        )


def downgrade() -> None:
    if _has_column("recovery_cases", "account_id"):
        op.drop_index(
            "ix_recovery_cases_account", table_name="recovery_cases"
        )
        with op.batch_alter_table("recovery_cases") as batch_op:
            batch_op.drop_column("account_id")
    if _has_column("risk_events", "account_ref"):
        with op.batch_alter_table("risk_events") as batch_op:
            batch_op.drop_column("account_ref")
    for table in (
        "account_tasks",
        "merchant_alerts",
        "case_disputes",
        "plan_instalments",
        "payment_plans",
        "ar_contact_log",
        "ar_contacts",
        "ar_accounts",
    ):
        if _has_table(table):
            op.drop_table(table)
