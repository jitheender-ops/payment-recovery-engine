import { createHash, createHmac } from "node:crypto";
import type { FailureContext } from "./actions.js";

export const SYSTEM_PROMPT = `You are a payment retry policy agent for an Indian payment gateway. Your job is to decide
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
}`;

const USER_PROMPT_TEMPLATE = `Analyze this failed payment and decide the optimal recovery action.

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

Respond with ONLY a JSON object matching the RetryAction schema. No other text.`;

export function maskCustomerId(
  customerId: string | null | undefined,
  webhookSecret: string,
): string {
  if (!customerId) return "unknown";
  const digest = createHmac("sha256", webhookSecret)
    .update(`pii-mask|customer_id|${customerId}`, "utf8")
    .digest("hex");
  return `cust_${digest.slice(0, 16)}`;
}

export function sha256Hex(input: string): string {
  return createHash("sha256").update(input, "utf8").digest("hex");
}

export function formatUserPrompt(
  context: FailureContext,
  webhookSecret: string,
): string {
  const dayNames = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const amountDisplay = (context.amount / 100).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
  });
  const dayName =
    context.dayOfWeek >= 0 && context.dayOfWeek <= 6 ? dayNames[context.dayOfWeek] : "Unknown";

  return USER_PROMPT_TEMPLATE.replaceAll("{payment_id}", context.paymentId)
    .replaceAll("{amount_display}", amountDisplay)
    .replaceAll("{amount}", String(context.amount))
    .replaceAll("{method}", context.method)
    .replaceAll("{bank}", context.bank ?? "Unknown")
    .replaceAll("{card_network}", context.cardNetwork ?? "N/A")
    .replaceAll("{card_type}", context.cardType ?? "N/A")
    .replaceAll("{failure_class}", context.failureClass)
    .replaceAll("{error_code}", context.errorCode)
    .replaceAll("{error_description}", context.errorDescription ?? "N/A")
    .replaceAll("{error_source}", context.errorSource ?? "N/A")
    .replaceAll("{error_reason}", context.errorReason ?? "N/A")
    .replaceAll("{is_retryable}", String(context.isRetryable))
    .replaceAll("{customer_id}", maskCustomerId(context.customerId, webhookSecret))
    .replaceAll("{retry_count_24h}", String(context.retryCount24h))
    .replaceAll("{nudge_count_24h}", String(context.nudgeCount24h))
    .replaceAll(
      "{previous_retry_outcomes}",
      context.previousRetryOutcomes.join(", ") || "None",
    )
    .replaceAll("{failed_at}", context.failedAt.toISOString())
    .replaceAll("{current_time}", context.currentTime.toISOString())
    .replaceAll("{hour_of_day}", String(context.hourOfDay))
    .replaceAll("{day_of_week_name}", dayName);
}
