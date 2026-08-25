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

---

## 8. Process Dies Before a BackgroundTask Runs

**Trigger:** Restart, crash or deploy between the webhook router's 200 and the background task finishing. Razorpay will not re-send after a 200.

**System response:**
1. The router commits the event to `webhook_events` BEFORE returning 200, so it is durably stored even if processing never starts.
2. The scheduler's `reconcile_events()` sweep re-runs `payment.failed` events still `processed=False` past an age threshold (`event_reconcile_after_seconds`).
3. Events whose payload raises every tick get `processing_error` set so they cannot starve the batch.

**What a reviewer sees:** A reconciled event in the logs; the payment gets its recovery attempt on the next tick instead of never.

---

## 9. Consent Withdrawn During a Deferred Wait

**Trigger:** A `retry_at` approved at 22:00; the customer opts out at 23:00; the retry is due at 02:00.

**System response:**
1. `record_opt_out()` closes all open cases for the customer immediately.
2. When the scheduler fires the deferred attempt, it re-reads case state and consent status at fire time.
3. The scheduled attempt is marked `cancelled` with the reason, and a `stopped` audit event records who stopped it.

**What a reviewer sees:** Attempt row with `result="cancelled"`, reason `"customer opted out of contact"` — not the 02:00 message a complaint would be about.

---

## 10. Two Webhooks Race the Same Idempotency Key

**Trigger:** Duplicate delivery of one payment failure; both workers count attempts, derive the same key, pass check-before-execute.

**System response:**
1. The UNIQUE constraint on `retry_attempts.idempotency_key` breaks the tie BEFORE Razorpay is called.
2. The loser catches the IntegrityError, rolls back and exits as a clean skip.
3. The write-ahead commit ordering means the winner's row was already durable before its API call.

**What a reviewer sees:** One attempt row per key ever. No second Payment Link.

---

## 11. Process Dies Mid-Execution (Stale Pending Attempts)

**Trigger:** The write-ahead intent row is committed as `result="pending"`, then the process dies before the executor records the outcome. Nothing downstream resolves a pending row unless money arrives (`attribute_capture` supersedes it).

**System response:**
1. The scheduler's `reconcile_stale_attempts()` sweep claims pending rows older than `attempt_stale_after_seconds` (default 900s — the executor's own timeout bounds how long a live call can hold one) with a conditional UPDATE, so it cannot race an execution that just resolved them.
2. The row becomes `result="failed"` with `result_details.scheduler = "stale-pending: no outcome … outcome unknown (fail-closed)"`, and a `reconciled` case event records it.
3. Fail-closed by construction: the slot stays spent (a link MIGHT exist), but attribution still works — matching reads the idempotency-key breadcrumb, never `result`.

**What a reviewer sees:** The dashboard "In flight" tile counts only genuinely unresolved work; lost attempts are visible as failed-with-reason instead of pending-forever.
