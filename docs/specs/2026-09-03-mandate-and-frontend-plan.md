# Promise-to-Pay collection + frontend completion — plan

**Status: IMPLEMENTED 2026-09-03**, except where noted under "Still open" at
the end. Written before the Render deployment. Two workstreams, sequenced: the money-path change first because it
adds a surface the frontend then has to show.

## Context

Two things prompted this.

**A promise to pay currently collects nothing.** It is a deferral plus a
reminder plus a link: `record_promise` pushes `next_action_at` out, a reminder
fires at T-48h with a payment link, and at `due_at + 24h` grace
`expire_promises` marks it broken. The customer pulls the money or nobody does.
The decision is to authorise a **UPI Autopay mandate at promise time** and debit
it on the due date — with today's trust-based promise kept as the fallback when
authorisation is declined or the amount is too large to debit unattended.

**The console has real capability with no way to reach it.** An inventory found
two panels whose data is queried on every render and never displayed, three
merchant workflows that exist only as HMAC-signed JSON, and several modules with
no read surface at all. One panel tells the merchant to take an action the page
gives them no control for.

Intended outcome: promises that collect themselves where the customer allows it,
and a console where every enforced behaviour is visible and every named action
is doable.

---

## Part 0 — Preconditions

Both are small, both block Part A, and the first is a live money-path defect.

### 0.1 The amount ceiling says one number and enforces another

`policy.yaml:23` publishes `amount_ceiling_inr: 250000` (₹2,50,000).
`src/config.py:219` enforces `amount_ceiling_paise` defaulting to `5_000_000` —
**₹50,000**. `AMOUNT_CEILING_INR` is retained as a `validation_alias` *for the
paise field*, so an operator copying policy.yaml's own number into that env var
gets **₹2,500** — a 100× under-ceiling that silently refuses almost every retry.
The code comment at `config.py:216` shows the hazard was known; policy.yaml still
publishes the trap.

PRODUCT.md's rule is "the engine states only what it enforces." This also gates
Part A: the amount ceiling is the guardrail that decides whether a
promise-backed debit is allowed to run.

- Correct `policy.yaml` to the enforced number, in the enforced unit.
- Add a sanity validator on `amount_ceiling_paise`: a value below ~₹1,000
  (`100_000` paise) is almost certainly rupees pasted into a paise field —
  refuse at startup with that explanation rather than running a 100× -tight
  ceiling. Keep the alias so existing deployments still boot.

### 0.2 `/voice/demo` is unauthenticated in production

`src/voice/webhook.py:257` (`GET /voice/demo`) and `:262`
(`POST /voice/demo/stt`) carry no auth dependency, and the router is mounted
unconditionally at `src/main.py:171`. `include_in_schema=False` hides it from
`/docs`; it does not gate the URL. `/voice/demo/stt` accepts an **unauthenticated
file upload and spends real Sarvam quota per request**. The in-code control is a
comment saying it is "not linked from the console nav."

Gate it behind the console session (`_gate()`), or mount it only in development
the way `src/demo.py` is. Development-only is the smaller diff and matches the
existing precedent.

---

## Part A — Promise-to-Pay backed by a UPI Autopay mandate

### What exists and is reused, not rebuilt

- **`record_promise`** (`src/cases.py:883`) is the single choke point. All four
  capture surfaces funnel through it — customer page (`customer/routes.py:804`),
  voice (`voice/pipeline.py:200`), the signed merchant API
  (`ingestion/risk_router.py:222`), payment plans (`receivables/plans.py:160`).
  The mandate attaches to a promise; it never replaces one.
- **`promised_rail`** already exists as a column with a migration
  (`0008_promise_capture`) and a docstring — and **no caller has ever set it**.
  It becomes `upi_autopay` when a mandate backs the promise.
- **`check_mandate_predebit_notification`** (`guardrail/rules.py:248`) already
  enforces the RBI 24-hour pre-debit notice and is wired in as gate check #10.
  It has never been reachable, because the action it guards mints a Payment Link.
  This work is what opens that door.
- **`remind_promises`** already sends a real, recorded contact at T-48h
  (`orchestrator.py:1309`). That is the pre-debit notice — no new notification
  path is needed, only the wiring that lets the guardrail see it.
