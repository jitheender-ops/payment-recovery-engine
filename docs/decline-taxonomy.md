# Decline taxonomy — Razorpay's reasons, and what this engine does with them

**Audited 2026-09-01.** Source: Razorpay's own failure-analysis page, reached
through `razorpay.com/docs/llms.txt` →
`razorpay.com/docs/build/llm-docs/payments/payments/failure-analysis.md`.

This document exists because the mapping it describes had never been written
down. `src/classifier/error_codes.yaml` had 41 rules and no record of what
they were checked against, so nobody could tell a deliberate omission from an
accidental one — and ten of Razorpay's eighteen documented reasons turned out
to be accidental ones.

## Where a decline actually goes

A `payment.failed` webhook carries Razorpay's error 5-tuple. The machine
string this file is about is **`error_reason`**; `error_code` is the
envelope-level bucket (`BAD_REQUEST_ERROR`, `GATEWAY_ERROR`, `SERVER_ERROR`).

```
webhook → ClassifierMapper.classify()   src/classifier/mapper.py
        → first matching rule wins      src/classifier/error_codes.yaml  (priority-sorted)
        → FailureClass                  src/classifier/taxonomy.py
        → customer copy                 src/customer/explain.py
        → retry policy / rail choice    src/agent/, src/executor/rail_selector.py
```

**Falling through costs money.** No matching rule means
`FailureClass.UNKNOWN`, and `UNKNOWN` is non-retryable — the case is closed
without the engine making a single attempt.

Two things make a gap hard to see, and both bit here:

1. **Low-priority catch-alls absorb most misses.** `BAD_REQUEST_ERROR` +
   `error_source: customer` → `issuer_decline`; `GATEWAY_ERROR` →
   `network_error`. An unmapped reason usually lands in one of these, so it
   is still *chased* — just explained to the customer as something it wasn't.
2. **They don't cover `error_source: bank`.** A reason arriving from the bank
   with no rule of its own hits nothing at all, and is abandoned.

## What the audit found

| Razorpay `error_reason` | Razorpay says retryable | Before the audit | After |
|---|---|---|---|
| `authentication_failed` | yes | `3ds_dropoff` (only with `error_step=payment_authentication`) | `3ds_dropoff`, with or without the step |
| `card_not_enrolled` | yes | `issuer_decline` *(catch-all)* | `3ds_dropoff` |
| `insufficient_funds` | yes | `insufficient_funds` | unchanged |
| `payment_cancelled` | yes | `customer_cancelled` | unchanged — **deliberate disagreement**, below |
| `payment_collect_request_expired` | yes | `issuer_decline` *(catch-all)* | `upi_collect_timeout` |
| `card_declined` | yes | **`unknown` — abandoned** | `issuer_decline` |
| `gateway_technical_error` | yes | `network_error` *(via `GATEWAY_ERROR`)* | `network_error`, by name |
| `payment_declined` | yes | **`unknown` — abandoned** | `issuer_decline` |
| `payment_failed` | yes | `network_error` *(via `GATEWAY_ERROR`)* | `network_error`, by name |
| `payment_timed_out` | yes | `payment_timeout` | unchanged |
| `input_validation_failed` | no | `hard_decline` *(catch-all)* | `hard_decline`, by name |
| `international_transaction_not_allowed` | no | `hard_decline` *(catch-all)* | `hard_decline`, by name |
| `invalid_amount` | no | `hard_decline` *(catch-all)* | `hard_decline`, by name |
| `invalid_currency` | no | `hard_decline` *(catch-all)* | `hard_decline`, by name |
| `bank_technical_error` | yes | `bank_downtime` | unchanged |
| `server_error` | yes | `network_error` *(via `SERVER_ERROR`)* | `network_error`, by name |
| `mobile_number_invalid` | yes | `issuer_decline` *(catch-all)* | **deliberately unmapped**, below |
| `payment_risk_check_failed` | yes | `fraud_block` | `risk_check_failed` — resolved 2026-09-02, **switch-only**, below |

