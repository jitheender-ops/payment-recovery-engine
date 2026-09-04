# Scaling — capacity math and the upgrade path

What carries at what user count, what to change, and in what order. Written
before any of it is needed, per this codebase's rule that the plan is on
paper before the pressure is on.

---

## 1. Capacity math — the load is smaller than it sounds

10M users · 10% daily payment failures ≈ **1M webhooks/day**:

| Quantity | Value |
|---|---|
| Average webhook rate | ~12/s |
| Peak (diwali-sale-shaped bursts) | 300–500/s |
| Active recovery cases/day | ~1M open, ~10M rows/yr in the detail tables |
| Decision cost | XGBoost: microseconds, in-process, free — the eval already proved it beats the LLM, so the default policy is the scalable one |

A few API replicas over Postgres handle the request path comfortably. The
constraints are elsewhere — below, in hit order.

## 2. What already scales (do not touch)

- **Database-as-queue.** Webhooks, retries, voice calls, call-task claims
  are rows claimed by conditional `UPDATE … WHERE state='queued'
  RETURNING` — the database picks the winner, so workers can race safely
  and replicas can be added freely. This is the property everything else
  leans on.
- **Per-customer `SELECT … FOR UPDATE` ledger locks.** One customer's
  writes serialize; a million customers are a million independent locks.
  No global contention point. (Caveat: breaks behind a transaction-mode
  pooler — set `DB_BEHIND_POOLER=true`, see DEPLOY.md.)
- **Idempotency at the boundary.** `processed_events` UNIQUE +
  per-attempt idem keys mean redelivery storms cost nothing twice.
- **Stateless money path.** Signature → parse → insert. No session
  affinity required anywhere on the webhook path.

The write-ahead ordering in `orchestrator._execute_and_record`, the
guardrail's check-everything discipline, and the attribution rules are
correctness properties first and performance shapes second. They get
bigger instances; they do not get redesigned under load.

## 3. The mechanisms, in the order you hit them

### Phase 1 — sweep throughput (config only)

The scheduler's `scheduler_batch_size=50` / `interval=60s` sizes a
prototype: ~70–100k active cases/day. It fails politely — cases sit
past `next_action_at`, nothing errors, recovery goes late.

**Fix (env, no code):** `SCHEDULER_BATCH_SIZE=500`,
`SCHEDULER_INTERVAL_SECONDS=10` ≈ 100×. The sweeps already budget
their tick time and are safe to race each other — conditional-update
claims make overlapping workers correct by construction.

**Deployment shape:** one worker deployment with `SCHEDULER_ENABLED=true`
(no public URL), N API replicas with it `false`. The knob exists today.

Carries to roughly ~500k users.

### Phase 2 — shared rate limiting (code, shipped)

Three limiters (customer page, voice webhook, Plivo bridge) were
in-process dicts — correct only at `WEB_CONCURRENCY=1`, which render.yaml
pins *because* of them. That pin quietly prevented scaling the API tier.

**Now:** `src/rate_limit.py` — one fixed-window limiter. `REDIS_URL` unset
keeps in-process behavior (dev/demo/tests unchanged); set it and every
limiter switches to atomic `INCR`+`EXPIRE`, shared across all workers. A
Redis that dies at runtime degrades to per-process counting, never to an
open page: a limiter outage must not become a product outage.

Set `REDIS_URL` the day the API goes multi-replica. (Any managed Redis;
~0.1ms/op.)

Carries to ~5M users alongside Phase 1.

### Phase 3 — dedup-table hygiene (code, shipped)

`processed_events` rows exist only to collide on the UNIQUE index, and
Razorpay redelivers for at most a day. At millions of webhooks/year the
hottest index in the system fills with dead weight.

**Now:** the tick prunes rows past `PROCESSED_EVENTS_RETENTION_DAYS`
(default 30, `0` disables), bounded by the batch size, and reports the
count in the tick line — a retention policy nobody can see is one nobody
trusts. `webhook_events` (the replay log) stays append-only, untouched.

