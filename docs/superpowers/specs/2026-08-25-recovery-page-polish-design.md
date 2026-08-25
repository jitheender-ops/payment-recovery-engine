# Customer recovery page — polish pass

**Status:** design, awaiting review
**Scope:** `src/customer/` only. The Streamlit ops console is explicitly out.

## Why

`/recover/<token>` shipped as the first surface a paying customer ever sees in
this product. It answers "where is my money" and refuses to take payment in the
four states where paying again would be wrong. That core is sound and this pass
does not revisit it.

What it does not yet do is let a customer *act* in any way except paying. Every
other path — stop contacting me, I already paid, this looks wrong — dead-ends.
For a page that exists to chase someone for money, that asymmetry is the whole
problem: it can ask, and it cannot listen.

## Work items

### 1. Opt-out from the page  (compliance, highest priority)

`src/cases.py::record_opt_out()` already exists, already closes every open case
for the customer, and is already wired into the guardrail. Nothing in the
customer-facing product calls it. A person being chased for money with no way to
say "stop" is the first thing a regulator asks about, and the backend has been
ready the entire time.

- `POST /recover/{token}/stop` → `record_opt_out(session, case.customer_id)`
- Reachable from **every** state, not only the payable one. Someone who wants
  the messages to stop does not care what state their case is in.
- Two-step, because it is irreversible: an opt-out never reopens. A GET
  confirmation view states plainly what stops and what does not (this is not a
  refund, and it does not cancel money already owed).
- Idempotent: opting out twice is a no-op, not an error.

### 2. "I already paid" / dispute path

Two dead ends today. Someone who paid but sees "pay now" has nowhere to go, and
the `unknown` state tells them to contact support without offering a mechanism.

- `GET /recover/{token}/paid` — an information view, not an action. It must NOT
  let the customer mark their own case paid; the capture webhook is the only
  thing that moves that row, and a self-declared "I paid" is precisely the
  client-side claim this page refuses to trust everywhere else.
- Shows the reference to quote, what the system is already doing, and when to
  escalate.
- Linked from `payable` and `unknown`.

### 3. Auto-refresh while confirming

The `confirming` state currently requires tapping "Check again". Every manual tap
is a moment where an impatient customer decides to pay again instead — which is
the exact outcome that state exists to prevent.

- `<meta http-equiv="refresh" content="15">`, on the `confirming` state ONLY.
- Not on `unknown`: once we have stopped claiming to know, refreshing forever
  is theatre.
- Announced visibly ("this page updates on its own"), with the manual button
  kept. An unannounced auto-refresh is disorienting with a screen reader, and
  WCAG 2.2.1 wants the user to know timing exists and retain control.

### 4. Receipt detail on the recovered state

Shows the amount and nothing else. `recovery_cases.recovered_ref` and
`recovered_at` are already populated by attribution — a customer disputing this
later has nothing to quote without them.

- Add reference and timestamp to the recovered view. Plumbing only; no new data.

### 5. Bug: `?error=1` is set and never rendered

`routes.py` redirects with `?error=1` when payment-link creation fails, and
`recover.html` ignores it. The customer gets the same page back with no
explanation and no reason to believe anything happened.

- Read the flag, render a banner: what failed, that no money moved, what to do.
- This is a defect, not an enhancement — it ships regardless of the rest.

## Testing

Extends `tests/test_customer_recovery.py`, which is already structured around
refusals rather than happy paths. New cases:

- opt-out closes the case, is idempotent, and is reachable from every state
- the "already paid" view never mutates case state
- auto-refresh appears on `confirming` and nowhere else
- the error banner renders when `?error=1` and not otherwise
- recovered view shows reference and timestamp when present, degrades when not

## Out of scope

- The Streamlit ops console (hard Streamlit ceiling; separate decision)
- Refund flows — no backend capability exists
- Hindi/Hinglish copy — real, but a translation workstream, not a polish pass
- Any change to the state machine in `_view_state`

## Open question for review

Item 2 deliberately gives the customer no button that changes state. The
alternative — letting them flag "I paid" and surfacing it to ops — needs a
queue and a human to work it, which does not exist. Flagging here in case the
weaker information-only version is not enough.
