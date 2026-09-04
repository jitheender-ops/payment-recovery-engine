# Recovery Console — Design System (v2, "Ledger on paper")

Replaced 2026-08-28. v1 (dark slate + gold/purple, GSAP reveals) is the
anti-reference: the merchant found it generic, heavy and templated. v2 is the
committed world; do not polish v1 back in.

## Thesis

The merchant's own ledger, printed on paper. Recovery is bookkeeping brought
home — calm, exact, enforced — not a glowing dashboard. Every surface reads as
a well-kept financial document: warm paper, near-black ink, one deep green that
only ever means recovered money.

## Color (restrained: neutrals + one accent)

| Token | Value | Role |
|---|---|---|
| `--paper` | `#FAFAF7` | page ground (warm white, never pure `#FFF`) |
| `--card` | `#FFFFFF` | cards, table wells |
| `--well` | `#F4F4EF` | recessed sections, hover tints |
| `--ink` | `#15181C` | primary text (≈15:1 on paper) |
| `--ink-2` | `#4A5057` | secondary text (≈8:1) |
| `--ink-3` | `#6B7076` | faint text, labels (≥4.5:1 — floor, don't go lighter) |
| `--line` | `#E8E7E2` | hairline rules |
| `--line-2` | `#DBDAD3` | input/chip borders |
| `--green` | `#0E7A4D` | THE accent: recovered money, primary actions, live state |
| `--green-deep` | `#0B5E3C` | green hover, green text on paper |
| `--red` | `#B3261E` | at-risk money, errors — never decoration |

Green is semantic: it means "money came back" or "go". Never use it as generic
decoration; never introduce a second accent.

## Type

- **Schibsted Grotesk** — display + UI. Weights 400–800; display uses 800 with
  `-0.038em` tracking; section headings 700 `-0.032em`; controls 500/600.
- **Spline Sans Mono** — every number, reference, label and code block, always
  with `tabular-nums`. Mono is for data and measurement, never for prose.
- Display max `4.1rem` (CTA `3.4rem`, login `4.75rem`); body 16px/1.65 at
  ≤68ch measure; more space above a heading than below it.

## Components

- **Buttons**: pill radius; primary = green fill/white text; ghost = hairline
  border. Focus ring: 2px green, 2–3px offset.
- **KPI cards**: white card, hairline border, soft offset shadow; mono label
  uppercase `.68rem` tracking `.1em`; value mono `1.85rem`; foot note faint.
  Recovered value green, at-risk red.
- **Ledger rows** (landing): 52px icon well + name/blurb + right-aligned mono
  bound chips; hairline dividers; hover → `--well`.
- **Tables**: mono uppercase `.66rem` headers in `--ink-3`; right-aligned
  tabular numerals; money green/red by sign of meaning; hover row tint.
- **Code**: dark block (`#161A1E`) on the light page — the one dark surface,
  reserved for the thing you paste.

## Navigation (added 2026-09-04, v2.1)

The console outgrew a flat strip at ten pages. Grouped into the four
questions it answers — **Recovery · Receivables · Customer · Trust** — and
expressed twice, because the available room differs:

- **≥720px**: one strip, groups separated by a hairline (`.subnav-group`,
  `border-left:1px solid var(--line)`). No group labels: there is no vertical
  room and doubling the header to gain them is a bad trade.
- **<720px**: a `<details>` drawer (`.navdraw`) where the labels fit and earn
  their place. `<details>`, never a script — a nav that needs JS to open is
  the worst place for the no-JS rule to break. Summary shows
  "Group · Current page"; the chevron is a rotated border, no icon file.

**Badges** (`.nav-badge`) count only what automation has deliberately stopped
short of — open disputes, open call tasks, unclaimed queued calls. Never
volume. **Never rendered as "0"**: the absence is the message, and a zero
badge trains the reader to stop looking at badges. Unreadable counts render
*no* badge rather than zeros, since a "0 disputes" a merchant believes is
worse than nothing.

## Shared macros (`_console_shared.html`)

Four shapes every list or detail page reaches for. Use these rather than
hand-rolling a variant, so a page added later inherits the decisions instead
of re-litigating them.

- **`stat_tile(label, value, foot, tone)`** — a figure, its label, and the
  sentence that makes it mean something. `tone` is `""`, `"green"`, `"red"`.
- **`empty_state(title, why, hint)`** — why there is nothing here and what
  would put something here. Never a blank space, never a bare zero.
- **`severity_pill(level, label)`** — `stop` (a person must act) · `wait`
  (it resolves itself, watch it) · `info`. Carries a **word** as well as a
  colour: the AA rule and plain sense — a merchant with a colour deficiency
  reads the label, and print has no colour at all. The dot is redundant by
  design.
- **`filter_bar(base, param, options, current)`** — anchors, not buttons. A
  filter is a place you can link a colleague to, and the back button must
  work.

## Motion (native only — no animation library)

- Hero: staged entrance (`data-stage` 1→5), rise + blur-out, `.95s`
  `cubic-bezier(.16,1,.3,1)`, 100–140ms stagger. The page's authored moment.
- Scroll: sections reveal once (fade + 22px rise), sibling stagger via `--d`.
- Signature: money figures count up once on first view (`data-count`).
- `prefers-reduced-motion`: everything renders final-state, instantly.
- GSAP/CDN is gone on purpose: the choreography is ~50 lines of vanilla JS +
  CSS keyframes and must stay that way unless a real need appears.
- **The one exception is `/model`** (2026-09-03). That page is a scrubbed
  scroll narrative — a pinned stage where scrolling writes one more line of a
  real case record, so the artifact filling in IS the progress indicator —
  and that is genuinely not fifty lines of vanilla.
  GSAP + ScrollTrigger load there and nowhere else; the console and the
  landing stay native. The exception is scoped, not a precedent: it holds only
  while the effect is scroll-*scrubbed* narrative. A fade-in does not qualify.
  Conditions it ships under, all non-negotiable: every layout is authored as
  the final readable state and the pinned variant lives behind a `.gsap-on`
  class JS adds only after the library is confirmed, so a blocked CDN costs
  the animation and not one word; no pinning under 900px or under
  `prefers-reduced-motion`; and the pin's own accessibility cost is paid —
  a `focusin` handler scrolls a beat into view, because a pinned stage is out
  of flow and the browser cannot.

## Non-negotiables

- Contrast ≥4.5:1 for all text (AA); `--ink-3` is the lightest allowed gray.
- Status is never carried by colour alone — every severity colour is paired
  with a word (see `severity_pill`).
- No eyebrows/kickers above headings; no icon-card grids as page structure;
  chasers render as ledger rows, not cards.
- Every state styled: hover, focus, error, empty ("ledger is empty"), DB-down
  ("can't reach the database") — never a fake number.
- Public landing shows product facts and sample figures clearly labeled
  "Sample data"; live numbers live only behind the gated console.
- Browser surfaces belong to the design: selection tint, green caret, themed
  scrollbar, focus-visible, tabular numerals, 3px underline offset.
