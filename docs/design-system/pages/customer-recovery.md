# Customer recovery page — design rules

**Overrides `docs/design-system/recovery-console/MASTER.md` for `src/customer/templates/**`.**

MASTER describes the **Recovery Console**: an operator's dashboard, dark, dense,
glassmorphic, GSAP-animated. This is a different product on a different surface —
one page, one customer, one failed payment — and MASTER's rules are wrong here:

| MASTER rule | Why it does not apply |
|---|---|
| Anti-pattern: "Light mode default" | The reader is anxious, not an operator. A dark technical surface reads as *system internals* exactly when someone needs reassurance. |
| Density 8/10 (dashboard) | This page has four sentences and one button. |
| GSAP + ScrollTrigger reveals | The page must work with JS disabled. Nothing may be invisible-by-default. |
| Glassmorphism, ambient blobs, blur | Cost with no benefit on a mid-range Android on mobile data. |
| Gold `#F59E0B` + purple `#8B5CF6` | Amber reads as *warning* on the one page whose job is saying nothing is wrong. |
| Cards with `cursor:pointer` and hover-lift | Nothing here is a card, and non-clickable things must not claim to be. |

## Tokens

| Role | Hex | Var | Note |
|---|---|---|---|
| Ground | `#F0F2EF` | `--slip` | Cheque stock — cool grey-green, deliberately **not** warm cream |
| Field | `#FFFFFF` | `--field` | |
| Ink | `#171A18` | `--ink` | Near-black, green cast. Also the primary button. |
| Quiet | `#565E58` | `--quiet` | |
| Rule | `#C7CCC6` / `#E1E5E0` | `--rule` / `--rule-soft` | Hairlines |
| Hold | `#6E5A97` | `--hold` | ₹100-note lavender. Money **in transit**. Reads *suspended*, not *dangerous*. |
| Settled | `#0F6B3E` | `--settled` | Money received |
| Halt | `#A32424` | `--halt` | Blocked / errored |

## Type

- **Display** `IBM Plex Sans Condensed` 600 — headlines and buttons. Condensed
  survives long strings at 360px, and has a real Devanagari sibling for when
  Hindi copy lands.
- **Mono** `IBM Plex Mono` 400/500/600 — money, references, timestamps, labels.
  The statement-register voice: machine-attested, not decorative.
- **Body** system stack. Zero network cost, native on the device.

The page is **complete without the web fonts**: body is system, and money falls
back to the platform monospace, which is already tabular. Fonts improve first
paint; they never gate it.

## Signature: the custody rail

A three-segment track — `Your account · In transit · Merchant` — with one filled
block whose **position** is the case state.

| `state` | Position | Fill |
|---|---|---|
| `payable` | Your account | `--ink` (live) |
| `stopped`, `not_retryable`, `opted_out` | Your account | `--quiet` (dormant) |
| `confirming` | In transit | `--hold`, slow breathe |
| `unknown` | In transit | `--hold` **hatched** |
| `recovered` | Merchant | `--settled` |

Rules, non-negotiable:

1. The rail renders `_view_state()` and **nothing more**. It can never show
   *Received* unless the server said `recovered`. No client-side inference.
2. Ink = live, grey = dormant. Colour is the second signal; the legend word and
   the verdict sentence are the first.
3. `unknown` is hatched so uncertainty survives greyscale and colour blindness —
   a yellow badge does not.
4. One animation on the page: the block settling into position, 620ms. Plus a
   breathe on `confirming` only. `prefers-reduced-motion` kills both.

## Dark mode

Both modes, `prefers-color-scheme`, no toggle. Only the tokens move; every rule
is written once. Night ledger: `--slip #141815`, `--field #1C211D`,
`--ink #E8EBE7`, `--quiet #99A29B`, `--hold #A996CE`, `--settled #4FBF87`,
`--halt #E88C8C`, `--hatch #2E2A38`. The rail track drops to `#0F120F` so it
reads as a recess the block sits in rather than a raised surface.

Measured, both modes: body 15.6:1 / 14.9:1, quiet 5.9:1 / 6.8:1, button label
17.5:1 / 13.6:1. Rail fills clear 3:1 against ground in dark.

Forcing light here would ignore a preference the reader set deliberately, and a
payment page that blasts pure white at somebody checking their phone at 1am is a
defect, not a brand decision.

## Type scale & rhythm (2026-08-31 refinement)

`h1` tightened from `1.6rem`/`1.95rem` to `1.5rem`/`1.75rem` (mobile/desktop)
— the jump from body text felt looser than the rest of the page's
deliberate hierarchy. Section-to-section whitespace (`.sec` top margin,
`--sp-6` → `--sp-5`) and footer bottom padding (`--sp-7` → `--sp-6`)
tightened to read as one continuous flow rather than a long scroll of ruled
blocks. `.amount` is untouched — it is the emotional focal point and
already has its own overflow-safe sizing rule below; tightness elsewhere
should never come at its expense. The mono label voice (uppercase, `.68rem`,
wide letter-spacing) is untouched too — it is signature statement/register
typography, not something "modern" should erase.

