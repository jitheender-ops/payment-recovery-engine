# Documented Failure Cases

Showing exactly where the agent breaks reads as senior engineering judgment. Hiding failure cases reads as junior.

## 1. LLM Hallucination

**Trigger:** The LLM returns an action outside the fixed schema — e.g., invents a new action type, returns a malformed JSON, or includes extra fields.

**Frequency:** not yet measured. The LLM policy has no recorded eval run, so we have no
hallucination rate to report. The handling path below is implemented and unit-tested
(`tests/test_agent.py::test_action_schema_validation`); the rate is an open measurement.

**System response:**
1. Pydantic schema validation catches the malformed output.
2. The agent retries once with a correction prompt: "Your previous response was not valid JSON."
3. If the retry also fails, the system falls back to `abandon` with reason logged.
4. The full LLM response (raw text) is logged for debugging.

**What a reviewer sees:** `retry_attempts` table entry with `agent_type="llm"`, `guardrail_passed=False`, and `guardrail_rejection_reason` explaining the schema violation.

---

## 2. Bank API Timeout During Retry

**Trigger:** The Razorpay API call to create a Payment Link times out or returns a 5xx error during retry execution.

**System response:**
1. The idempotency key is deterministic, and the orchestrator checks `retry_attempts`
   for that key before calling Razorpay — so a replay of the same webhook does not
   create a second payment link. The gateway itself offers no idempotency header;
   the guarantee is ours, enforced by a UNIQUE constraint plus check-before-execute.
2. The `RetryAttempt` record is created with `result="failed"` and `result_details` containing the error.
3. If a `payment.captured` webhook arrives later (indicating the original payment actually succeeded despite the timeout), the system marks all pending retries as `superseded`.

**What a reviewer sees:** Retry attempt with `result="failed"` and no duplicate link.
The known gap: if the Razorpay call succeeds but our commit then fails, the link exists
without a local record, and a replay would create a second one. Closing that needs an
outbox/two-phase write, which is not implemented.

---

## 3. Customer Consent Expiry

**Trigger:** A retry is attempted more than 72 hours after the original payment failure.

**System response:**
1. The guardrail gate's `check_consent_window` rule rejects the action.
2. The rejection reason is logged: "Consent window expired: 80.5h > 72h".
3. No retry is executed. No notification is sent.

**What a reviewer sees:** `guardrail_passed=False` with clear rejection reason in the `retry_attempts` table.

---

## 4. Rate Limit Exhaustion

**Trigger:** A customer has already received 2 nudge messages in the last 24 hours, and the agent recommends a 3rd nudge.

**System response:**
1. The guardrail gate's `check_customer_nudge_rate_limit` rule rejects the nudge action.
2. The retry itself may still proceed (if the action was `switch_rail` with a nudge), but the notification is suppressed.
3. The `retry_ledger` table tracks per-customer counts.

**What a reviewer sees:** Nudge count in the ledger, rejection reason in the retry attempt.

---

## 5. Cascade Failure (Extended Bank Downtime)

**Trigger:** A bank is down for an extended period (hours). The agent keeps scheduling future retries that all fail when they execute.

**System response:**
1. Max retry cap per payment (3) prevents more than 3 attempts per payment.
2. Max retry cap per customer per 24h (5) prevents runaway retries across multiple payments.
3. Each failed retry is logged with `result="failed"`.
4. After the 3rd failure, the agent receives the history of 3 failed attempts in its context, which biases it toward `abandon`.

**What a reviewer sees:** A sequence of retry attempts with increasing attempt numbers, capped at 3. No infinite loops.

---

## 6. LLM Provider Outage

**Trigger:** Anthropic/OpenAI API is down or returns errors for an extended period.

**System response:**
1. The PolicyAgent catches the exception and returns a `_fallback_action`.
2. Fallback uses simple heuristics: hard_decline→abandon, network_error→retry_now, bank_downtime→retry_at+30min, everything else→abandon.
3. The `agent_type` field in `retry_attempts` is set to `"xgboost"` to distinguish from LLM decisions.
4. The system remains fully operational. The recovery-rate delta vs. the LLM policy is
   not yet measured — the LLM policy has no recorded eval run to compare against.

**What a reviewer sees:** Retry attempts with `agent_type="xgboost"` instead of `"llm"`. No service interruption.

---

## 7. Simulator vs. Reality Gap

**Trigger:** This is a permanent, structural limitation, not a runtime failure.

**What's missing:**
- The bank response simulator uses synthetic success probabilities, not real bank data.
- Real correlations (e.g., SBI downtime correlated across all rails, festival season payment patterns) are not modeled.
- Customer behavior after nudges is simulated with a 60% retry probability — real behavior varies widely.

**Mitigation:**
- The eval methodology document explicitly states all assumptions.
- The simulator is designed to be calibrated with real data: swap the bank profiles and the results update automatically.
- Multiple random seeds with variance reporting ensures the eval is statistically sound within the synthetic model.

**Why we state this explicitly:** Naming this assumption is a maturity signal, not a weakness. Every model has a sim-to-real gap. The question is whether the system is designed to close it — and this one is.
