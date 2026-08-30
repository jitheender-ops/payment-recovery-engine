# Codebase map — where everything lives

One page, top-down. Read `docs/architecture.md` for the *why* (design
principles, ER diagram); this file is the *where* — for a new contributor
or a new session, the answer to "which file do I open?"

```
payment-recovery-engine/
│
├── src/                       THE ENGINE — one package per pipeline layer
│   │
│   │ ── Layer 1: INGESTION — two doors in, one pipeline behind them ──
│   ├── ingestion/             webhook + merchant-risk rails
│   │   ├── router.py         Razorpay webhooks: signature → store → classify
│   │   │                     → hand to orchestrator. Also: attribute_captured_
│   │   │                     payload (capture → case credit), rearm()
│   │   ├── risk_router.py    /risks: merchant-pushed events (cart, invoice,
│   │   │                     subscription, mandate) — same discipline
│   │   ├── signature.py      HMAC verify, body-size cap (fail-closed)
│   │   └── idempotency.py    event dedup via processed_events UNIQUE insert
│   │
│   │ ── Layer 2: CLASSIFIER — deterministic first, LLM only for the tail ──
│   ├── classifier/
│   │   ├── mapper.py         error-code → FailureClass lookup table
│   │   ├── taxonomy.py       FailureClass enum + retryable/hard-decline sets
│   │   ├── error_codes.yaml  the mapping data itself
│   │   └── llm_tail.py       opt-in LLM for UNKNOWN codes only (off by default)
│   │
│   │ ── Layer 3: DECISION — the LLM sandwich's middle ──
│   ├── agent/
│   │   ├── actions.py        RetryAction / FailureContext pydantic contracts
│   │   ├── policy_agent.py   the LLM decision caller (constrained JSON)
│   │   ├── prompts.py        system + user prompt builders, PII sanitizer
│   │   └── xgboost_baseline.py  the fallback/heuristic model
│   │
│   │ ── Layer 4: GUARDRAIL — deterministic, post-LLM, pre-money ──
│   ├── guardrail/
│   │   ├── gate.py           rule orchestration — ALL rules, no short-circuit
│   │   ├── rules.py          the business rules themselves
│   │   └── schemas.py        action-shape validation
│   │
│   │ ── Layer 5: EXECUTION — the only code that touches money ──
│   ├── executor/
│   │   ├── retry_executor.py Razorpay Payment Links + notify (idempotent,
│   │   │                     off-event-loop, timeout-session)
│   │   └── rail_selector.py  UPI/card rail resolution
│   │
│   │ ── ORCHESTRATION — ties the five layers together ──
│   ├── orchestrator.py       the pipeline (see _execute_and_record for the
│   │                         write-ahead ordering — a money-safety property,
│   │                         never reorder)
│   ├── scheduler.py          background sweeps: fire due retries, reconcile
│   │                         dropped events/stale attempts, cancel dead links,
│   │                         chase due cases (asyncio task, not Celery)
│   ├── cases.py              recovery-case lifecycle: open/stop/attribute/
│   │                         close, customer identity keys, promises to pay
│   ├── chasers/policy.py     per-risk-type chase policy (budget, cadence, rail)
│   ├── models.py             SQLAlchemy models — recovery_cases is the centre
│   ├── audit_chain.py        hash-chained case_events audit trail
│   ├── llm.py                ONE shared LLM-client builder (all 3 callers)
│   ├── recovery_link.py      HMAC-signed self-serve recovery links
│   ├── config.py             Settings + secret reveal(); every knob lives here
│   ├── database.py           engine/session factory
│   ├── auth.py               API-key middleware
│   ├── formatting.py         ₹ money + IST formatting
│   ├── main.py               FastAPI app assembly + lifespan
│   │
│   │ ── SURFACES ──
│   ├── customer/             the recovery page the customer lands on
│   │   ├── routes.py         state machine: payable/confirming/expired/opted-out
│   │   ├── i18n.py           en/hi catalogs + language pick
│   │   ├── explain.py        per-failure-class customer-facing explanations
│   │   └── templates/        recover.html, expired.html, base.html
│   ├── merchant/             the operator console (login-gated)
│   │   ├── routes.py         live console data, recovery funnel, link admin
│   │   └── templates/        landing, live, login
│   │
│   │ ── NEWER MODULES (in active development) ──
│   ├── voice/                Hinglish voice-agent webhook: retrieval-grounded
│   │                         answers, injection refusal, opt-out (WIP)
│   └── receivables/          receivables/disputes module (WIP)
│
├── alembic/versions/         9 migrations — 0000 schema → 0007 audit hash chain
│
├── tests/                    one test file per module, ~460 tests
│   └── conftest.py           SQLite async harness + fixtures
│
├── eval/                     offline policy comparison (the README's numbers)
│   ├── runner.py             5,000-scenario × 5-seed harness, CRN + CI
│   ├── policies/             no_retry / fixed_retry / xgboost / llm
│   └── results/              committed results (README cites them as evidence)
│
├── dashboard/                Streamlit ops dashboard (separate from merchant/)
│
├── scripts/                 ops tools: simulate_webhooks, train_xgboost,
│                             seed_error_codes, audit_chain verify, etc.
│
├── models/xgboost_baseline.joblib   trained fallback model (gitignored)
│
├── docs/                     EVERYTHING written, one root
│   ├── architecture.md       design principles + ER diagram  ← START HERE
│   ├── ARCHITECTURE_MAP.md   this file
│   ├── failure_cases.md      the failure taxonomy, with examples
│   ├── eval_methodology.md   how the headline numbers were produced
│   ├── WEBHOOKS_RUNBOOK.pdf  webhook operations
│   ├── SESSION-2026-08-28-security-audit.md
│   ├── Payment_Failure_Recovery_Engine_Brief.pdf   the original brief
│   ├── specs/                implementation contracts
│   │   ├── customer-recovery-page-spec.md
│   │   ├── 2026-08-25-recovery-page-polish-design.md
│   │   └── checkout-dropoff-recovery-plan.md      (PLAN — not yet implemented)
│   └── design-system/        visual design rules
│       ├── recovery-console/MASTER.md     the console's design world (v2)
│       └── pages/customer-recovery.md     overrides MASTER for the customer page
│
├── README.md                 the pitch + the proof (eval numbers)
├── AGENTS.md                 rules for AI sessions (verify commands, invariants)
├── policy.yaml               the product's retry/consent bounds, human-readable
├── pyproject.toml            deps, ruff+mypy strict config, pytest config
├── Dockerfile · docker-compose*.yml · render.yaml · run.sh · alembic.ini
└── .github/workflows/        ci.yml (ruff+mypy+pytest) · mutation.yml
```

