# The Payment Recovery Engine — Complete Working Document

One document that explains the whole system as it exists today: what it
does, how it is structured, how money and information flow through it,
every surface it exposes to the internet and how each is protected, and
how it deploys. Written for an engineer or reviewer seeing the project
for the first time; `docs/ARCHITECTURE_MAP.md` remains the file-by-file
index and `docs/architecture.md` the design principles.

---

## 1. What this system is

Indian checkout success rates sit in the high 80s. Every failed payment,
abandoned cart, dead mandate, unpaid subscription charge and overdue
invoice is money that leaked. This engine watches for those events,
decides intelligently **whether**, **when**, and **on which rail** to
chase the money, executes through Razorpay Payment Links and a
multi-channel chaser (SMS / recovery page / Hinglish voice call), and
**listens to the reply** — a promise to pay, an instalment plan, a
dispute, an opt-out.

Two claims distinguish it from a cron job with an LLM bolted on:

1. **No LLM ever directly authorizes money movement.** The LLM is
   sandwiched between deterministic layers (classifier before,
   guardrail after). Every action is schema-constrained, every rupee
   amount the system ever states is grounded in a database row, and
   every executed action is idempotent and audited.
2. **The improvement is measured, not asserted.** The eval harness
   (`eval/runner.py`, 5,000 scenarios × 5 seeds, common random numbers)
   shows +11pp recovery over fixed 3-retry at *fewer* attempts, with
   confidence intervals on the difference. XGBoost wins over the LLM
   agent and the README says so plainly.

### The five risk types — one pipeline

A failed card charge, an abandoned checkout, a failed mandate debit, an
unpaid invoice and a halted subscription are all "money we might get
back". They differ only in how they were *detected*, so they share one
case table, one attempt budget, one terminal state machine:

| risk_type | enters via | typical chase |
|---|---|---|
| `payment_failure` | Razorpay webhook (`payment.failed`) | retry / switch rail / nudge |
| `checkout_abandonment` | `POST /risks` (merchant push) | cart chaser cadence |
| `subscription_failure` | `POST /risks` | nudge + link |
| `mandate_failure` | `POST /risks` | pre-debit notice (RBI), then retry |
| `invoice_overdue` | `POST /risks` | B2B ladder by person, not volume |

---

## 2. The pipeline — webhook to recovered rupee

```
                    ┌────────────────────────────────────────────────┐
                    │  LAYER 1 — INGESTION (src/ingestion/)          │
                    │  HMAC verify → dedup → append-only store       │
                    └───────────────┬────────────────────────────────┘
                                    ▼
                    ┌────────────────────────────────────────────────┐
                    │  LAYER 2 — CLASSIFIER (src/classifier/)        │
                    │  error-code table first; LLM only for UNKNOWN  │
                    └───────────────┬────────────────────────────────┘
                                    ▼
                    ┌────────────────────────────────────────────────┐
                    │  LAYER 3 — DECISION (src/agent/)               │
                    │  XGBoost baseline OR LLM → fixed action space   │
                    └───────────────┬────────────────────────────────┘
                                    ▼
                    ┌────────────────────────────────────────────────┐
                    │  LAYER 4 — GUARDRAIL (src/guardrail/)          │
                    │  ~12 deterministic rules, ALL checked, no      │
                    │  short-circuit; violations all reported        │
                    └───────────────┬────────────────────────────────┘
                                    ▼
                    ┌────────────────────────────────────────────────┐
                    │  LAYER 5 — EXECUTION (src/executor/)           │
                    │  Razorpay Payment Link (idempotent, bounded)   │
                    │  or nudge / voice queue — never raw charges    │
                    └───────────────┬────────────────────────────────┘
                                    ▼
                    ┌────────────────────────────────────────────────┐
                    │  ATTRIBUTION (src/cases.py)                    │
                    │  capture webhook → match → credit the case;    │
                    │  replay refusal via credited_refs membership   │
                    └────────────────────────────────────────────────┘
```

**The money-safety invariants** (each pinned by tests):

- **Write-ahead ordering** — `orchestrator.py::_execute_and_record`
  records the attempt intent before the live Razorpay call and the
  outcome after; the order is a correctness property, never reorder.
- **Idempotency everywhere** — webhook events deduped by
  `processed_events` UNIQUE constraint (idless events keyed on a
  SHA-256 of the raw body); retries keyed
  `(payment_id, attempt_count)`; the executor carries the key to
  Razorpay.
- **Fail-closed secrets** — an unset secret closes its surface. The
  webhook refuses every delivery; `/risks` rejects every event; the
  recovery page refuses every token; the voice bridge refuses to run.
- **Opt-out is a never-rule** — any phrasing, any channel ("band karo"
  on a phone, a page click, an SMS reply) closes every open case for
  that customer and no later touch re-opens them.
- **Grounded speech** — the voice agent may only state numbers that
  exist verbatim in a retrieved passage or case-fact string; the
  numeric grounding gate abstains otherwise.
- **Audit hash chain** — `case_events` is hash-chained
  (`src/audit_chain.py`); `scripts/audit_chain.py --verify` recomputes
  the whole chain and names tampering.

