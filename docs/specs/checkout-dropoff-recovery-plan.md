# PLAN — Checkout Drop-off Recovery (deep-research + implementation plan)

> **STATUS: IMPLEMENTED (2026-08-30)** — Phases 1, 2, 3, 4 (Option A), 5, 6 are
> live; tests in `tests/test_cart_chaser.py` (16) + full suite green.
> Exceptions: the recovery-page offer line was skipped (Razorpay's hosted
> checkout displays the applied offer authoritatively at the moment of
> decision — a page line would duplicate it and drift on expiry), and the
> per-touch page-view table lives in `cases.chase_effectiveness` (eval
> harness); the Streamlit dashboard shows the per-touch recovery table.
>
> Original plan below, unchanged for the record.

---

## 1. Research summary (what the outside world says)

### The leak is large and mostly mobile

| Finding | Number | Source |
|---|---|---|
| Average cart abandonment rate | **70.22%** (avg of 50 studies, 2006–2025) | Baymard Institute |
| Mobile abandonment vs desktop | **85.65% vs 73.07%** | Barilliance |
| Why (excluding "just browsing") | extra costs **40%**, account demanded **18%**, checkout too complicated **17%**, trust **19%**, card declined **10%**, payment methods **9%** | Baymard quant study |
| Recoverable through better UX alone | **35.26%** conversion uplift possible | Baymard |
| Abandoned-cart flows = highest-ROI automated flow | **$3.65** avg revenue/recipient, **3.33%** avg conversion; top 10%: **$28.89 RPR, 7.69% conv** | Klaviyo 2024 benchmark (143K flows) |
| Email engagement | ~**50.5%** open, **6.25%** click, best-in-class 65%/13.3% | Klaviyo 2024 |
| Cadence that works | reminder **2–4h** → follow-up (+incentive) **24h** → final **48h**; the **3rd touch generated the most revenue** ($24.85M vs $16.4M for the 2nd) | Klaviyo / Barilliance |
| Incentives | discounts lift conversion but train waiting; segment: incentive only on **high-value carts**, never touch 1 | Klaviyo takeaways |
| Personalization | name + items in subject/body → **+26% opens, 62% more responses** | Slicker (already cited as R7 in the recovery-page spec) |
| Honest deadlines beat fake countdowns | real expiry surfaced = faster payment | Loopwork (already R8) |

### India-specific (already partially in the repo's own spec, R9/R3)

- The **OTP step is India's biggest card drop-off; UPI skips it** → UPI-first CTA on
  recovery pages.
- **SMS CTR ~19% vs email ~4%; day-0 contact recovers most** → SMS is the
  primary channel, timing matters more than channel variety.
- **TRAI DLT**: transactional SMS templates must be registered; the engine
  already rides Razorpay's `notifyBy` (DLT handled by Razorpay + merchant
  template registration). Anything we add must keep the **160-char** budget.
- No WhatsApp — that is a pinned product commitment (PRODUCT.md), not a gap.

### What the research does NOT support (rejected up front)

- Exit-intent popups, onsite nudges, browse-abandonment — these are
  **merchant-side UX**, not post-abandonment outreach. Out of this engine's
  boundary (the merchant pushes us an event; we never see the session).
