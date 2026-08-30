# PRODUCT

## What this is

A payment-failure recovery engine for Indian merchants on Razorpay. Failed
card/UPI charges, abandoned checkouts, stalled subscriptions, overdue invoices
and bounced mandates each leak revenue quietly; the engine opens a case for
each, decides when and how to chase, and recovers the money through signed,
expiring payment links on the rail most likely to clear.

It also handles the reply. A promise to pay buys silence until its date; a
plan splits the amount into instalments, each its own promise; a disputed
invoice freezes the case and goes to a human; a Hinglish voice call answers
what the customer asks about their own case, grounded in that case's facts.
A B2B buyer is escalated by role, one contact per account per rung.

## Audience & scene

The merchant who owns the leaking revenue: a solo founder or small finance/
ops team in India, checking recovery on a laptop in daylight, sometimes phone.
They are not payment experts; they are experts in their own customers.

## Who uses what

- **Merchant console** (`/console`, `/console/live`, `/console/login`) — the
  merchant's surface: a public explanation of what recovers, what happens when
  the customer replies, and a password-gated live ledger of their own money.
  This is the primary UI. The live page leads with the engine heartbeat, then
  the outstanding balance, then the worklist of things automation refused —
  a frozen dispute, a case out of attempts, a stuck voice call — because what
  it refused is what actually needs a person.
- **Customer recovery page** (`/recover/<token>`) — the end customer's one
  page, reachable by SMS link; English/Hindi, trust-critical.
- **Ops dashboard** (Streamlit `:8501`) — the operator's internal console.
- **API** — `POST /risks` (HMAC-signed merchant events), Razorpay webhook.

## Truth the UI must keep

- Bounds are enforced, not aspirational: touches/window/rail per chaser come
  from `src/chasers/policy.py`; the payment rail's bounds from config.
- Recovered means paid through a link the engine sent — self-payments are
  counted but never claimed.
- The live console is aggregate and PII-free: totals, counts, and the
  merchant's own references; never a customer email, phone or id.
- Empty and unreachable states are honest; the console never shows a invented
  number. Gating fails closed on an unset password.
- The engine states only what it enforces. Chase bounds render from
  `src/chasers/policy.py`, escalation rungs from `src/receivables/ladder.py` —
  read from the enforcing structure, never restated in copy that can drift.
- The voice agent answers only from the case's own facts and abstains rather
  than inventing an amount or a date. An opt-out ends the call.
- No WhatsApp yet — the console and the customer page say "coming", never
  imply it works. No invented claims, no unsourced benchmarks.

## Commitments

Premium-fintech-light direction, pinned by the user in a structured interview
(2026-08-28): light paper world, ink + one deep recovery-green, restrained
pure-sans typography, balanced density, full choreography, long-form landing,
ledger rows, oversized login. Recorded in
`docs/design-system/recovery-console/MASTER.md` (v2).

The live console has since moved from a card grid to a worklist-led ledger:
the card grid gave a vanity total and a frozen dispute the same visual weight
several screens apart, and the whole point of bounded automation is that what
it refuses is the important half.