### The scheduler (`src/scheduler.py`)

An in-process asyncio loop (no Celery, no Redis — deliberate for the
deployment size) that sweeps every 60s: fire due `retry_at` attempts
(re-running the guardrail at fire time), reconcile webhook events whose
background task died, resolve stale pending attempts fail-closed, expire
broken promises, chase due cases on the per-type ladder, and cancel dead
payment links. On Render's free plan the loop sleeps with the service —
nothing is lost, only punctuality.

---

## 3. Listening to the reply — the chaser surfaces

### The customer recovery page (`src/customer/`)

A signed, expiring, single-case token URL (`src/recovery_link.py`) — the
URL itself is the credential. States: payable → confirming →
recovered / expired / opted-out, with per-failure-class Hinglish/English
explanations. Rate-limited per IP (page views vs. payment starts are
budgeted separately). A "Pay" mints a *fresh* Razorpay Payment Link for
the outstanding amount only — never a raw charge.

### Promises, plans and disputes (`src/cases.py`, `src/receivables/`)

A promise ("kal tak bhej dunga") silences the case until its date; one
pre-due reminder, ever. Instalment plans are promises with structure;
each instalment is its own promise with its own kept/broken verdict. A
dispute freezes the case and escalates to a human — the engine never
argues with a customer who says the invoice is wrong.

### The Hinglish voice agent (`src/voice/`)

Four deterministic gates before any generation: opt-out lexicon (both
scripts) → injection refusal (transcript is attacker text) → promise
capture (deterministic date/amount parsing, spoken double-loop
confirmation) → retrieval + numeric grounding. Replies are assembled
extractively from a Hinglish FAQ corpus + case facts, with an opt-in
LLM rephrase path that the same grounding gate still governs.

**The call leg** (`src/voice/plivo_bridge.py`, `docs/VOICE_CALL_SETUP.md`):
the engine never dials. It queues `voice_call_queue` rows after a
successful nudge; a bridge worker claims them via
`POST /voice/queue/claim` (HMAC-signed), dials through **Plivo**, and
serves the XML callbacks Plivo fetches during the call:
`/plivo/answer` (AI-disclosure greeting + GetInput) →
`/plivo/turn` (download recording → **Sarvam saaras:v3** STT → signed
`POST /voice/turn` → **Sarvam bulbul:v3** TTS → `<Play>`) →
`/plivo/hangup` (outcome reported to the queue, audio deleted).
Opt-out, promise capture or 3 abstains end the call; a 12-turn cap and
silence nudge keep any customer from being trapped.

### The merchant console (`/console`)

Password-gated live console: recovery funnel, case timeline with the
full decision chain (agent → guardrail → execution, with rejection
reasons), receivables aging, downtime status, batch recovery planning,
audit chain browser. The landing page renders product facts only — no
live numbers without the password. Sign-in throttle is per-client
bucketed (an attacker burning guesses locks only their own bucket).

---

## 4. Every internet surface and its authentication

| Surface | Auth | Additional bounds |
|---|---|---|
| `POST /webhooks/razorpay` | HMAC over raw body (`X-Razorpay-Signature`) | optional IP allowlist, 16KB body cap, event dedup |
| `POST /risks` | HMAC (`X-Risk-Signature`) | per-IP rate limit, event dedup |
| `POST /ar/*` (receivables API) | HMAC, same secret family as /risks | dispute/verdict writes only |
| `GET /recover/<token>` + `POST` actions | signed expiring single-case token | per-IP rate limit, security headers |
| `GET/POST /console/*` | password → signed session cookie (httponly, samesite=lax, secure) | per-client lockout buckets |
| `POST /voice/turn` | HMAC (`X-Voice-Signature`, dedicated secret) | per-IP rate limit |
| `POST /voice/queue/claim` · `/report` | same HMAC | claim is a conditional UPDATE (no double-claim) |
| `POST /plivo/answer·turn·hangup` | HMAC over raw body + per-call turn signature | per-IP rate limit |
| `GET /plivo/audio/<call_uuid>/<n>.wav` | none (Plivo fetches GET) | exists only while the call is live, unguessable id, `<digits>.wav` shape policed, no-store |
| `GET /health` | none | says only "up" |
| `/docs` `/redoc` | open in `development` only | schema behind `X-API-Key` otherwise |
| `GET /voice/demo` + STT/turn | none (local validation surface) | binds no case → grounding gate has no amounts to speak |

Fail-closed rule throughout: **an unset secret means the surface
refuses everything** — not that it accepts everything.

---

## 5. Data model — the centre is the case

`recovery_cases` is the hub (`docs/architecture.md` has the full ER
diagram): `risk_type` + `subject_ref` identify it, `customer_id` binds
every case for one human to one identity (email → phone → merchant-id
canonical key), `amount_at_risk` / `amount_recovered` /
`credited_refs` (every capture ever credited — the replay firewall)
track the money, `attempts_used/max_attempts` the budget,
`state` the terminal machine (open → recovered | exhausted | abandoned |
expired | opted_out). Detail records hang off it: `payment_failures`
(rail-specific), `retry_attempts` (one per action, idempotency-keyed,
carries the guardrail verdict and the Payment Link id — the
attribution join key), `promises_to_pay` (kept/broken ledger that feeds
the next decision), `case_events` (hash-chained audit),
`voice_call_queue` (the dial intent, written-ahead like every action).

