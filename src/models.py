"""
SQLAlchemy ORM models for the payment recovery engine.

Tables:
    - webhook_events:    Append-only event store (fully replayable)
    - risk_events:       Append-only store for merchant-pushed risk events
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
    ForeignKey,
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    razorpay_event_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. payment.failed
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processing_error: Mapped[str | None] = mapped_column(Text)
    # How many times the reconciler has tried to run this event through the
    # pipeline. The claim-then-process sweep used to consume the event on the
    # first exception — a transient database blip permanently skipped a real
    # payment failure. Now the sweep re-arms the event (processed=False) until
    # this crosses the retry cap, so only a deterministically-broken payload
    # is ever given up on.
    processing_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    __table_args__ = (
        Index("ix_webhook_events_type_received", "event_type", "received_at"),
    )


# ── Risk Event Store (merchant-pushed) ──────────────────────────────────────


class RiskEvent(Base):
    """
    Append-only store for revenue-at-risk events the merchant pushes to us.

    A card decline announces itself through Razorpay's webhook; an abandoned
    cart, a halted subscription, an overdue invoice and a failed mandate debit
    only exist in the merchant's own systems, so the merchant POSTs them to
    /risks (HMAC-signed). This table is the durable record of what arrived,
    mirroring webhook_events: committed before the background task runs, so a
    crash between the 200 and the processing loses nothing, and the
    reconcile_risk_events sweep re-runs anything whose task died.

    The denormalised columns (reference_id, customer_*, amount) exist so the
    chaser can read them back without parsing the payload: the pipeline needs
    the customer's email/contact to mint a link days after the event arrived,
    and the payload is the merchant's free-form shape, not ours.
    """

    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The dedup key: merchant-supplied, or derived from
    # (risk_type, reference_id, occurred_at) when absent. UNIQUE so a
    # re-delivered event is a clean 200, not a second case.
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    risk_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reference_id: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # paise
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    customer_id: Mapped[str | None] = mapped_column(String(255))
    customer_email: Mapped[str | None] = mapped_column(String(255))
    customer_contact: Mapped[str | None] = mapped_column(String(20))
    # The merchant's own code for the buyer organisation (their ERP customer
    # code). When present, the receivables layer consolidates this event's
    # cases under that account; when absent, the account is derived from the
    # canonical customer key. Denormalised here for the same reason the other
    # customer_* columns are: the consolidation runs days after arrival.
    account_ref: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the money was due — an invoice's due date, a subscription's charge
    # date. Drives RecoveryCase.due_at, which the receivables ladder ages on.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A Razorpay offer id (merchant's own account) for cart chases — the
    # incentive a merchant may attach from the second touch on. NULL is the
    # default and means no offer, which keeps every pre-existing event's
    # behaviour byte-for-byte.
    offer_id: Mapped[str | None] = mapped_column(String(64))
    # Merchant free-form context (cart contents, invoice number, mandate name,
    # plan). Reduced to bounded printable data before it reaches an LLM prompt
    # — see src/agent/prompts.py, UNTRUSTED INPUT.
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # The raw payload as received, kept for replay and dispute.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processing_error: Mapped[str | None] = mapped_column(Text)
    # Same re-arm discipline as webhook_events: a transient failure re-arms the
    # event until the cap; only a deterministically-broken payload rests.
    processing_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_risk_event_id"),
        Index("ix_risk_events_type_received", "risk_type", "received_at"),
        Index("ix_risk_events_reference", "risk_type", "reference_id"),
    )


# ── Scheduler heartbeat ─────────────────────────────────────────────────────


class SchedulerHeartbeat(Base):
    """
    A dead-man's-switch row: the scheduler stamps it on every tick.

    A tick that swallows an exception (correctly — one bad tick must not end the
    loop) is indistinguishable from a scheduler that died three days ago, unless
    something outside the loop remembers when it last ran. This row is that
    something: single-row by convention (id=1), read by the Operations view, and
    stale past a couple of intervals means nobody is firing deferred retries.
    """

    __tablename__ = "scheduler_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    last_tick_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_tick_counts: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(String(255), index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # in paise
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    method: Mapped[str] = mapped_column(String(50), nullable=False)  # card, upi, netbanking, wallet
    bank: Mapped[str | None] = mapped_column(String(100))
    wallet: Mapped[str | None] = mapped_column(String(100))
    vpa: Mapped[str | None] = mapped_column(String(255))  # UPI VPA
    card_network: Mapped[str | None] = mapped_column(String(50))
    card_type: Mapped[str | None] = mapped_column(String(20))
    card_issuer: Mapped[str | None] = mapped_column(String(100))

    # Error details from Razorpay (5-tuple)
    error_code: Mapped[str] = mapped_column(String(100), nullable=False)
    error_description: Mapped[str | None] = mapped_column(Text)
    error_source: Mapped[str | None] = mapped_column(String(50))
    error_step: Mapped[str | None] = mapped_column(String(100))
    error_reason: Mapped[str | None] = mapped_column(String(100))

    # Classification
    failure_class: Mapped[str] = mapped_column(String(50), nullable=False)
    is_retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Customer info
    customer_email: Mapped[str | None] = mapped_column(String(255))
    customer_contact: Mapped[str | None] = mapped_column(String(20))

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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    risk_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_id: Mapped[str | None] = mapped_column(String(255), index=True)
    # The AR account this case consolidates under (B2B receivables layer,
    # src/receivables/accounts.py). Nullable on purpose: a case with no
    # derivable buyer identity chases per-case exactly as it did before the
    # receivables layer existed. No FK — same convention as every cross-table
    # reference in this schema (bare UUID + index), so the receivables tables
    # stay loadable and testable on their own.
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # ── Money ────────────────────────────────────────────────────────────
    amount_at_risk: Mapped[int] = mapped_column(Integer, nullable=False)  # paise
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    amount_recovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The payment that actually paid. NOT subject_ref: recovery goes out as a
    # Razorpay Payment Link, and the customer paying it produces a brand-new
    # payment id. Storing only the original id is why nothing could be
    # attributed before this table existed.
    recovered_ref: Mapped[str | None] = mapped_column(String(255))
    # EVERY payment id credited to this case, recovered_ref being only the
    # latest. The membership test in cases.py replay-guards captures on a
    # case paid in three or more parts, where the single latest-ref check
    # would let an earlier distinct capture credit twice.
    credited_refs: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    recovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Which attempt earned it. NULL means the money came back without us — the
    # customer paid the order directly and the capture matched on order_id, not
    # on a link we sent. Both are recoveries; only this one is *ours*, and a
    # headline that cannot tell them apart takes credit for the control group.
    recovered_via_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )

    # ── Timing ───────────────────────────────────────────────────────────
    # When the money was actually due — an invoice's due date, a subscription's
    # charge date, the moment a cart went cold. Aging, not expiry: a receivables
    # ladder escalates on days-past-due, and there is nowhere else to compute
    # that from. NULL for a card decline, where "due" and "failed" are the same
    # instant and payment_failures already records it.
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Earliest this case may be touched again. Written by the escalation backoff
    # and by a promise-to-pay; read by stop_reason() and by the sweep that finds
    # work for risk types with no inbound webhook. NULL means no wait, so every
    # path that existed before this column behaves exactly as it did.
    next_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # ── Bounded workflow ─────────────────────────────────────────────────
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    close_reason: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Persisted rather than recounted. A count(*) over retry_attempts answers
    # "how many rows exist", which is not the same question once a case can be
    # closed early by opt-out or expiry — and a stopping rule you recompute from
    # a mutable table is a stopping rule that can quietly start again.
    attempts_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Batch / audit ────────────────────────────────────────────────────
    batch_id: Mapped[str | None] = mapped_column(String(100), index=True)
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
        # The receivables consolidation sweep: open invoice cases grouped by
        # account. account_id is nullable, so partial rows never use it.
        Index("ix_recovery_cases_account", "account_id", "state"),
    )


# ── Retry Attempts ───────────────────────────────────────────────────────────


class RetryAttempt(Base):
    """Every retry attempt with its idempotency key, decision, and outcome."""

    __tablename__ = "retry_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Both NULL for a case with no payment behind it — an abandoned cart, an
    # overdue invoice, a mandate that never presented. NOT NULL here is what
    # confined this table to the payment rail: `risk_type` already named five
    # sources, but four of them could not write an attempt row at all.
    payment_failure_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    payment_id: Mapped[str | None] = mapped_column(String(255), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Nullable so rows written before recovery_cases existed still load.
    recovery_case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    # What we handed the customer — the Razorpay Payment Link id. This is the
    # join key for revenue attribution: the payment.captured webhook for a link
    # carries the link id, and it is the ONLY thing connecting the new payment
    # back to the case, because the captured payment has an id we have never
    # seen. Without this column the recovered rupees are unattributable.
    external_ref: Mapped[str | None] = mapped_column(String(255))

    # Agent decision
    action_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # retry_now, retry_at, switch_rail, nudge_customer, abandon
    target_rail: Mapped[str | None] = mapped_column(String(50))
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    agent_reasoning: Mapped[str | None] = mapped_column(Text)
    agent_type: Mapped[str] = mapped_column(
        String(20), default="llm"
    )  # llm, xgboost, fixed_retry
    agent_confidence: Mapped[float | None] = mapped_column(Float)

    # Guardrail
    guardrail_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    guardrail_rejection_reason: Mapped[str | None] = mapped_column(Text)

    # Execution
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Lifecycle: pending (write-ahead, Razorpay not yet answered) → success |
    # failed; scheduled (a retry_at parked for the Layer 6 scheduler) → pending
    # at claim time; rejected (guardrail veto); cancelled (scheduler fire-time
    # re-validation); skipped (abandon — no API call); superseded (money
    # arrived before this attempt resolved). The scheduler's two sweeps filter
    # on (result, scheduled_at) and (result='pending', created_at), which is
    # what the indexes below exist for.
    result: Mapped[str | None] = mapped_column(
        String(50)
    )
    result_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Nudge (if applicable)
    nudge_message: Mapped[str | None] = mapped_column(Text)
    nudge_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    # How the customer was reached and in what language. Recorded because
    # contact-frequency rules are per-channel, not global — two WhatsApp nudges
    # and a voice call is three contacts, and an audit that stores only the text
    # cannot show which of them a complaint is about. "hinglish" is a real value
    # here: it is what the voice script speaks, and it is not "hi".
    channel: Mapped[str | None] = mapped_column(String(20))
    language: Mapped[str | None] = mapped_column(String(20))

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
        DateTime(timezone=True)
    )
    last_nudge_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # When the CURRENT counting window opened. The old reset rule keyed off
    # last_retry_at alone, so contacts spaced just inside the window kept the
    # tally alive indefinitely — "5 per 24h" was really "5 per 24h from the
    # last contact". Anchoring the window at its first contact makes the
    # reset deterministic: past window_started + window, the tally resets no
    # matter how recently the last contact was. NULL on legacy rows = fall
    # back to the old last_* behaviour (see orchestrator._effective_counts).
    retries_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    nudges_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Compliance stop, distinct from the rate limits above. blocked_until is a
    # cooldown that expires on its own; an opt-out does not. Checked before any
    # outbound contact, and it closes open cases for this customer rather than
    # just skipping one nudge — "granted" | "opted_out" (src/cases.py).
    consent_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="granted"
    )
    opted_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    customer_id: Mapped[str | None] = mapped_column(String(255), index=True)

    amount_promised: Mapped[int] = mapped_column(Integer, nullable=False)  # paise
    promised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Where the promise came from, so a disputed one can be produced on demand.
    channel: Mapped[str | None] = mapped_column(String(20))
    language: Mapped[str | None] = mapped_column(String(20))
    source_ref: Mapped[str | None] = mapped_column(String(255))

    # "pending" | "kept" | "broken" | "cancelled" — Literals in src/cases.py.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # The payment that kept it. A different id from anything on the case, for
    # the same reason `recovered_ref` is: paying a link mints a new payment.
    resolved_ref: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    # ── Capture quality (who promised, how firmly) ────────────────────────
    # Nullable because every pre-existing row and every operator-logged
    # promise simply was not assessed — None means "unknown", never "full
    # confidence", and the metrics treat it as its own segment.
    is_partial: Mapped[bool | None] = mapped_column(Boolean)
    # "explicit" | "tentative" | "conditional" — the kept-rate analysis
    # segment research (Monk) puts the whole kept-vs-count argument on:
    # a tentative promise breaking is not the same signal as an explicit one.
    confidence: Mapped[str | None] = mapped_column(String(20))
    # A stated condition, sanitized at the WRITE boundary (same reduction as
    # gateway free text): "salary aane ke baad" is a promise about the
    # customer's cash cycle and belongs in the re-ask decision. Never rides
    # verbatim into an LLM prompt — the score aggregates only.
    condition_note: Mapped[str | None] = mapped_column(Text)
    # The rail the customer named, if any — kept-rate by rail is the honest
    # answer to "should we steer promises toward UPI links".
    promised_rail: Mapped[str | None] = mapped_column(String(20))

    # ── Workflow markers ──────────────────────────────────────────────────
    # The pre-due reminder sweep's one-shot marker: set when the reminder
    # fired (or was skipped-with-reason), so "remind 48h before due" can
    # never become "remind every tick until due". Never reset — one promise,
    # one reminder, by construction.
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Days between due_at and the payment that kept it (0 = on time or
    # early). The honest kept-rate metric separates kept-on-time from
    # kept-in-grace with this, instead of libelling a late payment as on-time.
    kept_late_days: Mapped[int | None] = mapped_column(Integer)

    # ── UPI Autopay mandate (optional; a promise works without one) ────────
    # A promise used to collect nothing: it deferred chasing, sent a reminder
    # 48h out, and waited for the customer to pull the money. These columns are
    # the other half — the customer may authorise a UPI Autopay mandate for
    # this promise's amount and date, and the scheduler debits it when the date
    # arrives.
    #
    # Deliberately columns on the promise rather than a mandate table. The
    # mandate is authorised for ONE amount on ONE date; a reusable per-customer
    # mandate is a different product decision (and a different consent), and
    # inventing the table now would pre-commit to it.
    #
    # `none` is the default and the fallback, so every pre-existing row and
    # every unmandated promise behaves exactly as it always has. That fallback
    # is not a lesser path: above the RBI unattended-debit threshold
    # (mandate_max_auto_debit_paise) a mandate must not be offered at all, so
    # for larger promises it is the only lawful one.
    mandate_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="none"
    )
    # The gateway's token id for the authorised mandate. Opaque to us; the only
    # thing that can be debited.
    mandate_token: Mapped[str | None] = mapped_column(String(255))
    # The authorisation payment/registration-link id, kept for the audit trail:
    # it is the evidence the customer consented, and the join key if a mandate
    # is ever disputed.
    mandate_authorization_ref: Mapped[str | None] = mapped_column(String(255))
    # Razorpay's OWN customer id, minted when the mandate was authorised. Not
    # RecoveryCase.customer_id, which is this engine's canonical key
    # (`email:a@b.in`) and means nothing to the gateway. The recurring charge
    # needs both this and the token, so storing one without the other would be
    # an authorisation we cannot actually collect against.
    mandate_customer_ref: Mapped[str | None] = mapped_column(String(255))
    mandate_registered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # When the debit was actually presented. Read by expire_promises: a promise
    # whose mandate was charged is NOT broken while the capture is still in
    # flight — the money left the customer's account and calling that a broken
    # promise is the exact libel `promise_grace_hours` exists to prevent. The
    # timestamp rather than a bare status flag so the reprieve is BOUNDED: if
    # no capture lands within the grace window of the charge, the promise
    # breaks normally instead of hanging pending forever.
    mandate_charged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # The tracker's sweep: pending promises whose date has passed. Ordered
        # this way because status is the selective half — most promises are
        # resolved and never need looking at again.
        Index("ix_promises_status_due", "status", "due_at"),
        # The debit sweep: promises whose mandate is live and whose date has
        # come. Same shape and same reasoning as the index above —
        # mandate_status is the selective half, since most promises carry none.
        Index("ix_promises_mandate_due", "mandate_status", "due_at"),
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
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Hash chain, stamped by src/audit_chain.py — deliberately NOT computed
    # inline in cases.log_event(). That function is a synchronous, no-I/O
    # session.add() by design (the audit row lands in the same transaction as
    # the change it describes); reading the previous row's hash before every
    # insert would add a query to that hot path. Instead these start NULL and
    # a separate stamping pass (idempotent, append-only) fills them in after
    # the fact — verifiable independently of whether it has run yet.
    event_hash: Mapped[str | None] = mapped_column(String(64))
    prev_event_hash: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_case_events_case", "recovery_case_id", "id"),
        Index("ix_case_events_type_created", "event_type", "created_at"),
    )


class VoiceCallQueue(Base):
    """
    Voice calls the engine wants placed, in the order the chasers queued
    them.

    The engine never dials. This row is a work item for the telephony leg:
    the orchestrator writes it in the SAME transaction as the chase attempt
    (write-ahead, same correctness property as RetryAttempt), the telephony
    leg claims it via POST /voice/queue/claim (HMAC-signed), places the call
    through its own provider, and drives the conversation by POSTing
    transcripts to /voice/turn. Opt-in by risk type — VOICE_CHASER_ENABLED
    gates the queueing at all, because a call is the highest-friction
    contact the engine can make and must never ship silently on.

    States are the call's real lifecycle: queued -> claimed -> done/failed,
    with opted_out as the terminal a spoken "band karo" forces.
    """

    __tablename__ = "voice_call_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recovery_cases.id"),
        nullable=False,
    )
    # The RetryAttempt this call belongs to — the voice touch of a chase is
    # still an attempt-row event, audited and capped like every other.
    retry_attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Customer's phone, copied at queue time: the telephony leg must never
    # need a case join to dial, and the number it dialed is part of the
    # compliance record. PII like the rest of the contact fields.
    customer_contact: Mapped[str | None] = mapped_column(String(20))
    risk_type: Mapped[str] = mapped_column(String(40), nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    # queued / claimed / done / failed / opted_out
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    # What the call leg reported back: outcome, spoken opt-out, last intent.
    result: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_voice_call_queue_recovery_case_id", "recovery_case_id"),
        Index("ix_voice_call_queue_state_created", "state", "created_at"),
    )


class AuditCheckpoint(Base):
    """
    Periodic anchor of the case_events hash chain (src/audit_checkpoint.py).

    One row per epoch: the event id the epoch ends at, the chain head hash at
    that boundary, and a keyed signature binding the two. Verification after
    an epoch exists recomputes only the post-checkpoint tail and checks older
    stretches against these signatures — O(recent history) instead of
    O(all history), with the same tamper-evidence: rewriting any row inside
    an old epoch changes that epoch's head, breaking its checkpoint
    signature; forging a checkpoint requires AUDIT_CHAIN_SECRET.
    """

    __tablename__ = "audit_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    last_event_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    head_event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    events_through: Mapped[int] = mapped_column(Integer, nullable=False)
    # The rotation marker (see audit_checkpoint.verify_chain_epoch): when
    # this epoch's stretch was last re-verified FROM CONTENT. NULL = due.
    # Every verification run recomputes the oldest NULL epoch and stamps it,
    # so O(all history) re-reading is spread across runs in rotation order
    # instead of repeating per run or — worse — never happening at all.
    content_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
