"""
SQLAlchemy ORM models for the payment recovery engine.

Tables:
    - webhook_events:    Append-only event store (fully replayable)
    - payment_failures:  Enriched failure records with classification
    - recovery_cases:    One row per unit of revenue at risk, any source
    - retry_attempts:    Every retry attempt with idempotency key and outcome
    - retry_ledger:      Per-customer running tally for rate limiting
    - processed_events:  Idempotency table for webhook deduplication
    - promises_to_pay:   Commitments to pay by a date, and whether they held
    - case_events:       Append-only audit trail of everything done to a case

`payment_failures` is the payment-rail-specific detail record; `recovery_cases`
is the source-agnostic one above it. A failed card charge, an abandoned cart, a
halted subscription and an overdue invoice are all money we might get back, and
they differ only in how they were detected — so they share one case table, one
bounded attempt budget, one terminal state and one recovered-amount column
rather than four parallel pipelines.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
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

# ── Webhook Event Store ──────────────────────────────────────────────────────


class WebhookEvent(Base):
    """Append-only event store. Every webhook received is logged here."""

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    razorpay_event_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. payment.failed
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_webhook_events_type_received", "event_type", "received_at"),
    )


# ── Processed Events (Idempotency) ──────────────────────────────────────────


class ProcessedEvent(Base):
    """Tracks which Razorpay event IDs have already been processed (dedup)."""

    __tablename__ = "processed_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    razorpay_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("razorpay_event_id", name="uq_processed_event_id"),
    )


# ── Payment Failure Records ──────────────────────────────────────────────────


class PaymentFailure(Base):
    """Enriched failure record created after classification."""

    __tablename__ = "payment_failures"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # in paise
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    method: Mapped[str] = mapped_column(String(50), nullable=False)  # card, upi, netbanking, wallet
    bank: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wallet: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vpa: Mapped[str | None] = mapped_column(String(255), nullable=True)  # UPI VPA
    card_network: Mapped[str | None] = mapped_column(String(50), nullable=True)
    card_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    card_issuer: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Error details from Razorpay (5-tuple)
    error_code: Mapped[str] = mapped_column(String(100), nullable=False)
    error_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_step: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Classification
    failure_class: Mapped[str] = mapped_column(String(50), nullable=False)
    is_retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Customer info
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_contact: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Metadata
    webhook_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_payment_failures_class", "failure_class"),
        Index("ix_payment_failures_method_bank", "method", "bank"),
        Index("ix_payment_failures_failed_at", "failed_at"),
    )


# ── Recovery Cases ───────────────────────────────────────────────────────────


class RecoveryCase(Base):
    """
    One unit of revenue at risk, and the bounded workflow that chases it.

    Source-agnostic on purpose: `risk_type` says how the money went missing and
    `subject_ref` identifies it in that source's namespace (a payment id, a cart
    id, a subscription id, an invoice id, a mandate token). Values for both are
    the Literals in src/cases.py.

    This table is what makes the headline number answerable. `amount_recovered`
    is money actually collected and attributed back to an attempt this engine
    made — not "the payment eventually succeeded", which would credit the engine
    for customers who retried on their own.
    """

    __tablename__ = "recovery_cases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    risk_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # ── Money ────────────────────────────────────────────────────────────
    amount_at_risk: Mapped[int] = mapped_column(Integer, nullable=False)  # paise
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    amount_recovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The payment that actually paid. NOT subject_ref: recovery goes out as a
    # Razorpay Payment Link, and the customer paying it produces a brand-new
    # payment id. Storing only the original id is why nothing could be
    # attributed before this table existed.
    recovered_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Which attempt earned it. NULL means the money came back without us — the
    # customer paid the order directly and the capture matched on order_id, not
    # on a link we sent. Both are recoveries; only this one is *ours*, and a
    # headline that cannot tell them apart takes credit for the control group.
    recovered_via_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # ── Timing ───────────────────────────────────────────────────────────
    # When the money was actually due — an invoice's due date, a subscription's
    # charge date, the moment a cart went cold. Aging, not expiry: a receivables
    # ladder escalates on days-past-due, and there is nowhere else to compute
    # that from. NULL for a card decline, where "due" and "failed" are the same
    # instant and payment_failures already records it.
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Earliest this case may be touched again. Written by the escalation backoff
    # and by a promise-to-pay; read by stop_reason() and by the sweep that finds
    # work for risk types with no inbound webhook. NULL means no wait, so every
    # path that existed before this column behaves exactly as it did.
    next_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Bounded workflow ─────────────────────────────────────────────────
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    close_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Persisted rather than recounted. A count(*) over retry_attempts answers
    # "how many rows exist", which is not the same question once a case can be
    # closed early by opt-out or expiry — and a stopping rule you recompute from
    # a mutable table is a stopping rule that can quietly start again.
    attempts_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Batch / audit ────────────────────────────────────────────────────
    batch_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Re-ingesting the same failure must find the existing case, not open a
        # second one — two cases for one payment would double-count both the
        # attempt budget and the recovered amount.
        UniqueConstraint("risk_type", "subject_ref", name="uq_recovery_case_subject"),
        Index("ix_recovery_cases_state", "state"),
        Index("ix_recovery_cases_type_state", "risk_type", "state"),
        # The sweep: open cases whose wait has elapsed. An invoice or an
        # abandoned cart never sends us a webhook, so something has to go
        # looking, and it must not seq-scan every case ever opened.
        Index("ix_recovery_cases_due", "state", "next_action_at"),
    )


# ── Retry Attempts ───────────────────────────────────────────────────────────


class RetryAttempt(Base):
    """Every retry attempt with its idempotency key, decision, and outcome."""

    __tablename__ = "retry_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Both NULL for a case with no payment behind it — an abandoned cart, an
    # overdue invoice, a mandate that never presented. NOT NULL here is what
    # confined this table to the payment rail: `risk_type` already named five
    # sources, but four of them could not write an attempt row at all.
    payment_failure_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Nullable so rows written before recovery_cases existed still load.
    recovery_case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # What we handed the customer — the Razorpay Payment Link id. This is the
    # join key for revenue attribution: the payment.captured webhook for a link
    # carries the link id, and it is the ONLY thing connecting the new payment
    # back to the case, because the captured payment has an id we have never
    # seen. Without this column the recovered rupees are unattributable.
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Agent decision
    action_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # retry_now, retry_at, switch_rail, nudge_customer, abandon
    target_rail: Mapped[str | None] = mapped_column(String(50), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_type: Mapped[str] = mapped_column(
        String(20), default="llm"
    )  # llm, xgboost, fixed_retry
    agent_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Guardrail
    guardrail_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    guardrail_rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Execution
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Lifecycle: pending (write-ahead, Razorpay not yet answered) → success |
    # failed; scheduled (a retry_at parked for the Layer 6 scheduler) → pending
    # at claim time; rejected (guardrail veto); cancelled (scheduler fire-time
    # re-validation); skipped (abandon — no API call); superseded (money
    # arrived before this attempt resolved). The scheduler's two sweeps filter
    # on (result, scheduled_at) and (result='pending', created_at), which is
    # what the indexes below exist for.
    result: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    result_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Nudge (if applicable)
    nudge_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    nudge_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    # How the customer was reached and in what language. Recorded because
    # contact-frequency rules are per-channel, not global — two WhatsApp nudges
    # and a voice call is three contacts, and an audit that stores only the text
    # cannot show which of them a complaint is about. "hinglish" is a real value
    # here: it is what the voice script speaks, and it is not "hi".
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_retry_attempts_payment_failure", "payment_failure_id"),
        Index("ix_retry_attempts_created_at", "created_at"),
        Index("ix_retry_attempts_case", "recovery_case_id"),
        # The attribution lookup path: a capture arrives knowing only the link id.
        Index("ix_retry_attempts_external_ref", "external_ref"),
        # The scheduler's fire sweep: due `retry_at` rows, every tick. Without
        # it each poll scans every attempt ever written.
        Index("ix_retry_attempts_scheduled", "result", "scheduled_at"),
        # The stale-pending sweep: write-ahead rows whose outcome never landed.
        Index("ix_retry_attempts_pending", "result", "created_at"),
    )


# ── Retry Ledger (Rate Limiting) ────────────────────────────────────────────


class RetryLedger(Base):
    """Per-customer running tally for rate limiting and consent tracking."""

    __tablename__ = "retry_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )  # email or contact
    total_retries_24h: Mapped[int] = mapped_column(Integer, default=0)
    total_nudges_24h: Mapped[int] = mapped_column(Integer, default=0)
    last_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_nudge_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Compliance stop, distinct from the rate limits above. blocked_until is a
    # cooldown that expires on its own; an opt-out does not. Checked before any
    # outbound contact, and it closes open cases for this customer rather than
    # just skipping one nudge — "granted" | "opted_out" (src/cases.py).
    consent_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="granted"
    )
    opted_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ── Promises to Pay ─────────────────────────────────────────────────────────


class PromiseToPay(Base):
    """
    A commitment to pay by a date, and whether it held.

    Its own table rather than columns on the case, because a case collects
    several: a promise breaks, the customer re-promises, and the kept-rate
    across those is the signal that decides whether another contact is worth
    making. Keeping only the latest promise on the case erases exactly that.

    While one is pending the case must go quiet — `record_promise()` pushes
    `RecoveryCase.next_action_at` out to `due_at` and `stop_reason()` refuses to
    act before then. That is the compliance half of the feature, not a nicety: a
    customer who answered and committed has earned silence until the date they
    named, and chasing them anyway is the behaviour a regulator asks about.
    """

    __tablename__ = "promises_to_pay"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    amount_promised: Mapped[int] = mapped_column(Integer, nullable=False)  # paise
    promised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Where the promise came from, so a disputed one can be produced on demand.
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # "pending" | "kept" | "broken" | "cancelled" — Literals in src/cases.py.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The payment that kept it. A different id from anything on the case, for
    # the same reason `recovered_ref` is: paying a link mints a new payment.
    resolved_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # The tracker's sweep: pending promises whose date has passed. Ordered
        # this way because status is the selective half — most promises are
        # resolved and never need looking at again.
        Index("ix_promises_status_due", "status", "due_at"),
    )


# ── Case Audit Trail ────────────────────────────────────────────────────────


class CaseEvent(Base):
    """
    Append-only record of everything done to a case. Never updated, never
    deleted.

    `webhook_events` covers what arrived and `retry_attempts` covers decisions
    that ended in an attempt — but a case is also closed, escalated, opted out
    of, credited and promised against, and none of those left a trace. "Why did
    this customer get contacted four times" was not answerable from the
    database, which is the question an audit trail exists for.

    Integer primary key on purpose: this is a log, and a monotonic id orders it
    without depending on a timestamp that two rows in one transaction share.
    """

    __tablename__ = "case_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Values are the CaseEventType Literal in src/cases.py.
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # Who acted. "system" for the pipeline, "agent" for an LLM decision, or an
    # operator id when a human overrode it — the distinction a compliance review
    # asks for first and the one a single boolean cannot carry.
    actor: Mapped[str] = mapped_column(String(40), nullable=False, default="system")
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_case_events_case", "recovery_case_id", "id"),
        Index("ix_case_events_type_created", "event_type", "created_at"),
    )