- Fake urgency/countdowns — violates the "no invented claims" truth rule.
- A blast of >3 touches — 2–3 is where the returns are; beyond that is spam
  (and the current policy's own words: "A third is spam").

---

## 2. Current state in the engine (verified in code)

The engine already chases abandoned checkouts end-to-end:

- **Intake**: `POST /risks` with `risk_type: checkout_abandonment`, HMAC-signed,
  deduped, write-ahead (src/ingestion/risk_router.py:63, :169).
- **Policy** (src/chasers/policy.py:77): `max_attempts=2`, window **48h**,
  first touch **1h**, re-chase **24h**, `recommended_rail=None`,
  `failure_class="abandoned_checkout"`, subject noun "order".
- **Delivery**: mints a Razorpay Payment Link + `notifyBy` **sms and email**
  (src/executor/retry_executor.py:388-393); nudge text LLM-or-template,
  ≤160 chars (src/messaging/templates.py:69 — "your ₹X order is still waiting").
- **Recovery page** (`/recover/<token>`): hero "About your order", trust strip,
  real expiry, opt-out (src/customer/routes.py:91).
- **Guardrail**: bounds enforced, all rules reported, hard-decline blocklist.

### Gaps vs research (ranked by expected lift ÷ effort)

| # | Gap | Research basis | Expected lift |
|---|---|---|---|
| G1 | **No cart-item personalization** in the nudge — `risk_meta` (cart_items) is stored but never reaches `NudgeGenerator.generate()` or the template | Klaviyo/Barilliance: personalization +26% opens; product names in message = higher CTR | High / small |
| G2 | **No incentive path** — no way for a merchant to attach an offer to a cart chase; Razorpay Payment Links natively support `offer_id` | Klaviyo: incentive on later touches, high-value carts only | High / medium |
| G3 | **2-touch ceiling** may be one touch short — Klaviyo's data says the 3rd touch earns the most | Klaviyo cadence study | Medium / product decision |
| G4 | **First touch at 1h** — research says 2–4h email but SMS recovers most at day-0; engine's 1h is defensible, but a cart that goes cold at 11pm shouldn't be chased at midnight — the **IST blackout** already clamps this; verify it applies to carts | Baymard/Klaviyo timing; engine blackout tests exist | Low / verify-only |
| G5 | **No click/visit signal** — we can't compute CTR or distinguish "nudge ignored" from "link clicked but page abandoned" | needed for any measurement of G1–G3 | Medium / medium |
| G6 | **Recovery page for carts is generic** — no item list, and carts (no original rail) don't get the UPI-first CTA the way card-failure classes do | R9 (UPI-first), Klaviyo (cart contents in email lift CTR) | Medium / small |
| G7 | **No per-touch attribution view for carts** in eval metrics (attempts_per_crore exists; recovery-rate-per-cadmium-step per risk_type doesn't) | Klaviyo benchmarks to compare against | Low / small |

---

## 3. The plan

Six phases, each independently shippable. Phases 1–2 are pure wins inside
existing promises; Phase 3 and 4 each carry an explicit product decision;
Phase 5 is measurement that makes 3/4 honest; Phase 6 is docs/graph.

### Phase 1 — Cart items in the nudge (G1) · *no product decision needed*

**What**: surface merchant-supplied `meta.cart_items` (already validated,
bounded, sanitized on intake) in the nudge text and on the recovery page.

- `orchestrator._execute_case_and_record` (src/orchestrator.py:1020): pass a
  bounded cart summary (≤40 chars, already-printable — reuse
  `agent.prompts.sanitize_meta` discipline) into `NudgeGenerator.generate()`.
- `NudgeGenerator.generate()` + `_TEMPLATES["abandoned_checkout"]`
  (src/messaging/nudge_generator.py:124, src/messaging/templates.py:69):
  optional `cart_summary` param; template gains
  `{{ ' — ' + items if items else '' }}` inside the existing 160-char budget.
  If the summary would break 160, template path drops it (LLM path gets it in
  the prompt with the same budget rule).
- Recovery page: render cart items line under the hero from the same sanitized
  field (server-rendered, i18n keys `cart_items_en/hi`).
- **Files**: orchestrator.py, nudge_generator.py, templates.py, customer/routes.py
  (+ spec's i18n catalog), tests.
- **Tests**: 160-char ceiling still holds with items; items never rendered when
  absent; page shows items iff meta carried them; no new prompt-injection
  surface (extend test_security_attacks with a hostile cart_items value).

### Phase 2 — Click/visit signal on the recovery page (G5) · *prerequisite for honest measurement*

**What**: a `case_events` row (`event_type="page_viewed"`) when a recovery page
for an open cart case is served. Server-side only, no third-party JS, no PII
beyond what the token already scopes. This is observability of engine-owned
surfaces, consistent with the existing audit chain.

- `customer/routes.py` recover handler: after token verify, write the event on
  the same session (best-effort — a failed write never blocks the page).
- `eval/metrics.py`: add `recovery_page_view_rate` and
  `nudge_to_view` per risk_type.
- **Files**: customer/routes.py, cases.py (event writer already exists — reuse),
  eval/metrics.py, tests.
- **Tests**: page view recorded once per serve; forged-token 404 records
  nothing; metrics endpoint computes the new numbers.

### Phase 3 — Optional merchant incentive on the cart chase (G2) · **product decision**

**What**: an OPTIONAL, merchant-supplied offer on a checkout_abandonment event.
The engine never invents a discount; it relays one the merchant already made.

Decision needed: do we want offers on the money line at all? The engine's
truth rules are satisfiable ("the merchant is offering 10% off" is a merchant
claim, not ours), but it adds a knob on a surface that is deliberately
knob-free.

If yes:

- `RiskEventIn` (src/ingestion/risk_router.py:86): optional
  `offer_id: str | None` — a **Razorpay offer id** in the merchant's Razorpay
  account, not a freeform percent (the gateway validates amounts; we never
  compute money we don't control).
- Executor `create_link` path (src/executor/retry_executor.py): pass
  `offer_id` through to Razorpay's Payment Link create. Razorpay enforces the
  offer's own validity/amount rules — the engine's amount ceiling and
  write-ahead ordering are untouched.
- Recovery page: one honest line, only when the link actually carries an offer:
  i18n `offer_line` ("A discount from {merchant} is applied on this link.").
  No invented urgency.
- Policy guardrail: offers ride only on **touch ≥2** of a cart case (Klaviyo:
  incentive on touch 1 trains discount-waiting). Enforce in the chase step
  that builds the action, not in the agent's prompt.
- **Files**: risk_router.py, retry_executor.py, chasers/policy.py (no — the
  touch-count rule lives in the chase builder in orchestrator/cases),
  customer/routes.py, tests.
- **Tests**: offer_id accepted + round-tripped; refused for non-cart risk
  types; page line present iff link has offer; touch-1 never carries offer.

### Phase 4 — Third touch for carts (G3) · **product decision**

**What**: Klaviyo's data (3rd touch = most revenue) vs the frozen promise
("Two contacts total… A third is spam", src/chasers/policy.py:73-76).

Options, pick one:

- **A (recommended)**: keep `max_attempts=2`, but let the SECOND touch be a
  "last call" nudge (no new link, re-uses the existing one — `test_an_existing_link_is_reused_not_reminted`
  already pins this behaviour) scheduled at window-minus-2h instead of flat
  +24h. Same contact count, better position.
- **B**: raise to `max_attempts=3`, window 48h→72h. More recovery room,
  breaks a published product promise; requires the console copy and any
  merchant-facing docs to change with it.
- **C**: status quo — document the 2-touch promise as final.

**Files (if A)**: chasers/policy.py (re_chase timing only — max_attempts
untouched), scheduler chase_due_cases, tests in test_chasers.py.
**Test**: second touch fires at the new offset; still exactly 2 touches ever.

### Phase 5 — Cart-recovery metrics (G7)

- `eval/metrics.py`: recovery rate, page-view rate, and recovered-₹ **per
  risk_type per touch-index** (the data is all in `recovery_cases` +
  `retry_attempts` + `case_events`; only the query/shape is new).
- Dashboard: one table on the ops Streamlit, no new tables in the DB.
- **Test**: metrics snapshot test with seeded cases (pattern exists in
  eval/ — follow eval_methodology.md).

### Phase 6 — Docs, graph, verification

- `docs/failure_cases.md` + `docs/architecture.md`: new section for the cart
  chaser's research-backed parameters, citing the numbers in §1.
- `graphify update .` per AGENTS.md.
- Full gate: `.venv/bin/ruff check src eval scripts tests` ·
  `.venv/bin/mypy --strict src scripts eval` · `.venv/bin/python -m pytest -q`.

---

## 4. Explicitly out of scope

- Onsite/exit-intent/browse-abandonment (merchant UX, not this engine).
- WhatsApp, Hinglish voice, any channel outside SMS/email via Razorpay
  `notifyBy` (pinned product commitments).
- Generic A/B-testing infrastructure (YAGNI — Phase 5's per-touch metrics
  answer the ranking questions manually; add real experiments only when a
  merchant asks).
- Changing the write-ahead ordering, guardrail short-circuiting, or any
  money-path rule in AGENTS.md.

## 5. Suggested order

1 → 2 → 5 (measure the baseline), then decide 3 and 4 with data.
Phase 1+2 ship inside every existing promise with no decisions required.