### Phase 4 — audit re-anchoring (code, shipped)

`case_events` is one global hash chain — tamper-evident by construction,
and O(all history) to verify, which at millions of events becomes a
long job.

**Now:** `src/audit_checkpoint.py` + the `audit_checkpoints` table
(migration 0014). Every `AUDIT_CHECKPOINT_INTERVAL_EVENTS` (default 5000)
stamped events, the tick anchors an epoch: it re-verifies that stretch
from content, then stores (boundary id, chain head, keyed signature).
Verification (`verify_chain_epoch`) recomputes only the post-checkpoint
tail, checks every epoch's signature, and — amortized — fully recomputes
the oldest not-yet-re-verified epoch per run. The amortization is the
difference between a fast verifier that trusts stored hashes (my first
version — a rewrite that leaves old hashes in place passed the signature
check; a test caught it) and one that re-reads content on rotation.

Tamper-evidence is unchanged: rewriting an epoch's content breaks its
recomputed head → signature mismatch; forging a checkpoint requires
`AUDIT_CHAIN_SECRET`; the tail is recomputed every run.

### Phase 5 — the parts that are deployment, not code

- **Voice bridge state.** Call state (`_CALLS`) and TTS audio are
  process-local; a load balancer round-robining Plivo's callbacks to the
  wrong replica ends the call mid-sentence. Until real call volume:
  one bridge process per Plivo number (N numbers, N URLs). At 50+
  concurrent calls: externalize state to Redis + audio to object storage
  with presigned GETs, or sticky-route on the `call_uuid` the bridge
  already signs into every action URL.
- **Postgres layout.** Partition `case_events` by month (`PARTITION BY
  RANGE (created_at)` — queries already filter by case or recency);
  keep `processed_events` pruned (Phase 3); point the console's
  aggregate panels at a read replica (they are read-only queries —
  one session factory, one connection string).
- **The LLM path.** Optional by design and the eval says XGBoost wins
  anyway. At volume the LLM surface (nudge rephrasing) degrades to
  templates automatically. Do not scale LLM inference for the decision
  path — there is nothing to scale; XGBoost already is the scale story.

## 4. Sequencing

| Users | What's needed |
|---|---|
| ≤ ~100k | today's deployment, paid Postgres (the 30-day free tier deletes data — see DEPLOY.md) |
| ~100k–500k | Phase 1 env tuning |
| ~500k–5M | + `REDIS_URL`, split worker/API deployments |
| 5M+ | + voice state externalization, `case_events` partitioning, read replica; Phases 3–4 already shipped |

## 5. What was deliberately left out

Queue-for-queues (Kafka/Celery) — the database is already a correct
queue with exactly-once claims, and the failure mode of "add Kafka" is
two systems that can disagree about whether work happened. Microservices
split — every boundary the codebase has (ingestion/decision/execution)
is already a module seam; process boundaries can follow the same seams
if a team split ever demands them. Neither is a scale requirement at any
number in this document.

---

## 6. Frontend posture (shipped)

The HTML surfaces' hardening that shipped alongside the scale mechanisms:

- **Headers per surface** (`src/main.py` middleware): the money pages
  (`/recover`, `/statement`) carry DENY + CSP frame-ancestors + no-store +
  no-referrer; the operator surfaces (`/console`, `/voice/demo`,
  `/foundation`) carry DENY + CSP + no-store; every HTML response carries
  `X-Content-Type-Options: nosniff`. The JSON API is untouched.
- **`/foundation`** — the scroll-told product story (the public front door;
  `/` redirects to it). Dark ground, one idea per screen, every number from
  the eval harness. Progressive enhancement: fully readable without JS.
- **Link previews** — og:/twitter: tags on the customer money page and the
  landing, backed by `/static/og-card.svg`. The recovery link travels in
  SMS; an unfurled merchant-named card is the trust signal that earns the tap.
- **Favicon** — `/favicon.svg` (inline SVG mark; no 404 noise).
- **Print receipts** — `@media print` on the customer page: money facts and
  reference, no interactive chrome.
