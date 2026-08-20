"""
Fixed action space for the policy agent.

The agent (LLM or XGBoost) MUST output one of these actions.
The guardrail gate validates against these schemas before execution.
No freeform output is ever accepted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

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
    rail: Optional[PaymentRail] = Field(
        default=None,
        description="Target payment rail. Required for 'switch_rail', optional otherwise.",
    )
    retry_at: Optional[datetime] = Field(
        default=None,
        description="Scheduled retry time (UTC). Required for 'retry_at' action.",
    )
    reason: str = Field(
        ...,
        min_length=5,
        max_length=500,
        description="Agent's reasoning for this action. Logged for audit, not shown to customer.",
    )
    confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Agent's confidence in this action (0-1). Optional.",
    )


class FailureContext(BaseModel):
    """
    Structured input to the policy agent — all the information needed to
    decide on a recovery action. Assembled by the orchestrator.
    """

    # Failure details
    payment_id: str
    order_id: Optional[str] = None
    failure_class: str  # FailureClass value
    error_code: str
    error_description: Optional[str] = None
    error_source: Optional[str] = None
    error_reason: Optional[str] = None

    # Payment details
    amount: int  # in paise
    currency: str = "INR"
    method: str  # card, upi, netbanking, wallet
    bank: Optional[str] = None
    card_network: Optional[str] = None
    card_type: Optional[str] = None

    # Customer context
    customer_id: Optional[str] = None  # email or contact
    customer_email: Optional[str] = None
    customer_contact: Optional[str] = None
    retry_count_24h: int = 0
    nudge_count_24h: int = 0
    previous_retry_outcomes: list[str] = Field(default_factory=list)

    # Temporal context
    failed_at: datetime
    current_time: datetime
    hour_of_day: int = Field(ge=0, le=23)
    day_of_week: int = Field(ge=0, le=6)  # 0 = Monday

    # Metadata
    is_retryable: bool = True
    original_failure_id: Optional[str] = None
