# Razorpay integration notes

What was learned from Razorpay's own documentation on 2026-09-01, what was
acted on, and what was deliberately not. Companion to
`docs/decline-taxonomy.md`, which covers the decline mapping in detail.

The useful entry point is **`razorpay.com/docs/llms.txt`** — a machine-readable
index of the documentation, where each topic resolves to a `.md` page under
`razorpay.com/docs/build/llm-docs/`. It is the reason three questions this
codebase had open could finally be answered; the marketing overview pages that
turn up first answer none of them.

## Acted on

### Decline reasons → `FailureClass`

`payments/payments/failure-analysis.md` publishes eighteen `error_reason`
values with Razorpay's own retry verdict for each. Auditing
`src/classifier/error_codes.yaml` against it found ten unmatched, two of which
(`card_declined`, `payment_declined`) were being abandoned without an attempt.
Full write-up: **`docs/decline-taxonomy.md`**.

The list now lives in `error_codes.yaml` under `razorpay_documented`, and
`scripts/seed_error_codes.py` fails if any entry stops classifying — so the
audit is a check rather than a memory.

### The error envelope

`errors.md` gives the shape every failure arrives in:

```json
{"error": {"code": "...", "description": "...", "field": null,
           "source": "...", "step": "...", "reason": "...",
           "metadata": {"payment_id": "...", "order_id": "..."}}}
```

`code` is the envelope bucket (`BAD_REQUEST_ERROR`, `GATEWAY_ERROR`,
`SERVER_ERROR`); **`reason` carries the machine string** the classifier keys
on. `scripts/simulate_webhooks.py` now generates its synthetic declines in
this vocabulary — it previously used six invented reason strings and none of
the two that were being abandoned, so the demo could not have surfaced the
bug it was ostensibly exercising.

### The sandbox, and running against it

`razorpay.com/docs/api/sandbox-setup`, verbatim: **"The base URL for the
Razorpay Sandbox and production API is the same — `https://api.razorpay.com/v1/`."**
The key prefix (`rzp_test_` vs `rzp_live_`) is the entire difference. There
is no separate host, and nothing to run locally — it is the live API with
test credentials, so it needs an account and a network and can never
replace demo mode.

The code already supported it: `DEMO_MODE=false` with test keys is the
ordinary production path. Only packaging was missing, so `./run.sh --sandbox`
borrows the demo's SQLite and seeding while using the **real** gateway.

Two guards, because this one can reach the outside world:

- A `rzp_live_` key is **refused outright**. The mode seeds synthetic cases,
  and against live credentials that would mint real Payment Links addressed
  to real customers.
- `.env` is sourced (the keys live there) but cannot beat a value the caller
  exported — `source` overwrites, so `RAZORPAY_KEY_ID=... ./run.sh --sandbox`
  would otherwise silently use a different key than the one just named.

**Webhooks need a public URL.** Without `--tunnel`, links mint and open
Razorpay's real checkout but the capture cannot come back, so paying one
will not show as recovered. The banner says so rather than letting it look
like a broken engine.

### Checking the taxonomy against the real gateway

The forcing test cards (`razorpay_test_cards` in `error_codes.yaml`) each
produce a chosen `error_reason`. Pay a sandbox Payment Link with one, choose
**failure** on the success/failure screen, and the decline arrives carrying
that exact string — which is how a mapping is verified against the gateway
rather than against documentation. That check found three wrong mappings on
2026-09-01; see `docs/decline-taxonomy.md`.

### Webhook source IPs

`security.md` is mostly Razorpay's own posture (PCI-DSS Level 1, ISO 27001,
TLS, AES-128, field-level PII encryption) — nothing there for this codebase
to implement. The one merchant-actionable item is **allowlisting Razorpay's
webhook source IPs** as defence in depth alongside HMAC verification.

Implemented as `WEBHOOK_IP_ALLOWLIST`, comma-separated IPs or CIDRs, **off by
default**. Unlike every other guard here it does not fail closed, and that is
deliberate: it only narrows what HMAC already guards, so defaulting it closed
would reject every real webhook the moment someone upgraded without setting
it. HMAC remains the authenticator — an IP is not an identity. The addresses
themselves are not baked in, because Razorpay changes them and a stale
hardcoded list is an outage waiting to happen.

