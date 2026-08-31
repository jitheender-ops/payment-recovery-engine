# Architecture

## Design Principle: Deterministic → LLM → Deterministic

The LLM is sandwiched between two deterministic layers. This is the single most important architectural decision and the first thing a fintech panel will evaluate.

```mermaid
flowchart LR
    subgraph Deterministic["Deterministic (Pre-LLM)"]
        A[Signature Verify] --> B[Idempotency] --> C[Event Store] --> D[Error Code Classifier]
    end
    subgraph LLM["LLM Layer (Constrained)"]
        E[Policy Agent<br>Fixed Action Space]
    end
    subgraph Guard["Deterministic (Post-LLM)"]
        F[Schema Validation] --> G[Business Rules] --> H[Idempotency Key] --> I[Execute]
    end
    D --> E --> F
```

**Why this order?**
- Pre-LLM determinism handles what a regex can solve. Calling an LLM to classify `insufficient_funds` from a structured error code is wasteful and fragile.
- Post-LLM determinism is the safety net. The LLM can hallucinate, but the guardrail catches it before any money moves.

## Database Schema

`recovery_cases` is the centre, not `payment_failures`. A failed card charge, an
abandoned cart, a halted subscription, an overdue invoice and a dead mandate are
all money we might get back; they differ only in how they were detected, so they
share one case table, one attempt budget, one terminal state and one
recovered-amount column rather than five parallel pipelines. `risk_type` says
which source, `subject_ref` identifies it in that source's namespace.

The payment-rail tables hang off that as detail records — which is why
`retry_attempts.payment_failure_id` and `.payment_id` are nullable. An invoice
chase has no payment behind it.

```mermaid
erDiagram
    webhook_events ||--o{ payment_failures : triggers
    payment_failures ||--o| recovery_cases : opens
    recovery_cases ||--o{ retry_attempts : bounds
    recovery_cases ||--o{ promises_to_pay : collects
    recovery_cases ||--o{ case_events : audits
    retry_ledger ||--o{ retry_attempts : rate_limits
    risk_events ||--o| recovery_cases : opens
    ar_accounts ||--o{ ar_contacts : addresses
    ar_accounts ||--o{ ar_contact_log : escalates
    ar_accounts ||--o{ recovery_cases : groups
    recovery_cases ||--o{ case_disputes : freezes
    recovery_cases ||--o{ payment_plans : splits
    payment_plans ||--o{ plan_instalments : schedules
    plan_instalments ||--|| promises_to_pay : is_a
    recovery_cases ||--o{ voice_call_queue : dials

    recovery_cases {
        uuid id PK
        string risk_type "payment_failure|checkout_abandonment|subscription_failure|invoice_overdue|mandate_failure"
        string subject_ref UK "with risk_type"
        string customer_id
        int amount_at_risk
        int amount_recovered
        string recovered_ref "the NEW payment id, not subject_ref"
        uuid recovered_via_attempt_id "NULL = self-recovery, not ours"
        string state "open|recovered|exhausted|abandoned|expired|opted_out"
        int attempts_used
        int max_attempts
        int escalation_level
        timestamp due_at "when the money was due — receivables aging"
        timestamp next_action_at "escalation clock; NULL = webhook-driven"
        string batch_id
    }
    retry_attempts {
        uuid id PK
        uuid recovery_case_id FK
        uuid payment_failure_id "NULL for non-payment risk types"
        string payment_id "NULL for non-payment risk types"
        string idempotency_key UK
        string external_ref "Payment Link id — the attribution join key"
        string action_type
        string channel "sms|email|voice|whatsapp|payment_link"
        string language "hinglish is a real value here"
        boolean guardrail_passed
        string result
    }
    promises_to_pay {
        uuid id PK
        uuid recovery_case_id FK
        int amount_promised
        timestamp due_at
        string status "pending|kept|broken|cancelled"
        string resolved_ref
        string channel "voice|payment_link|merchant|payment_plan"
        boolean is_partial "NULL = not assessed"
        string confidence "explicit|tentative|conditional"
        string condition_note "sanitized at write; never in a prompt verbatim"
        string promised_rail
        timestamp reminded_at "one pre-due reminder per promise, ever"
        int kept_late_days "0 = on time; the honest kept-rate split"
    }
    case_events {
        int id PK
        uuid recovery_case_id FK
        string event_type "opened|contacted|escalated|promise_*|attributed|closed|opted_out|deferred|stopped"
        string actor "system|agent|customer|operator id"
        jsonb detail
        timestamp created_at
    }
    webhook_events {
        uuid id PK
        string razorpay_event_id UK
        string event_type
        jsonb payload
        boolean processed
    }
    payment_failures {
        uuid id PK
        string payment_id
        string order_id
        int amount
        string failure_class
        boolean is_retryable
        timestamp failed_at
    }
    retry_ledger {
        string customer_id UK
        int total_retries_24h
        int total_nudges_24h
        string consent_status "granted|opted_out"
        timestamp blocked_until
    }
    risk_events {
        uuid id PK
        string event_id UK
        string risk_type
        string reference_id
        string offer_id "merchant incentive, 2nd touch on"
        boolean processed
    }
    ar_accounts {
        uuid id PK
        string account_ref UK
        string display_name
    }
    ar_contacts {
        uuid id PK
        uuid account_id FK
        string role "ap_clerk|finance_manager|escalation"
        string email
        boolean active
    }
    ar_contact_log {
        uuid id PK
        uuid account_id FK
        int stage_level "which rung fired"
        jsonb case_refs "every invoice in the one statement"
        timestamp planned_for
        timestamp sent_at
    }
    case_disputes {
        uuid id PK
        uuid case_id FK
        string reason "the customer's own words"
        string status "open|resolved"
        timestamp resolved_at
    }
    payment_plans {
        uuid id PK
        uuid case_id FK
        int principal_paise "sized to the OUTSTANDING, not the opening figure"
        int settlement_paise "approved reduced payoff, if any"
        string status "active|completed|defaulted"
    }
    plan_instalments {
        uuid id PK
        uuid plan_id FK
        int seq
        int amount_paise
        uuid promise_id FK "an instalment IS a promise"
    }
    voice_call_queue {
        uuid id PK
        uuid recovery_case_id FK
        string customer_contact
        string state "queued|claimed|done"
    }
    merchant_alerts {
        uuid id PK
        string event_type
        boolean delivered
        int delivery_attempts
    }
```