## CTA & trust-strip weight

The primary button (`.btn`) carries a contact shadow (`--btn-shadow`,
tied to `--ink`'s rgb rather than a flat black, so it reads as this
surface's own shadow) so it is unambiguously the one raised, clickable
thing on the page. Label weight is `700` (was `600`). `.btn-quiet`
explicitly opts back out of both (`box-shadow:none`, weight `600`) so the
one primary action per state stays the only heavy element — a page with
two "loud" buttons has zero. Corner radius stays the sharp `3px` — that is
part of the cheque-stock identity, not something a "modern" pass should
round off.

Dark mode redefines `--btn-shadow` rather than reusing the light-mode
value: a black contact shadow disappears against the `#141815` ground, so
dark mode trades it for a crisp 1px edge definition plus a tighter shadow
instead — still legible as "the raised thing," by a different mechanism.

The trust strip's lock icon is sized up slightly (`14×16`, was the SVG's
raw `12×14`) and its internal gap tightened (`--sp-4` → `--sp-3`) so lock +
"secured" line + payment marks reads as one unit sitting immediately under
the button, at the point of hesitation research says security cues matter
most — weight through proximity, not new elements.

The button gets a subtle `scale(.98)` on `:active` alongside the existing
colour change — a tactile "checkout" press. Governed by the same blanket
`prefers-reduced-motion` kill-switch as every other transition on this page
(`*{transition:none!important}`), so it degrades to an instant state change,
not motion, when that preference is set.

## Amount sizing

Mono advance width is fixed and a crore has five more glyphs than a thousand, so
`₹12,34,56,789` at `3rem` runs off the right edge of a 320px phone. The template
adds `.amount-long` when the formatted string exceeds 9 characters (`2rem`, or
`2.8rem` above 33rem). Common amounts keep full display size. Verified rendered
at 320 / 360 / 390px.

## Structure

`amount → rail → verdict → headline → where your money is → what to do →
action → register → safety`. That is the reader's order of anxiety, not ours.

Section heads are ruled mono labels — statement vernacular, and true: this page
is a record. **Not numbered.** Nothing here is a sequence.

## Promise-to-pay tracker (2026-09-01)

A pending promise is shown persistently, not just as a one-time flash right
after submitting — a customer who returns to the link days later still sees
"You said you'd pay by {date}." plus an honest, server-computed days-left
(or "Due today.", never a negative number). Absent a live pending promise,
if the last one broke, that's said plainly before the promise form is
offered again — same no-hiding rule as every other flash on this page.
Mutually exclusive: never show both at once. No countdown, no urgency
language beyond the real date — same discipline as `expires_line`.

## Retry-sequence panel (2026-09-01, subscription/mandate only)

The "what happened" timeline gains a row per collection touch actually made
(`RetryAttempt.executed_at is not null` — a decision that was only made,
never executed, never renders), plus one hollow "upcoming" row when a next
touch is genuinely scheduled and budget remains.

**The upcoming row states WHEN, never WHAT.** It was tempting to label it
"next attempt" vs "next reminder", but that turns out to be something this
page cannot honestly know: `retry_now`, `retry_at`, `switch_rail` and
`nudge_customer` are all decided live by the agent at the moment it next
acts, not stored ahead of time — even the RBI pre-debit rule
(`src/guardrail/rules.py::check_mandate_predebit_notification`) only
evaluates at execution time, against whatever action the agent picked then.
A page that predicted the verb would eventually be wrong and would have
invented a claim, which is the one thing this page's rules never allow. So
the honest, always-true sentence is "We'll be in touch again around
{date}." — same discipline as `expires_line` and the promise tracker above:
state the real date, claim nothing about content.

## Constraints inherited from the master prompt

- Never display success from a client signal. `recovered` comes from the
  capture webhook only.
- No secrets, keys or internal state in the markup. The URL is the credential:
  `no-referrer`, `noindex`.
- No raw backend errors. `?error=1` renders one written sentence, placed
  directly above the Pay button it refers to.
- **No em-dashes or en-dashes in rendered copy.** "3 to 5 working days", not
  "3-5". Sentences split with a period rather than joined with a dash. Code
  comments are exempt; they are never rendered.
- Frontend guards are courtesy; `routes.py` re-reads the case on every POST.

## Open

Merchant name is not on `PaymentFailure`. When it is plumbed, it replaces
"Payment recovery" in the masthead — the merchant is a better trust anchor
than we are.