## Deliberately not built

### The CLI and the MCP server

`developer-tools.md` documents a CLI, an MCP server (remote via `npx`, or
self-hosted Docker; 35+ tools), an n8n community node, and llms.txt itself.

**None of these belongs inside this project.** Each needs API keys and network
access, which is the opposite of a build that runs entirely on a laptop. They
are tools for a developer working *on* this repo, not dependencies it carries.

Worth knowing anyway: the **CLI carries `Smart Collect` and `Downtime` command
groups**, which is the fastest way to answer the two questions still open
below — against a real test account, rather than by reading more prose.

### Self-serve card / mandate update

Previously scoped as a customer-page feature. **The API does not exist.**
`api/payments/subscriptions/update-subscription.md`:
`PATCH /v1/subscriptions/:id` updates only `plan_id`, `offer_id`, `quantity`,
`remaining_count`, `start_at`, `schedule_change_at` and `customer_notify`, and
explicitly **cannot change the payment method or card** — any other field is a
validation error.

So there is no in-place update to link to. The only path is a fresh
mandate/subscription authorisation — "re-subscribe", not "update" — which is a
consent and compliance decision (and interacts with the RBI e-mandate rules
already enforced in `src/guardrail/rules.py`), not the frontend change it was
scoped as. **Dropped** pending a deliberate decision to design
re-authorisation as its own piece of work.

## Still open

### Smart Collect allocation

`POST /v1/virtual_accounts` is confirmed, with `receivers.types`
(`bank_account` / `vpa`), `customer_id`, `close_by` and `notes`, returning
account number, IFSC and bank name. That is enough to mint one virtual account
per `ArAccount` and surface it on the statement page.

Two questions block the rest, and neither is answered anywhere in the docs
reachable so far:

1. **What event fires on an incoming credit?** `webhooks.md` is an
   introductory page naming three events and does not carry the catalogue.
2. **Does Razorpay allocate one credit across several open invoices, or is
   that ours to implement?** If ours, the natural rule is oldest-invoice-first,
   mirroring `stage_for_aging`.

Resolve with the CLI's Smart Collect commands against a test account, not by
reading more overview pages.

### Live downtime feed — BUILT

`GET /v1/payments/downtimes` and `/v1/payments/downtimes/:id`. Implemented in
`src/downtime.py`, consumed by `src/executor/rail_selector.py`, surfaced on
`/console/live`.

It resolves a `ponytail:` note that had stood in the rail selector asking for
exactly this — *"real downtime-aware routing needs a bank-health source we
don't have; wire one in and take `bank` as an argument when it exists."*
Previously `bank_downtime` was inferred only AFTER a decline had already
cost an attempt; now a rail the gateway reports as impaired is dropped from
`switch_rail`'s choices first.

Two caveats, both in the code:

- **It is an on-demand Razorpay feature** — the docs say support must enable
  it, so a fresh test account gets 401/404. Every failure path degrades to
  "nothing is known to be down" rather than raising. A health feed that can
  talk the engine out of chasing is a liability, not a feature. Logged at
  info, not error: it is a configuration fact.
- **The response schema was not in the reachable documentation.** Endpoints
  are confirmed; field names follow Razorpay's usual collection shape and are
  parsed defensively, ignoring anything unrecognised. `_parse` is the single
  place to correct if the real payload differs.

An issuer-scoped outage matches only that issuer; matching it against every
bank would route the whole book off a rail because one bank is down. A
`resolved` row never steers routing. In demo mode `src/demo.py` serves the
same shape locally, so the client, the parsing and the routing decision are
all the real ones.

### `payment_risk_check_failed`

Razorpay documents it as retryable, advising *"the customer must retry with a
different card or method"* — a rail switch, not a retry. This engine treats it
as `fraud_block` and abandons. Kept conservative on purpose; see
`docs/decline-taxonomy.md`. Worth deciding deliberately rather than leaving as
an unnoticed disagreement.
