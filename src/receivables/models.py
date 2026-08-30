"""The models behind the B2B receivables chaser.

Every table here joins the existing schema by UUID columns and indexes, not
by FOREIGN KEYS — the convention recovery_cases itself uses (RetryAttempt
references its case by a bare nullable UUID). This keeps the receivables
layer loadable and testable on its own, and keeps alembic autogenerate from
wiring cascade rules into tables the payment rail must never touch.

PII boundary: account display names and the merchant's own references are
the merchant's data and may surface on the gated console (the PII-free rule
covers customers, and a business's billing name is what the merchant typed).
Contact emails and phones are customer-adjacent PII: they live here for the
sender's use and must never reach a console query.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

# Re-exported for tests and the alembic migration (integration phase): merely
# importing this module registers every table below on the shared metadata.
from src.models import RecoveryCase  # noqa: F401  — register on the shared Base


class ArAccount(Base):
    """One paying organisation — the unit B2B collection actually runs on.

    A single buyer with four overdue invoices is one account with one AR
    balance, not four independent chases to the same person at the same desk.
    `account_ref` is the merchant's own identifier for the buyer (their ERP
    customer code), so consolidation survives re-sends and stays PII-free in
    logs. Events that carry no account_ref fall back to the customer_id the
    case layer already canonicalised.
    """

    __tablename__ = "ar_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The merchant's namespace. UNIQUE: the second event for the same buyer
    # must find this row, not open a second account (a split account is a
    # split contact budget and a split AR balance — the exact bug this table
    # exists to prevent).
    account_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # Inline-named like every UQ in this schema: SQLite renders a
        # table-level unnamed unique differently than Postgres, and the
        # migration checker diffs them by name.
        UniqueConstraint("account_ref", name="uq_ar_account_ref"),
    )


class ArContact(Base):
    """A person at the buyer's desk, in the order a dunning ladder escalates.

    B2B collection reaches people by role: the AP clerk who keys the payment,
    the finance manager who approves it, the escalation contact when both
    have stopped answering. The ladder picks the role; this table says who
    currently holds it. `active=False` is how a buyer says "that person left"
    without losing the history.
    """

    __tablename__ = "ar_contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # "ap_clerk" | "finance_manager" | "escalation" — the ladder's vocabulary.
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_ar_contacts_account_role", "account_id", "role", "active"),
    )


class ArContactLog(Base):
    """One consolidated account-level contact, for the audit trail.

    The chaser sends ONE communication per account per rung, carrying every
    open invoice on that account (the consolidation rule). That fact is not
    reconstructable from retry_attempts, which is per-case: this row is the
    record that a single email covered INV-204 and INV-205 together.
    """

    __tablename__ = "ar_contact_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The ladder stage that fired (ladder.LadderStage.level).
    stage_level: Mapped[int] = mapped_column(Integer, nullable=False)
    # The cases this one communication covered, as the merchant's own refs.
    case_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    channels: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    sms_copy: Mapped[str] = mapped_column(Text, nullable=False)
    email_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    planned_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_ar_contact_log_account", "account_id", "created_at"),
    )


class PaymentPlan(Base):
    """A customer-requested instalment plan over one case's outstanding money.

    Deliberately thin: each instalment is a PromiseToPay (created via the
    existing record_promise), so the pause machinery, the promise audit
    events and the expire_promises sweep that breaks a missed instalment are
    all REUSED rather than rebuilt. This row exists to group the instalments
    and to hold the plan-level verdicts (completed / defaulted) a merchant
    asks for. One plan per open case at a time — a second request replaces
    nothing, it is refused.
    """

    __tablename__ = "payment_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    principal_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    # A reduced settlement the merchant approved, when this plan is one. The
    # delta is recorded for honest reporting; it is never folded into
    # amount_at_risk, which keeps the "at risk" figure the money truthfully owed.
    settlement_paise: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )  # "active" | "completed" | "defaulted"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_payment_plans_case", "case_id", "status"),
    )


class PlanInstalment(Base):
    """One scheduled payment inside a plan.

    Status is derived, not stored: an instalment is exactly the state of its
    promise (kept -> paid, pending-and-past-due -> missed, else scheduled).
    A status column here would be a second copy of the promise state that
    only drifts — the promise is the record because the pause/break machinery
    already runs on it.
    """

    __tablename__ = "plan_instalments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    promise_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_plan_instalments_plan", "plan_id", "seq"),
    )


class CaseDispute(Base):
    """The customer said "this invoice is wrong" — freeze the chase, tell the merchant.

    A dispute is the one customer answer that must make the engine QUIETER
    while making the merchant LOUDER: the case is excluded from every
    consolidated contact until a human resolves it. Resolution is either
    upheld (the invoice was wrong — the case closes abandoned, nothing is
    recovered) or rejected (the invoice stands — the chase resumes where it
    left off). The reason text is the customer's own words, bounded at write
    time; it is evidence, so it is stored verbatim.
    """

    __tablename__ = "case_disputes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # "open" | "upheld" | "rejected" — see the module docstring in disputes.py.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    resolution_note: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The open-dispute check runs on every plan: selective half first.
        Index("ix_case_disputes_status_case", "status", "case_id"),
    )


class MerchantAlert(Base):
    """A fact the merchant must hear about without watching a console.

    Promise made/broken, dispute opened, plan requested/defaulted, chase
    exhausted, external payment recorded. This table is the queue; the
    outbound webhook dispatcher (alerts.py) drains it. `event_type` is a
    closed vocabulary so a merchant's automation can branch on it safely.
    """

    __tablename__ = "merchant_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    account_ref: Mapped[str | None] = mapped_column(String(255))
    case_ref: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_merchant_alerts_delivered", "delivered", "created_at"),
    )


class AccountTask(Base):
    """A piece of human work the ladder decided not to automate.

    Stage 3 of the ladder asks for a phone call, not another email — a call
    is the highest-converting B2B collection touch and cannot be sent
    programmatically. The row is the queue entry for the merchant's team; it
    is deliberately NOT counted against the customer's contact budget, which
    stays exactly what the frozen policy promised.
    """

    __tablename__ = "account_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # "call" today; the vocabulary is closed per kind of human work.
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_account_tasks_status", "status", "created_at"),
    )
