"""
SQLAlchemy ORM models for the payment recovery engine.

Tables:
    - webhook_events:    Append-only event store (fully replayable)
    - payment_failures:  Enriched failure records with classification
    - retry_attempts:    Every retry attempt with idempotency key and outcome
    - retry_ledger:      Per-customer running tally for rate limiting
    - processed_events:  Idempotency table for webhook deduplication
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


# ── Retry Attempts ───────────────────────────────────────────────────────────


class RetryAttempt(Base):
    """Every retry attempt with its idempotency key, decision, and outcome."""

    __tablename__ = "retry_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_failure_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payment_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

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
    result: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # success, failed, pending, skipped
    result_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Nudge (if applicable)
    nudge_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    nudge_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_retry_attempts_payment_failure", "payment_failure_id"),
        Index("ix_retry_attempts_created_at", "created_at"),
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