- **`_TimeoutSession`** (`executor/retry_executor.py:90`) applies its timeout to
  every SDK resource by construction, so new Razorpay resources inherit it.
- The **write-ahead ordering** (`orchestrator.py:1565`), the
  `retry_attempts.idempotency_key` UNIQUE constraint, `_attempt_exists`, and the
  off-thread pool all apply unchanged to a debit.

### What is genuinely new

No Razorpay `customer`, `token`, `subscription`, `order` or `createRecurring`
call exists anywhere. The engine's entire gateway surface today is three methods
on `payment_link` plus one raw HTTP GET for downtimes.

### A.1 Verify the webhook event names first

`docs/razorpay-integration-notes.md:139` records that Razorpay's webhook event
catalogue was never confirmed — `webhooks.md` names three events and does not
carry the list. Every event name below is therefore an assumption.

**Do this before building reconciliation:** register a UPI Autopay mandate on the
test account, debit it, and record the actual event names and payload shapes.
Write them into the integration notes. `src/ingestion/router.py:296` builds its
event id from `payload.payment.entity.id`; a mandate/token event has a different
entity path and will fall to the sha256-of-body branch — correct, but it should
be a known behaviour rather than a discovery.

### A.2 Data model — migration `0013_promise_mandate`

Columns on `promises_to_pay`, not a new table. The mandate is authorised for one
promise's amount and date; a reusable per-customer mandate is a different product
decision and is not being made here.

- `mandate_token` — the gateway token id, nullable
- `mandate_status` — `none` / `pending` / `active` / `failed` / `cancelled`
- `mandate_registered_at`, `mandate_authorization_ref`
- set `promised_rail = 'upi_autopay'` when active

`mandate_status = 'none'` is the fallback path and stays the default, so every
existing row and every non-mandated promise behaves exactly as it does today.

### A.3 Executor — the only module allowed to call Razorpay

Add to `src/executor/retry_executor.py`:

- `create_mandate_registration_link(...)` → `client.registration_link.create(...)`.
  Shaped almost identically to the existing `_create_payment_link`
  (`:483`), including the `notes` breadcrumb and the sanitised contact fields.
- `charge_mandate(...)` → `client.payment.createRecurring(...)`.

**`src/demo.py`'s `FakeRazorpayClient` must grow to match** (`:249`). It covers
exactly three methods on one attribute today; demo mode breaks the moment a
fourth is called. The fake's own contract note says it should only expose what
the real client does — keep that honest.

### A.4 Amount cap — the fallback is not optional

RBI limits unattended e-mandate debits without additional authentication.
**Confirm the current threshold and the category rules with Razorpay before
building** — this figure has moved more than once and the higher limits apply to
specific categories, not general merchant collection.

Add `mandate_max_auto_debit_paise`. Above it, do not offer the mandate: record
an ordinary promise. This is why the fallback exists — above the cap, an
unattended debit is not permitted, so the trust-based promise is the only legal
path, not a lesser one.

### A.5 The debit — a new scheduler sweep

`charge_due_promises`, running **before** `expire_promises` in the tick
(`scheduler.py:1399`), so a promise that is about to be charged is never marked
broken in the same pass.

For each pending promise with `mandate_status = 'active'` and `due_at <= now`:
write-ahead a `RetryAttempt`, validate through the **full guardrail gate**, then
debit. Never call Razorpay before the attempt row is committed — that ordering is
a stated money-safety property, not a style choice.

`ActionType` (`agent/actions.py:16`) is a closed Literal of five values and every
non-abandon case action currently mints a link. The debit needs to be
distinguishable from a link-mint. Prefer reusing `retry_now` with the rail
carrying the distinction over widening the action space — a sixth action means
re-training the XGBoost label encoder and touching every policy that branches on
`ActionType`.

### A.6 Guardrail — make rule 10 cover this

`check_mandate_predebit_notification` short-circuits to pass unless
`risk_type == "mandate_failure"`. A promise-backed debit on a `payment_failure`
case would skip the RBI rule entirely.

Extend the rule's trigger to cover a promise-backed debit on any risk type. Keep
it in that one function — a parallel rule is how two compliance checks drift
apart. `validate_self_serve` must stay excluded: a customer paying their own link
is not a mandate debit.

