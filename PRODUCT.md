# PRODUCT

## What this is

A payment-failure recovery engine for Indian merchants on Razorpay. Failed
card/UPI charges, abandoned checkouts, stalled subscriptions, overdue invoices
and bounced mandates each leak revenue quietly; the engine opens a case for
each, decides when and how to chase, and recovers the money through signed,
expiring payment links on the rail most likely to clear.

## Audience & scene

The merchant who owns the leaking revenue: a solo founder or small finance/
ops team in India, checking recovery on a laptop in daylight, sometimes phone.
They are not payment experts; they are experts in their own customers.

## Who uses what

- **Merchant console** (`/console`, `/console/live`, `/console/login`) — the
  merchant's surface: a public explanation of what recovers and how, and a
  password-gated live ledger of their own money. This is the primary UI.
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
- No WhatsApp, no Hinglish voice, no invented claims, no benchmarks.

## Commitments

Premium-fintech-light direction, pinned by the user in a structured interview
(2026-08-28): light paper world, ink + one deep recovery-green, restrained
pure-sans typography, balanced density, full choreography, long-form landing,
ledger rows, card-grid console, oversized login. Recorded in
`design-system/recovery-console/MASTER.md` (v2).