### The expensive one

`card_declined` and `payment_declined` are the most generic declines the card
rail produces — "Issuer Banks can decline the card due to multiple checks",
"Issuer Bank or Gateway has declined the payment". Both arrive with
`error_source: bank`, which no catch-all covers. Both classified as `unknown`,
which is non-retryable, so **every case carrying one was closed without an
attempt**, while Razorpay documents both as retryable.

### The wrong-words ones

Not money lost, but a money page saying something untrue:

- `payment_collect_request_expired` — an expired UPI collect request was
  explained as *"Your bank declined the payment… try a different card or UPI"*,
  and since `issuer_decline` is UPI-recommended, the page then recommended UPI
  to someone whose UPI request had just expired. It now says *"The UPI request
  expired before it was approved."*
- `card_not_enrolled` — a 3DS-enrolment problem blamed on the bank. Both
  classes are UPI-recommended, so the button was already right; only the
  explanation was wrong.

## Second audit: the forcing test cards (2026-09-01)

Razorpay publishes cards that force a **chosen** decline reason in test mode
(`payments/payments/test-card-details.md`). Those are strings the gateway is
*observed to emit*, where the list above is what Razorpay *documents* — two
references, kept separate in `error_codes.yaml` so neither is mistaken for
the other. That separation earned itself immediately: the documented-reason
check was already passing when three test-card strings turned out to reach
no rule at all.

All three were absorbed by the priority-10 customer catch-all into a
**retryable** `issuer_decline`. Unlike `card_declined` in the first audit
these were therefore *chased rather than abandoned* — which is the more
expensive failure, because the engine spent real budget doing it.

| Test-card reason | Was | Now | Why it mattered |
|---|---|---|---|
| `insufficient_fund` **(singular)** — our rule was plural | `issuer_decline` | `insufficient_funds` | The page said *"your bank declined… call the number on your card"* instead of *"add funds or use a different card"* — the one piece of advice that resolves it. |
| `card_number_invalid` — ours was `invalid_card_number` | `issuer_decline`, **retryable** | `invalid_card`, non-retryable | **The expensive one.** The engine retried a card whose number cannot work, spending attempt budget and contact allowance per case. That is precisely the false-retry the eval harness reports at 0%, so the gap quietly undercut that headline too. |
| `card_disabled_for_online_payments` — no rule | `issuer_decline`, **retryable** | `hard_decline` | The card cannot be used online at all; another attempt on the same instrument is guaranteed to fail. `hard_decline`'s copy is the honest one — *"another attempt on the same method will be declined too"* — where `expired_instrument` would claim an expiry that has not happened. |

### Reproducing these against the real gateway

The card numbers are in `error_codes.yaml` under `razorpay_test_cards`. In
test mode, pay a Payment Link with one and **select "failure"** on the
success/failure screen; the payment fails carrying that exact
`error_reason`. Two word-order traps to note, since both bit here:
Razorpay writes `card_number_invalid` and `insufficient_fund`, not
`invalid_card_number` and `insufficient_funds`.

`scripts/seed_error_codes.py` now checks this second list too, and reports
it on its own line. A missing mapping fails the build.

## Deliberate disagreements with Razorpay

Printed by `scripts/seed_error_codes.py` on every run, so it cannot quietly
become an accident. There is one left.

**`payment_cancelled` → `customer_cancelled`, non-retryable.** Razorpay marks
it retryable. The customer explicitly cancelled; chasing them for a payment
they just cancelled is the behaviour a complaint is about. The recovery page
still offers payment (`Explanation.retryable` is true — *the page* stays open),
but the engine does not pursue it on its own. Respecting stated intent outranks
a recoverable rupee.

`payment_risk_check_failed` used to be the second one. It is now resolved —
see below.

## Resolved: `payment_risk_check_failed` (2026-09-02)

It was mapped to `fraud_block` and therefore **abandoned without an attempt**,
while Razorpay documents it retryable and advises *"The customer must retry
with a different card or method"*. Both halves of that sentence matter, and
the old mapping got the first half wrong while the obvious correction would
have got the second half wrong:

