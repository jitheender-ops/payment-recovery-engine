# Receivables integration plan — wiring `src/receivables/` into the pipeline

Status: **INTEGRATED (2026-08-30).** All steps below are wired and green:

- Migration `0010_receivables` — 8 tables + `recovery_cases.account_id` +
  `risk_events.account_ref`, with runtime backfill (chain consistent on all
  four `check_migrations.py` paths)
- Ingestion: `RiskEventIn.account_ref` → `process_risk_event` links the case
  to its AR account (explicit ref → `ref:`, else derived from the canonical
  customer key)
- Sweep: `scheduler.chase_due_accounts` — the consolidation layer that
  schedules ONE contact per account per rung; the existing per-case pipeline
  still spends every budget slot and mints every link (carrier rotation)
- Customer page: dispute + plan request endpoints (token-auth, idempotent,
  same discipline as /promise), with the B2B dialog forms on the invoice
  recovery page
- Merchant surface: `POST /ar/cases/paid` (external payments, counted-never-
  claimed), `POST /ar/cases/dispute` (verdicts), `POST /ar/tasks/done`;
  console aging + days-to-pay + promise-kept + alerts panels
- Writeback: `deliver_pending_alerts` HMAC-signed drain on the tick,
  `MERCHANT_WEBHOOK_URL` config, 3-attempt fail-soft cap
- Plan lifecycle: `reconcile_plans` stamps defaulted/completed and raises
  the merchant alerts; the promise-expiry sweep + `stage_after_break`
  ratchet resume a defaulted plan's chase one rung firmer

The design notes below are the original plan, kept as the architecture
record of what each piece is for.

The module map, in dependency order (all standalone-tested before this
wiring; see tests/test_receivables.py):

## Step 0 — Migration (alembic, one revision)

Tables already register on the shared `Base` (importing
`src.receivables.models` is enough). The revision creates:

```
ar_accounts, ar_contacts, ar_contact_log, payment_plans, plan_instalments,
case_disputes, merchant_alerts, account_tasks
```

plus `recovery_cases.account_id` (nullable UUID, indexed) with a backfill:

- `account_ref` from `risk_events.meta.account_ref` where present
- else `derived:<canonical customer_key>` (accounts.account_ref_for_case)

## Step 1 — Ingestion (`src/ingestion/risk_router.py`)

- `RiskEventIn` gains optional `account_ref` (validated like reference_id)
  and the meta carries it through
- `process_risk_event` links/creates the account via
  `get_or_create_account` and stamps `case.account_id`
- Future `due_at` (already accepted) becomes the pre-due trigger

## Step 2 — The sweep (`src/scheduler.py`, `src/orchestrator.py`)

One new sweep `chase_due_accounts` beside `chase_due_cases` (same
deadline/re-arm discipline):

1. Group due `invoice_overdue` cases by account (skip cases with open
   disputes — `case_disputes.status='open'`)
2. `stage_for_aging(days_past_due)` places the account's oldest case on its
   rung; `segments.entry_stage_level`/`gap_multiplier` adapt within the
   envelope
3. `is_b2b_contact_time` gates; `next_b2b_window` defers (the blackout-defer
   pattern at orchestrator.py:785)
4. `compose_stage_message` renders; sender sends; `ArContactLog` records the
   one consolidated contact; per-case attempts/link-minting stays on the
   EXISTING chase_case path (budget, write-ahead, guardrail all unchanged)
5. Stage 3 channels include `call_task` → `raise_call_task` (no budget spend)
6. Broken promise → `stage_after_break` ratchet on the next sweep

## Step 3 — Customer page (`src/customer/routes.py`)

- Account statement view (all open invoices, per-invoice pay) at the
  existing `/recover/<token>` pattern
- POST `/recover/<token>/dispute` → `open_dispute` (token-auth, idempotent)
- POST `/recover/<token>/plan` → `create_plan` (validate shape server-side)

## Step 4 — Merchant surface (`src/merchant/`)

- HMAC'd `POST /cases/paid` → `record_external_payment` (mirrors `/risks`
  discipline: fail-closed secret, dedup, bounded body)
- Console: aging table, days-to-pay, promise effectiveness (aging.py
  queries are already PII-free)
- Alerts feed + outbound webhook dispatcher sweep (HMAC-signed POSTs,
  delivery_attempts cap)

## Step 5 — Ops dashboard + eval

Streamlit aging tab; eval scenarios for segment ladders, promise-break
ratchet, consolidation, dispute freeze.

## Verification at every step

```
.venv/bin/ruff check src eval scripts tests
.venv/bin/mypy --strict src scripts eval
.venv/bin/python -m pytest -q
graphify update .
```