## The five questions this map should answer fast

| Question | Answer |
|---|---|
| Where does a webhook enter? | `src/ingestion/router.py::receive_razorpay_webhook` |
| Where is money actually moved? | `src/executor/retry_executor.py` — the ONLY module calling Razorpay |
| Where is the double-charge prevented? | deterministic idem keys + UNIQUE constraint; the write-ahead in `orchestrator.py::_execute_and_record` |
| Where is the customer bound by one identity? | `src/cases.py::customer_key` (email → phone → merchant id) |
| Where do I verify my change? | `AGENTS.md`: ruff + mypy --strict + pytest, all three before claiming done |

## Local-only clutter (gitignored — safe to delete anytime)

`.DS_Store`, `.coverage`, `.mutmut-cache`, `ruvector.db`, `preview/`,
`.hypothesis/`, `.schemathesis/`, `graphify-out/`, `.env*` — a fresh clone
sees none of it. `preview/` holds UI screenshots and the skills guide PDF.

## Naming conventions (as-implemented, not aspirational)

- Tables: plural snake (`recovery_cases`, `retry_attempts`)
- Files: singular purpose, no `utils.py` dumping ground
- Tests: `test_<module>.py`, one per src module
- Every money-path module carries comments explaining the *invariant*, not
  the mechanics — read them before refactoring anything in `orchestrator.py`