- **Abandoning is too strict.** The payment is recoverable on another
  instrument; closing the case throws it away.
- **Plain "retryable" is too loose.** A risk screen refused *this
  instrument*. Re-presenting the same card walks straight back into the same
  screen — a certain decline that still spends one of three attempt slots and
  a contact allowance. That is the same false-retry the `card_number_invalid`
  fix removed.

So the class is `risk_check_failed`: **retryable, switch-only.**

| Layer | Behaviour |
|---|---|
| `src/classifier/taxonomy.py` | `is_retryable` true, new `is_switch_only` true, `is_hard_decline` false |
| `src/guardrail/rules.py:check_switch_only_class` | `retry_now` and `retry_at` are **rejected**; `switch_rail` and `nudge_customer` pass |
| `src/executor/rail_selector.py` | routes to UPI, alongside `3ds_dropoff` / `issuer_decline` / `card_limit_exceeded` |
| `src/customer/explain.py` | *"This payment was stopped by a security check"* — says plainly that the same card will be stopped again |
| `src/customer/routes.py` | UPI-only link: the recommendation is enforced, not decorative |
| `src/agent/prompts.py`, `xgboost_baseline.py`, `policy_agent.py` | LLM heuristic, rule baseline and LLM-failure fallback all move rails or nudge, never retry |

The guardrail rule is deliberately **not** applied in `validate_self_serve`.
A customer picking a different card on the recovery page is doing exactly what
Razorpay's advice says; the gateway page is where they pick it.

**`fraud_block` now has no rule that can produce it**, and
`scripts/seed_error_codes.py` prints that as a warning on every run. It is
kept rather than deleted: the classifier's LLM tail can still emit it, stored
cases carry it, and the eval harness scores it. Deleting a class to silence a
true warning would rewrite history that already happened.

## Deliberately unmapped

**`mobile_number_invalid`** — "the customer is using an invalid or an
unregistered mobile number", a wallet-registration problem. Every existing
`FailureClass` would put visibly wrong words on a money page: `invalid_card`
says *"check the number, expiry and CVV"*; `issuer_decline` says *"your bank
declined… call the number on your card"*. Falling through to `UNKNOWN` renders
the generic *"The payment didn't go through. You can try again below."* —
the only honest thing this taxonomy can currently say.

The cost is real and accepted: `UNKNOWN` is non-retryable, so the engine will
not chase these, while Razorpay calls them retryable. Closing it properly
means a new `FailureClass` with its own copy — a product decision, not a
mapping fix.

## Rules that are not on Razorpay's list

Twenty-odd rules key on reasons the failure-analysis page never names
(`card_stolen`, `do_not_honor`, `invalid_otp`, `upi_collect_timeout`,
`issuer_down`, `vpa_not_found`, …). **None were removed.** That page is a
curated failure-analysis article, not an exhaustive enum, and deleting a rule
that does fire would silently route real declines to `UNKNOWN` and abandon
them. An unused rule costs one dict comparison; a missing one costs the
recovery.

## Keeping this honest

`scripts/seed_error_codes.py` is the check, and it runs in `run.sh` and CI:

- every rule references a real `FailureClass`;
- **every documented reason is matched by name** — probed with an empty
  `error_code` and no source or step, so only `error_reason` rules can fire.
  A realistic 5-tuple would prove nothing, because the catch-alls swallow
  everything; that is exactly how ten reasons hid while the file looked
  complete;
- acknowledged gaps and deliberate disagreements are printed every run;
- **it exits non-zero.** It previously returned `None` whatever it found, so
  `run.sh`'s `&& ok "…"` printed a tick unconditionally and the gate could
  not fail.

The reference list itself lives in `error_codes.yaml` under
`razorpay_documented`, beside the rules it audits, so the audit is a check
rather than a memory.

**When Razorpay publishes new codes**, add them there; the check will fail
until each one is mapped or explicitly acknowledged.
