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

```mermaid
erDiagram
    webhook_events ||--o{ payment_failures : triggers
    payment_failures ||--o{ retry_attempts : generates
    retry_ledger ||--o{ retry_attempts : rate_limits

    webhook_events {
        uuid id PK
        string razorpay_event_id UK
        string event_type
        jsonb payload
        timestamp received_at
        boolean processed
    }
    payment_failures {
        uuid id PK
        string payment_id
        int amount
        string method
        string failure_class
        boolean is_retryable
        timestamp failed_at
    }
    retry_attempts {
        uuid id PK
        string payment_id
        string idempotency_key UK
        string action_type
        string agent_type
        boolean guardrail_passed
        string result
        timestamp created_at
    }
    retry_ledger {
        string customer_id UK
        int total_retries_24h
        int total_nudges_24h
    }
```

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