Fifteen migrations, every one inspector-guarded and idempotent —
`alembic upgrade head` runs on every container boot and is a no-op on a
current schema. `scripts/check_migrations.py` proves the chain builds
exactly the ORM's schema (empty→head, head→base→head, create_all→head),
on SQLite by default and on Postgres with `--postgres <url>` — the second
dialect because two migrations passed the SQLite run and then died on the
first deploy, on SQL only Postgres type-checks.

---

## 6. Decision quality — the eval harness

`eval/runner.py` runs 5,000 scenarios × 5 seeds under **common random
numbers** (the same scenario draws the identical random sequence for
every policy, outcomes differenced pairwise) against a bank simulator
whose central assumption is *a retry is not a fresh payment*
(`P(blocker cleared)` per failure class, `eval/bank_profiles.py`).
Blended baseline lands in the published 15–30% band and
`tests/test_calibration.py` fails if it drifts out. Headline (net of
₹2/attempt): **XGBoost +11.03pp recovery [+10.54, +11.52] at −0.41
attempts vs. fixed 3-retry; the LLM agent loses on every axis** — kept
in the harness as the honest negative result. The harness refuses to
fill the LLM row when fallback decisions dominate it.

The XGBoost baseline ships **inside the Docker image** (trained at
build, SHA-256 pinnable via `XGBOOST_MODEL_SHA256` — joblib is pickle,
and an unpinned pickle is unvetted code).

---

## 7. Deployment — one command locally, one blueprint in the cloud

### Local, zero credentials

```bash
./run.sh --demo        # SQLite + fake gateway + seeded book, offline
./run.sh --demo --scale# thousands of cases — the batch story
./run.sh --sandbox     # real Razorpay test keys, real API, SQLite
./run.sh               # full stack + public tunnel for webhooks
```

### Render (the deployed working model)

`render.yaml` is the whole cloud: one web service (`recovery-api`,
Docker, free plan) + one Postgres. The Docker image is two-stage,
non-root, trains the XGBoost model into the build, and its entrypoint
runs `alembic upgrade head` before uvicorn. Secrets are `sync: false`
(prompted in the dashboard, never in git) or `generateValue` (Render
mints them). `APP_ENV=staging` closes the docs; `BEHIND_TRUSTED_PROXY`
+ `TRUSTED_PROXY_HOPS` make the rate limiters key on the real client.
Free-plan caveats are documented in the blueprint itself: the service
sleeps (scheduler punctuality, not correctness), and free Postgres is
**deleted after 30 days** — the upgrade is one plan line.

The Plivo bridge runs as the same service's endpoints (`/plivo/*` are
mounted in the app) with its worker loop (`scripts/run_plivo_bridge.py`)
run wherever a public URL reaches the service; every `PLIVO_*` var is
optional and fail-closed, so a deployment without voice is exactly as
safe as before.

### CI (`.github/workflows/ci.yml`)

Five jobs on every push/PR: ruff + mypy --strict + pytest on 3.11/3.13,
the migration-chain check, the eval reproduction + calibration guard,
semgrep SAST (`--error`, suppressions carry written rationale), gitleaks
over full history, and **pip-audit for dependency CVEs**. Dependabot
watches pip + actions daily/weekly.

---

## 8. What is deliberately NOT here

- No Redis/Celery — the queue IS the database, sweeps are conditional
  UPDATEs, the scheduler is one asyncio loop. Right at this scale;
  documented where it stops being right.
- No streaming voice — GetInput round-trips keep the bridge stateless
  and provider-standard. The realtime upgrade path (saaras:v3-realtime)
  is noted in the voice TODO.
- No LLM-authored outbound message to customers — nudges are
  template-rendered; the LLM only ever sees sanitized, PII-masked
  context and its output is a constrained JSON action, not prose sent
  to a human.
- No silent capability degradation — a missing model, a dead LLM, an
  unconfigured secret each log loudly and fall back to the *more
  conservative* path, and tests pin each of those falls.

---

## 9. Verification — the contract for any change

```bash
.venv/bin/ruff check src eval scripts tests
.venv/bin/mypy --strict src scripts eval
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_migrations.py
```

All four green is the definition of "done" (AGENTS.md). As of this
document: **885 tests passing, ruff clean, mypy --strict clean across
96 files, migration chain consistent on both SQLite and Postgres, semgrep
+ gitleaks clean in CI**.

---

## 10. Reading order for a new session

1. `README.md` — the pitch, the numbers, the honest negatives
2. this document — the system as a whole
3. `docs/architecture.md` — design principles + ER diagram
4. `docs/ARCHITECTURE_MAP.md` — which file to open
5. `docs/VOICE_CALL_SETUP.md` — the voice call runbook
6. `docs/DEPLOY.md` — the Render path, incl. the 30-day catch
7. `policy.yaml` — the product's bounds in human-readable form