### A.7 Attribution and the product claim

PRODUCT.md defines *recovered* as "paid through a link the engine sent." A
mandate debit collects with no link, so auto-collected promises would either go
unattributed or quietly break the console's central claim.

Extend the definition to "…or collected on a mandate the customer authorised",
in PRODUCT.md and in the console copy. `attribute_capture` joins on
`external_ref` (`models.py:381`), so the recurring payment's id fills the same
slot the link id does today — the join does not change shape.

`resolve_promises(..., "kept")` already fires from `attribute_capture`
(`cases.py:620`), so a successful debit keeps the promise through the existing
path.

### A.8 Fix the horizon check while we are here

`promise_max_horizon_days` is enforced in **three separate copies** — page
(`customer/routes.py:845`), voice (`pipeline.py:223`), merchant API
(`risk_router.py:277`) — and `record_promise` itself accepts any `due_at`,
including one in the past. A mandate-authorisation callback would be a fifth
caller that silently bypasses it. Enforce at the choke point, keeping the
payment-plan exemption that pushed it outward originally.

### A.9 What must not regress

`tests/test_promises_and_audit.py:138` locks in that a promise never pulls
contact *earlier* than the escalation backoff — `record_promise` takes the
`max()` of the promise date and the existing schedule (`cases.py:974`). A debit
on the due date must not disturb that.

---

## Part B — Frontend

Gaps first, then a consistency pass. Everything stays inside the pinned v2
"Ledger on paper" system (`docs/design-system/recovery-console/MASTER.md`) and
its customer-page override (`docs/design-system/pages/customer-recovery.md`).
**No animation library** — GSAP was deliberately removed; motion is native CSS
plus the ~45 lines already in `base.html:260`.

### B.1 Dead data — queried every render, displayed nowhere

Cheapest wins in the codebase: the queries already run and the results are
discarded.

- **`ar_alerts`** — `merchant/routes.py:557`. Zero template references. The
  panel's own comment records fixing a Postgres bug that had made it
  "permanently empty in production"; the fix landed, the render never did.
- **`voice`** — `merchant/routes.py:566`. `voice_panel()` returns
  queued/claimed/done/**failed**/opted_out. The only voice signal a merchant ever
  sees is a one-line attention item, and only when `queued and not claimed` — so
  a queue with a climbing `failed` count is completely invisible.

### B.2 The UI names an action it does not offer

`/console/live` renders the disputed-invoices table and states "Chasing is frozen
on these until you uphold or reject" (`live.html:283`) — with no uphold or reject
control. The backend is `POST /ar/cases/dispute`
(`merchant/receivables_api.py:195`).

Same shape, same fix, two more places:

- **External payments** — `POST /ar/cases/paid` (`receivables_api.py:138`).
  Money arriving by NEFT/RTGS/cheque/cash. The console shows outstanding
  balances it cannot let you close.
- **Call tasks** — `POST /ar/tasks/done` (`receivables_api.py:233`). The ladder
  shows `open_call_tasks` as a bare count: no list, no refs, no close button.

These are server-rendered forms posting to existing handlers, consistent with
every other mutation in the console (`/console/batch/run` is the precedent).

### B.3 Modules with no read surface

Ordered by how much a merchant would miss them:

1. **Segments** (`receivables/segments.py`) — decides which rung an account
   enters at and how fast it escalates. A merchant cannot see which segment an
   account is in or why it is chased at that cadence.
2. **AR accounts and contacts** (`receivables/models.py:42`, `:76`) — no
   directory, no contact list, no add/edit. The only per-account view in the
   product is the customer-facing `/statement/{token}`.
3. **Audit-chain verification** (`audit_chain.py:138`) — reachable only from a
   CLI script. The "auditable" claim has no in-product status. Now that the
   scheduler stamps the chain every tick, a verify status belongs on
   `/console/case/{id}`.
4. **Message preview** (`messaging/nudge_generator.py`,
   `receivables/statement.py`) — no preview of what the customer actually
   receives, and no sent-message log.
5. **Ladder detail** (`receivables/ladder.py`) — only rung counts render; the
   B2B contact window and the after-break demotion are invisible.

