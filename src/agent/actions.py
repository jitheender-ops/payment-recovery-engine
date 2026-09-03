"""
Fixed action space for the policy agent.

The agent (LLM or XGBoost) MUST output one of these actions.
The guardrail gate validates against these schemas before execution.
No freeform output is ever accepted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ActionType = Literal[
    "retry_now",
    "retry_at",
    "switch_rail",
    "nudge_customer",
    "abandon",
]

PaymentRail = Literal["upi", "card", "netbanking", "wallet"]


class RetryAction(BaseModel):
    """
    Constrained action output from the policy agent.

    Every field is typed and validated. The guardrail gate applies additional
    business-rule checks on top of schema validation.
    """

    action: ActionType = Field(
        ...,
        description="The recovery action to take. Must be one of the fixed set.",
    )
    rail: PaymentRail | None = Field(
        default=None,
        description="Target payment rail. Required for 'switch_rail', optional otherwise.",
    )
    retry_at: datetime | None = Field(
        default=None,
        description="Scheduled retry time (UTC). Required for 'retry_at' action.",
    )
    reason: str = Field(
        ...,
        min_length=5,
        max_length=500,
        description="Agent's reasoning for this action. Logged for audit, not shown to customer.",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Agent's confidence in this action (0-1). Optional.",
    )


class FailureContext(BaseModel):
    """
    Structured input to the policy agent — all the information needed to
    decide on a recovery action. Assembled by the orchestrator.

    Despite the name this now carries ANY revenue-at-risk case, not only a
    failed payment: `risk_type` says which, and the payment-specific fields
    (method, bank, card_*) are simply empty for the non-payment types. The
    agent's action space is unchanged — a "retry" for an overdue invoice is a
    payment link, not a re-presented charge — so one context shape serves all
    five risk types.
    """

    # Which revenue leak this is. Defaults to the original rail so every
    # pre-existing caller (and every stored test fixture) behaves exactly as
    # before. The four non-payment values come from src/cases.py RiskType.
    risk_type: str = "payment_failure"

    # Failure details
    payment_id: str
    order_id: str | None = None
    failure_class: str  # FailureClass value
    error_code: str
    error_description: str | None = None
    error_source: str | None = None
    error_reason: str | None = None

    # Payment details
    amount: int  # in paise
    currency: str = "INR"
    method: str  # card, upi, netbanking, wallet
    bank: str | None = None
    card_network: str | None = None
    card_type: str | None = None

    # Customer context
    customer_id: str | None = None  # email or contact
    customer_email: str | None = None
    customer_contact: str | None = None
    retry_count_24h: int = 0
    nudge_count_24h: int = 0
    previous_retry_outcomes: list[str] = Field(default_factory=list)

    # Temporal context
    failed_at: datetime
    current_time: datetime
    hour_of_day: int = Field(ge=0, le=23)
    day_of_week: int = Field(ge=0, le=6)  # 0 = Monday

    # Consent window override. None means "use the global
    # consent_window_hours"; a chaser-driven risk type sets its own (a cold
    # cart is stale in two days, a receivable is chaseable for a month). The
    # guardrail reads this through the context so the window the agent was
    # told about and the window the gate enforces cannot drift.
    consent_window_hours: int | None = None

    # Merchant-supplied context for non-payment risk types (cart contents,
    # invoice number, mandate name, plan). Already reduced to bounded
    # printable data before it reaches a prompt — see prompts.sanitize_meta.
    risk_meta: dict[str, Any] | None = None

    # Metadata
    is_retryable: bool = True
    original_failure_id: str | None = None

    # When the customer was last successfully notified about this case (any
    # successful nudge_customer attempt). Only consulted for
    # risk_type=mandate_failure — the RBI e-mandate framework requires a
    # pre-debit notice at least 24h before a mandate is re-presented for
    # collection. None means no notification has gone out yet.
    last_notification_sent_at: datetime | None = None

    # True only for the scheduler's promise-backed UPI Autopay debit. It rides
    # on the context beside last_notification_sent_at because it feeds the same
    # rule: a mandate debit is lawful only 24h after a notice the customer
    # actually received. Defaults False, so every existing caller and every
    # stored fixture is unchanged — the same defaulting pattern risk_type uses.
    is_mandate_debit: bool = False

    # Promise-to-pay history for this customer: kept count, broken count,
    # pending count. Defaults (all zero) keep every stored fixture and
    # pre-existing caller behaving exactly as before — the same defaulting
    # pattern risk_type uses. The agent reasons over it ("a customer who
    # kept promises is worth one more gentle contact; two+ broken promises
    # without new information → prefer abandon"); kept_rate itself is NOT
    # sent, so the prompt cannot be fed a derived number we cannot audit.
    promise_kept: int = 0
    promise_broken: int = 0
    promise_pending: int = 0
