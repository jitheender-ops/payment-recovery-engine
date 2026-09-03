"""
Failure taxonomy — deterministic classification of payment failure types.

This enum is the contract between the classifier (Layer 2), the policy agent
(Layer 3), and the guardrail gate (Layer 4). Adding a new failure class here
automatically makes it available throughout the pipeline.
"""

from __future__ import annotations

from enum import Enum


class FailureClass(str, Enum):
    """
    Payment failure taxonomy.

    Each class maps to a distinct recovery strategy. The classifier maps
    Razorpay's 5-tuple error codes to one of these classes using a
    deterministic lookup table (no LLM involved).
    """

    # ── Retryable (same rail) ────────────────────────────────────────
    INSUFFICIENT_FUNDS = "insufficient_funds"
    """Customer's account has insufficient balance. Retry later or nudge
    to use a different instrument."""

    BANK_DOWNTIME = "bank_downtime"
    """Issuing bank is temporarily unavailable. Retry after a delay."""

    NETWORK_ERROR = "network_error"
    """Transient network/gateway error. Immediate retry often succeeds."""

    UPI_COLLECT_TIMEOUT = "upi_collect_timeout"
    """Customer didn't approve UPI collect request in time. Nudge and retry."""

    PAYMENT_TIMEOUT = "payment_timeout"
    """Transaction timed out at any stage. Retry is usually safe."""

    # ── Retryable (switch rail) ──────────────────────────────────────
    THREEDS_DROPOFF = "3ds_dropoff"
    """Customer dropped off during 3D-Secure (OTP entry). Nudge to retry
    or suggest UPI as a simpler flow."""

    ISSUER_DECLINE = "issuer_decline"
    """Issuer bank declined without a specific reason. May succeed on
    retry or on a different rail."""

    CARD_LIMIT_EXCEEDED = "card_limit_exceeded"
    """Daily/transaction limit exceeded on this card. Suggest alternate
    payment method."""

    RISK_CHECK_FAILED = "risk_check_failed"
    """A risk screen stopped this payment on THIS instrument. Razorpay
    documents it retryable and advises the customer retry "with a different
    card or method" — a rail switch, not another attempt on the same one.
    Switch-only: see `is_switch_only`."""

    # ── Non-retryable (abandon) ──────────────────────────────────────
    INVALID_CARD = "invalid_card"
    """Card number, expiry, or CVV is incorrect. Cannot retry same details."""

    EXPIRED_INSTRUMENT = "expired_instrument"
    """Card or instrument is expired. Customer must use a different one."""

    FRAUD_BLOCK = "fraud_block"
    """Blocked by fraud/risk engine. Do NOT retry — hard stop."""

    HARD_DECLINE = "hard_decline"
    """Permanent decline from issuer (stolen card, closed account, etc.).
    Do NOT retry."""

    CUSTOMER_CANCELLED = "customer_cancelled"
    """Customer explicitly cancelled the payment. Respect their intent."""

    # ── Catch-all ────────────────────────────────────────────────────
    UNKNOWN = "unknown"
    """Unrecognised error code. Logged for manual review. Default to
    conservative handling (no retry)."""

    @property
    def is_retryable(self) -> bool:
        """Whether this failure class is considered retryable."""
        return self in _RETRYABLE_CLASSES

    @property
    def is_hard_decline(self) -> bool:
        """Whether this failure class is a hard decline (never retry)."""
        return self in _HARD_DECLINE_CLASSES

    @property
    def is_switch_only(self) -> bool:
        """Retryable, but only on a rail other than the one that just failed."""
        return self in _SWITCH_ONLY_CLASSES


# Pre-computed sets for O(1) membership checks
_RETRYABLE_CLASSES = {
    FailureClass.INSUFFICIENT_FUNDS,
    FailureClass.BANK_DOWNTIME,
    FailureClass.NETWORK_ERROR,
    FailureClass.UPI_COLLECT_TIMEOUT,
    FailureClass.PAYMENT_TIMEOUT,
    FailureClass.THREEDS_DROPOFF,
    FailureClass.ISSUER_DECLINE,
    FailureClass.CARD_LIMIT_EXCEEDED,
    FailureClass.RISK_CHECK_FAILED,
}

# Retryable, but never on the instrument that just failed. Re-presenting the
# same card into the same risk screen is a guaranteed decline that still costs
# an attempt slot and a contact — so the guardrail rejects retry_now/retry_at
# for these (src/guardrail/rules.py:check_switch_only_class), leaving
# switch_rail and nudge_customer as the only ways to spend one.
_SWITCH_ONLY_CLASSES = {
    FailureClass.RISK_CHECK_FAILED,
}

_HARD_DECLINE_CLASSES = {
    FailureClass.INVALID_CARD,
    FailureClass.EXPIRED_INSTRUMENT,
    FailureClass.FRAUD_BLOCK,
    FailureClass.HARD_DECLINE,
    FailureClass.CUSTOMER_CANCELLED,
}