### B.4 The new promise UI (depends on Part A)

On `/recover/{token}`, inside the existing `<details class="promise-box">`
(`recover.html:283`): offer "set up autopay for that date" alongside the plain
date promise. Native `<input type="date">` with server-supplied min/max stays.

The copy has to be exact about what is being authorised — an amount, a single
date, one debit — because this is the one screen where the customer hands over a
standing instruction. Above the cap, the option is not shown at all rather than
shown and refused. Both language catalogs (`customer/i18n.py`) need entries.

Console side: promise rows show whether a mandate backs them, and the promises
panel splits auto-collected from customer-paid.

### B.5 Consistency pass

- **Merchant console has no dark mode** by design thesis, and that stays. The
  customer page has full `prefers-color-scheme` support — do not "fix" the
  console to match.
- Accessibility gaps found: no skip-link on any page; no `aria-live` on the
  confirming state (it uses a meta-refresh); the landing replica's tablist has
  `aria-current` but not `aria-selected`/`aria-controls`.
- `docs/specs/2026-08-25-recovery-page-polish-design.md` is still marked "design,
  awaiting review" though every item in it shipped. Correct the status.

### Explicitly not in scope

- Porting Streamlit's CSV export, Plotly charts or bank×rail heatmap to the
  console. The console uses static inline SVG by choice.
- A settings page for `policy.yaml`. Those bounds are deliberately code, not
  config: "a promise that can be moved by a typo in an env var is not a promise."

---

## Verification

Per AGENTS.md, all three before claiming anything is done:

```bash
.venv/bin/ruff check src eval scripts tests
.venv/bin/mypy --strict src scripts eval
.venv/bin/python -m pytest -q
```

Plus, specific to this work:

- `python scripts/check_migrations.py` — the chain must stay consistent with the
  ORM through `0013`.
- **The mandate round trip on the Razorpay test account**, which is the only
  real evidence: authorise a UPI Autopay mandate from the recovery page, confirm
  the promise records `mandate_status='active'` and `promised_rail='upi_autopay'`,
  let the due-date sweep debit it, and confirm `attribute_capture` resolves the
  promise to `kept` and credits the case. Nothing about that path is simulated
  except the money.
- Guardrail: assert a promise-backed debit with no pre-debit notice on record is
  **refused**, and that the same debit passes 24h after a reminder went out.
- Above-cap: assert no mandate is offered and the promise records as today's
  trust-based promise.
- Demo mode must still run end to end (`./run.sh --demo`) with the extended
  `FakeRazorpayClient`.


---

## Still open

Carried forward deliberately, not forgotten:

- ~~`MANDATE_CONFIRMED_EVENTS` is a candidate set~~ — **resolved by deletion.**
  Guessing Razorpay's event names was the wrong shape of fix: a guess that
  fails silently is worse than no feature. Confirmation now asks the gateway
  for the token's status (`reconcile_pending_mandates` →
  `RetryExecutor.fetch_mandate_status`), which is authoritative and needs no
  webhook registered at all.
- ~~The RBI unattended-debit threshold needs confirming~~ — **settled at
  ₹15,000**, the general limit for authentication-exempt debits, which is what
  merchant collection is. The raised ceilings are category-specific and do not
  apply here.
- ~~Part B.3~~ — **done.** In-product audit-chain verification (`/console/ops`),
  the buyer directory (`/console/accounts`, `/console/account/<id>`, with
  add-contact), message preview (`/console/messages`), and full ladder rung
  detail plus the B2B contact window on `/console/live`.
- **Segments were NOT given a UI, deliberately.** `src/receivables/segments.py`
  turned out to be dead code: `classify`, `entry_stage_level` and
  `gap_multiplier` are exported and unit-tested, and **called by nothing**. No
  account is classified and no chase cadence differs because of that file. A
  segment badge would have shown a merchant a label the engine does not act on,
  which is a lie on a money page. Wiring it is a real product change to contact
  cadence — a decision to make on purpose. The module now says so at the top.
- **Streamlit-only capabilities** (CSV export, the bank x rail heatmap,
  per-touch cart recovery) stay Streamlit-only, as scoped.
