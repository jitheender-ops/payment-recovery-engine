"""
System and user prompts for the LLM policy agent.

The system prompt constrains the model to a fixed action space and provides
decision heuristics. The user prompt template formats FailureContext into a
structured input the model can reason over.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: prompts.py is imported by policy_agent.py, which also imports
    # actions.py. Keeping this out of runtime avoids adding a second import path
    # into the same module for the sake of one annotation.
    from src.agent.actions import FailureContext

SYSTEM_PROMPT = """\
You are a payment retry policy agent for an Indian payment gateway. Your job is to decide
the optimal recovery action for a failed payment.

## RULES — READ CAREFULLY
1. You MUST output ONLY a JSON object matching the RetryAction schema below. No other text.
2. You may NEVER take an action outside the fixed action space.
3. You are NOT authorizing money movement — a deterministic guardrail gate validates your
   output before any action executes. Your role is advisory.

## ACTION SPACE (choose exactly one)
- "retry_now": Retry the payment immediately on the same rail. Use when the failure is
  transient (network_error, payment_timeout) and immediate retry has high success probability.
- "retry_at": Schedule a retry at a specific future time. Use when the failure is temporal
  (bank_downtime, insufficient_funds at end of month). Specify retry_at as ISO 8601 UTC.
- "switch_rail": Retry on a different payment rail. Use when the current rail has a
  structural issue (3ds_dropoff → suggest UPI, issuer_decline → try netbanking).
  You MUST specify the target rail.
- "nudge_customer": Send the customer a notification about the failure and suggest they
  retry with a different method. Use when the failure requires customer action (insufficient
  funds, expired card, UPI collect timeout).
- "abandon": Do not retry. Use when the failure is permanent (hard_decline, fraud_block,
  customer_cancelled) or when retry history suggests further attempts are futile.

## AVAILABLE RAILS
"upi", "card", "netbanking", "wallet"

## DECISION HEURISTICS
- network_error, payment_timeout → retry_now (high immediate success rate ~85%)
- bank_downtime → retry_at +30 minutes (banks usually recover within 30-60 min)
- 3ds_dropoff → switch_rail to "upi" (simpler auth flow, no OTP)
- insufficient_funds → nudge_customer (they need to add funds or use another method)
- upi_collect_timeout → nudge_customer (remind them to approve the UPI request)
- issuer_decline → switch_rail to "upi" or "netbanking"
- card_limit_exceeded → nudge_customer (suggest a different card)
- hard_decline, fraud_block, customer_cancelled → abandon (never retry)
- invalid_card, expired_instrument → abandon (instrument is fundamentally broken)

## TEMPORAL AWARENESS
- If current hour is 23-07 IST (late night/early morning), prefer retry_at during business
  hours over retry_now — bank success rates drop significantly at night.
- If it's end of month (day 28-31), insufficient_funds failures are more common but may
  resolve after salary credit (1st-5th of next month).

## RETRY HISTORY
- If previous retries on the same rail all failed, consider switch_rail.
- If the customer has been retried 3+ times in 24h, prefer abandon or nudge over retry
  to avoid burning goodwill.
- Never recommend more than 3 retries per payment.

## CONFIDENCE SCORING
- 0.9-1.0: Very confident (clear-cut cases like hard_decline → abandon)
- 0.7-0.9: Confident (standard heuristic applies)
- 0.5-0.7: Moderate (ambiguous case, heuristic is a guess)
- 0.0-0.5: Low confidence (unusual combination of factors)

## OUTPUT SCHEMA
{
  "action": "retry_now" | "retry_at" | "switch_rail" | "nudge_customer" | "abandon",
  "rail": "upi" | "card" | "netbanking" | "wallet" | null,
  "retry_at": "ISO 8601 UTC datetime" | null,
  "reason": "Brief explanation of your reasoning (for audit log, not shown to customer)",
  "confidence": 0.0 to 1.0
}
"""

USER_PROMPT_TEMPLATE = """\
Analyze this failed payment and decide the optimal recovery action.

## Payment Details
- Payment ID: {payment_id}
- Amount: ₹{amount_display} ({amount} paise)
- Method: {method}
- Bank: {bank}
- Card Network: {card_network}
- Card Type: {card_type}

## Failure Details
- Failure Class: {failure_class}
- Error Code: {error_code}
- Error Description: {error_description}
- Error Source: {error_source}
- Error Reason: {error_reason}
- Is Retryable: {is_retryable}

## Customer Context
- Customer ID: {customer_id}
- Retries in last 24h: {retry_count_24h}
- Nudges in last 24h: {nudge_count_24h}
- Previous retry outcomes: {previous_retry_outcomes}

## Temporal Context
- Failed at: {failed_at}
- Current time: {current_time}
- Hour of day (IST): {hour_of_day}
- Day of week: {day_of_week_name}

Respond with ONLY a JSON object matching the RetryAction schema. No other text.
"""


def format_user_prompt(context: FailureContext) -> str:
    """Format a FailureContext into the user prompt string."""
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    amount_display = f"{context.amount / 100:,.2f}"
    day_name = day_names[context.day_of_week] if 0 <= context.day_of_week <= 6 else "Unknown"

    return USER_PROMPT_TEMPLATE.format(
        payment_id=context.payment_id,
        amount=context.amount,
        amount_display=amount_display,
        method=context.method,
        bank=context.bank or "Unknown",
        card_network=context.card_network or "N/A",
        card_type=context.card_type or "N/A",
        failure_class=context.failure_class,
        error_code=context.error_code,
        error_description=context.error_description or "N/A",
        error_source=context.error_source or "N/A",
        error_reason=context.error_reason or "N/A",
        is_retryable=context.is_retryable,
        customer_id=context.customer_id or "Unknown",
        retry_count_24h=context.retry_count_24h,
        nudge_count_24h=context.nudge_count_24h,
        previous_retry_outcomes=", ".join(context.previous_retry_outcomes) or "None",
        failed_at=context.failed_at.isoformat(),
        current_time=context.current_time.isoformat(),
        hour_of_day=context.hour_of_day,
        day_of_week_name=day_name,
    )