**Two joins carry the design.** `plan_instalments.promise_id` is why a payment
plan needed no new state machine: an instalment IS a `promises_to_pay` row, so
the pause, the audit event and the break-on-miss all come free from the promise
sweep. And `ar_contact_log.case_refs` is a JSON array rather than a foreign key
because the row records **one contact covering many invoices** — the
consolidation guarantee is in the shape of the table.

### The three stopping rules, and where each lives

| Rule | Column | Enforced by |
|---|---|---|
| Attempt budget spent | `recovery_cases.attempts_used / max_attempts` | `stop_reason()` |
| Consent withdrawn | `retry_ledger.consent_status` | `stop_reason()`, never expires |
| Contact not yet due | `recovery_cases.next_action_at` | `stop_reason()`, expires on its own |

`next_action_at` is written by two things: the escalation backoff in
`attach_attempt()` (24h × escalation level, so the gap widens per rung) and a
promise to pay, which takes the *later* of the promise date and whatever the
ladder had already scheduled. A promise is permission to wait, not permission
to contact sooner.

A promise breaks on the clock plus a grace window (`PROMISE_GRACE_HOURS`,
default 24h) — a payment initiated on the due date can post a day late, and
breaking a kept promise is the one lie this ledger must never tell. Inside
grace, a capture keeps the promise with `kept_late_days` recording how late.

Promises are captured from three surfaces — the Hinglish voice agent (dates
resolved by a deterministic lexicon, never the LLM), the recovery page's
"promise a date" form, and `POST /risks/{type}/{ref}/promise` for merchants
who collected one on their own calls. All three enforce the horizon cap
(`PROMISE_MAX_HORIZON_DAYS`, 14 — kept rate decays with length) and the
per-case promise cap (`MAX_PROMISES_PER_CASE`, 3 — words that stopped
predicting money must not park a case forever; payment-plan instalments are
exempt as a validated set). The 48h pre-due reminder is sweep #9 in the
scheduler and spends a real contact slot through the chase pipeline — a
promise buys silence for the chase, never a free lane to remind from.

A broken promise pulls `next_action_at` back to *now* rather than NULL —
`due_cases()` skips NULL because NULL means "a webhook is this case's trigger",
so clearing it would release the case into a queue nothing reads.

## Latency Budget

| Stage | Target | Notes |
|-------|--------|-------|
| Signature verification | <1ms | HMAC-SHA256 |
| Idempotency check | <5ms | DB lookup with index |
| Classification | <1ms | In-memory YAML lookup |
| LLM policy agent | <3s | Anthropic API call |
| Guardrail validation | <1ms | Pure Python rule checks |
| Nudge generation | <3s | LLM call with 3s timeout |
| Razorpay API call | <2s | Payment Link creation |
| **Total** | **<10s** | Async — webhook returns 200 immediately |

The webhook endpoint returns 200 OK in <10ms. All processing happens asynchronously in a background task.
