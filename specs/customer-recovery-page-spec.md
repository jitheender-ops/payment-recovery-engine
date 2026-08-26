# SPEC — Customer Payment Recovery Page ("the one honest page")

> **Hand this entire document to an AI coder.** It is self-contained: research
> basis, feature contract, acceptance criteria, state machine, route
> contract, i18n catalog, design tokens, and security invariants. Implement
> every feature at production quality; nothing here is aspirational.

---

## 1. What this page is

One customer, one failed payment, one page at `/recover/<token>`. The reader
is on a phone, on Indian mobile data, seconds after a payment failed — often
staring at a debit alert on their lock screen, half-convinced they've been
scammed. The page has one job: answer **"has my money gone?"** in one glance,
then get them to complete the payment with minimum friction and maximum
trust.

**The reader's order of anxiety sets the page order:**

```
0. WHO is asking for money   → merchant name (an unnamed link = phishing)
1. HOW MUCH                  → the amount, huge, monospaced
2. WHERE the money is        → the custody rail (you → in transit → merchant)
3. WHAT happened             → a 3-row timeline in their words
4. WHAT to do                → exactly one primary action + trust strip AT it
5. THE RECORD                → reference/method/bank, folded below
6. THE WAY OUT               → WhatsApp help + opt-out, visible but quiet
```

**Hard constraints (non-negotiable):**

- The URL is the only credential: token is HMAC-signed, expiring, scoped to
  one case, PII-free. No login, ever. Forged/expired/unknown tokens render
  the **identical** 404 page (differences are a probing oracle).
- The SERVER decides the state on every request — never the page, never the
  URL. The page cannot show "received" unless the database says recovered.
- **Never offer a second payment while the first might be alive.** While any
  attempt is `pending`, the page shows "confirming" and refuses to pay.
- One response for every token failure (forged = expired = unknown = 404).
- `noindex, nofollow`, `referrer: no-referrer` (the URL is a credential).
- No card fields on-page — payment happens on Razorpay's hosted checkout
  (PCI boundary stays with the gateway).
- Works with JS disabled; JS is progressive enhancement only.

---

## 2. Research basis (why these features)

| # | Finding | Source | Feature it drives |
|---|---|---|---|
| R1 | Each extra step loses 20–30% of users in payment-update flows | QuantLedger via ChurnWard | One page, one button, token auth (no login) |
| R2 | 70% of payment actions happen on mobile | Churn Buster | Mobile-first, 44px targets, one-hand use |
| R3 | SMS CTR 19% vs email 4%; day-0 contact recovers most | Churnkey / Baremetrics (1M+ emails) | Page loads from SMS → must be instant, <20KB |
| R4 | Vernacular UI drives trust; English error messages cause panic + abandonment for Tier-2/3 users | Razorpay 2026 checkout research; 2025 UPI app UX study | Language toggle + Accept-Language auto-detect |
| R5 | Interface design language drives security perception as strongly as the backend | 2025 UPI UX study (GPay/PhonePe/Paytm/BHIM) | Trust strip AT the button, merchant name hero |
| R6 | 18% abandon over security concerns; trust badges at hesitation points lift conversion | Baymard / Cashfree | Lock + "Secured by Razorpay" + card-network marks at the CTA |
| R7 | Personalization (name, order, failed method) = +26% opens, 62% more responses | Slicker | Show order ref, method, bank, failed time |
| R8 | Honest deadlines ("link works until…") outperform fake countdowns; link expiry timers speed payment | Loopwork | Surface the token's REAL expiry |
| R9 | UPI context-switch is India's biggest drop point; OTP steps cause card drop-offs | Razorpay mobile-checkout research | UPI-recommended failures get "Pay by UPI" verb + UPI-only link |
| R10 | A visible opt-out raises trust and conversion for everyone else; human escalation paths save churn | Loopwork / Churnkey | On-page opt-out + WhatsApp help |

---

## 3. Feature contract (implement ALL of these)

### F1. Merchant identity hero — P0
- Masthead: `[MERCHANT NAME] · PAYMENT RECOVERY` + language toggle.
- Hero line above the amount: *"About your payment to **{merchant_name}**"*.
- `merchant_name` comes from `MERCHANT_NAME` env (public info). Empty →
  omit the hero line and fall back to a neutral masthead; never block the
  page on a missing name.
- **Accept:** merchant name appears above the fold; without it the page
  still renders.

### F2. Hero answer: "has my money gone?" — P0
- The amount + a custody rail (three stops: Your account / In transit /
  Merchant) with a written verdict sentence. The rail is the page's one bold
  element; it renders exactly the server's state and nothing else.
- For every non-recovered state the verdict includes the literal sentence
  **"No money has left your account."** (Indian issuers place temporary
  holds on declines; naming the 3–5 working-day release is the difference
  between a reassured customer and a support ticket.)
- **Accept:** every state renders a written verdict (not colour alone);
  `role="img"` + `aria-label` on the rail; non-recovered states carry the
  money-safety sentence.

### F3. Trust strip at the CTA — P0
- Directly under the pay button: lock SVG (inline, no emoji/webfont) +
  "Payments secured by Razorpay" + `UPI · VISA · MASTERCARD` text marks.
- **Accept:** strip renders only on the payable state, adjacent to the CTA.

### F4. Real expiry line — P0
- Under the trust strip: *"This link works until **Sat, 28 Aug, 11 AM**."*
- The instant is the token's actual expiry (= the consent-window close),
  decoded from the token after signature verification, rendered in IST.
- **Accept:** present on payable state; absent if expiry can't be decoded;
  no fake countdowns anywhere.

### F5. "What happened" timeline — P1
- Three rows on a vertical rule: (1) Payment attempted · method · bank ·
  time, (2) The bank declined it — {plain-language reason}, (3) Retrying is
  safe. No money has left your account. (green dot = safe row).
- The reason comes from a failure-class explanation table — 13 classes, each
  with `headline` (their words, never ours), `money` (where the money is),
  `next` (the one thing to do), `retryable` (may we offer another attempt).
- **Accept:** payable state shows the timeline; non-retryable classes
  (fraud block, expired card, permanent decline) NEVER get a pay button —
  they get "call the number on your card" style guidance instead.

### F6. UPI-recommended primary verb + enforced rail — P1
- For failure classes `{3ds_dropoff, card_limit_exceeded, issuer_decline,
  insufficient_funds, invalid_card, expired_instrument}` the button reads
  **"Pay ₹X by UPI"** and the payment link created on submit is **UPI-only**
  (`upi_link: true` on Razorpay). Other classes: "Pay ₹X securely" + generic
  link.
- The recommendation is ENFORCED at link creation, not decorative.
- **Accept:** button verb matches the class; the created link carries
  `upi_link: true` iff the rail is UPI; `notes.target_rail` is recorded.

### F7. Language toggle + auto-detect — P1
- Catalogs: `en` (source of truth) + `hi` (Hindi). Resolution order:
  explicit `?lang=` (support walks-throughs) → `Accept-Language` header →
  English. Toggle in the masthead shows the OTHER language's native name.
- Missing keys degrade to English, never blank. Amounts/dates stay
  locale-neutral. Hindi strings need native review before a real send —
  machine-grade phrasing undermines the trust the page builds.
- **Accept:** `?lang=hi` renders Hindi chrome; `Accept-Language: hi-IN`
  auto-detects; default is English; `<html lang>` matches.

### F8. Honest confirming/unknown states — P1
- `confirming` (an attempt is `pending`, <15min old): "Don't pay again yet…
  Paying now could charge you twice." One automatic re-check after ~8s via
  meta-refresh with a `?r=1` flag (single shot — no infinite loop), then the
  honest *"We'll message you the moment it's confirmed."*
- `unknown` (pending >15min): same refusal + "check your bank statement or
  contact support with the reference below."
- **Accept:** neither state contains the word "securely" in a button; both
  offer "Check again"; the refresh fires exactly once.

### F9. WhatsApp help deep-link — P1
- `SUPPORT_WHATSAPP` env (digits only, wa.me format). If set: a quiet button
  "Talk to us on WhatsApp" pre-filled with the order reference, plus a
  WhatsApp link in the footer. If empty: footer falls back to "reply to the
  message we sent you and quote {reference}".
- **Accept:** button present iff configured; opens `wa.me/<number>` with
  `rel="nofollow noopener"`.

### F10. On-page opt-out — P1
- Quiet centered link-button under the details: "Don't contact me about
  this payment" → `POST /recover/<token>/optout` → withdraws consent AND
  closes every open case for the customer (not just this message), then
  redirects back; the page now reads "We've stopped contacting you" and can
  never offer payment again.
- **Accept:** POST closes the case (state `opted_out`), the page confirms,
  and a subsequent visit offers nothing.

### F11. The record — P1
- "This payment" register (definition list, monospaced, right-aligned):
  Reference · Method · Bank · Attempted date. Plus a folded `<details>`:
  "Is it safe to pay here?" → Razorpay handles payment; card details never
  seen or stored by us; link expires on its own.
- **Accept:** present in all states; the safety explainer only on payable.

### F12. Performance & accessibility floor — P1
- Server-rendered single HTML document, one paint, no framework, works with
  JS disabled. Two small web fonts load async and the page is COMPLETE
  without them (system stacks as fallback). Target <20KB above the wire.
- Light + dark mode via `prefers-color-scheme` (a payment page blasting
  pure white at someone checking their phone at 1am is a defect).
- 17px base text, 56px primary button, visible focus rings, contrast ≥4.5:1,
  `prefers-reduced-motion` kills all animation, responsive at 320–1440px.

---

## 4. State machine (server-computed per request)

| State | Condition | Pay button? | Copy anchor |
|---|---|---|---|
| `recovered` | case recovered or `amount_recovered >= amount_at_risk` | NEVER | "Payment received… you can close this page." |
| `confirming` | latest attempt `pending` and <15min old | NEVER | "Don't pay again yet… could charge you twice." + one auto re-check |
| `unknown` | latest attempt `pending` and ≥15min old | NEVER | "We can't confirm this payment yet… check your bank statement." |
| `opted_out` | case state `opted_out` | NEVER | "We've stopped contacting you" |
| `stopped` | case `exhausted`/`abandoned`/`expired` | NEVER | failure explanation + "no longer retrying automatically" |
| `not_retryable` | failure class is non-retryable (fraud block, expired instrument…) | NEVER | failure explanation + bank-guidance next step |
| `payable` | everything else | YES (single) | failure explanation + timeline + trust strip + expiry |

Order of checks matters: `confirming` is evaluated BEFORE the payable branch
— that ordering is what prevents a second charge. Non-retryable classes map
to `stopped`-like treatment even when the case is technically open.

---

## 5. Route contract

```
GET  /recover/<token>            page (404 for forged/expired/unknown — identical bodies)
     query: ?lang=en|hi  ?r=1   (r=1 = the one auto-refresh already happened)
POST /recover/<token>/pay        re-reads case; refuses non-payable; reuses the
                                 live link if one exists (never re-mints);
                                 otherwise creates one with the recommended rail;
                                 303 → Razorpay short_url; on failure 303 →
                                 /recover/<token>?error=1
POST /recover/<token>/optout     withdraws consent + closes all the customer's
                                 open cases; 303 back to the page
```

Both POST routes are rate-limited per client IP (fixed window, e.g. 30 page
views / 6 pay-or-optout starts per minute, 429 beyond). No CSRF token: the
capability token in the path IS the authority — whoever can forge the POST
already holds the token and could just open the page.

---

## 6. i18n catalog (en source of truth; hi provided — native review before send)

```
masthead            "Payment recovery"                    "भुगतान वसूली"
hero_about          "About your payment to"               "आपका भुगतान"
rail_you            "Your account"                        "आपका खाता"
rail_transit        "In transit"                          "रास्ते में"
rail_merchant       "Merchant"                            "व्यापारी"
sec_what_happened   "What happened"                       "क्या हुआ"
sec_what_to_do      "What to do"                          "क्या करें"
sec_this_payment    "This payment"                        "यह भुगतान"
sec_help            "Need a hand?"                        "मदद चाहिए?"
timeline_attempted  "Payment attempted"                   "भुगतान की कोशिश हुई"
timeline_result     "The bank declined it"                "बैंक ने भुगतान रोक दिया"
timeline_safe       "Retrying is safe. No money has left your account."
                                                          "दोबारा कोशिश करना सुरक्षित है। आपके खाते से कोई पैसा नहीं गया है।"
pay_securely        "Pay {amount} securely"               "{amount} सुरक्षित रूप से भुगतान करें"
pay_upi             "Pay {amount} by UPI"                 "{amount} UPI से भुगतान करें"
pay_opening         "Opening Razorpay…"                   "Razorpay खुल रहा है…"
pay_note            "Opens Razorpay. You'll come back here once it's done."
                                                          "Razorpay खुलेगा। भुगतान होते ही आप यहीं वापस आ जाएंगे।"
pay_other_methods   "You can choose a different payment method on the next screen."
                                                          "अगली स्क्रीन पर आप कोई और भुगतान तरीका चुन सकते हैं।"
trust_secured       "Payments secured by Razorpay"        "भुगतान Razorpay के ज़रिए सुरक्षित है"
trust_never_stored  "Your card details are never seen or stored by us"
                                                          "आपके कार्ड की जानकारी हम कभी देखते या सेव नहीं करते"
expires_line        "This link works until {when}."       "यह लिंक {when} तक चलेगा।"
check_again         "Check again"                         "फिर जाँचें"
confirming_auto     "This page will check again by itself in a few seconds."
                                                          "यह पेज कुछ सेकंड में खुद दोबारा जाँच लेगा।"
unknown_sms         "We'll message you the moment it's confirmed. You don't need to do anything."
                                                          "पक्का होते ही हम आपको संदेश भेज देंगे। आपको कुछ करने की ज़रूरत नहीं है।"
help_whatsapp       "Talk to us on WhatsApp"              "WhatsApp पर हमसे बात करें"
help_footer         "Trouble with this payment? Reply to the message we sent you and quote"
                                                          "भुगतान में दिक्कत है? हमारे भेजे संदेश का जवाब दें और यह हवाला दें"
opt_out             "Don't contact me about this payment" "इस भुगतान के बारे में मुझे संपर्क न करें"
opt_out_done        "We've stopped contacting you"        "हमने आपसे संपर्क करना बंद कर दिया है"
lang_name           "English"                             "हिंदी"
```

Failure-class explanations (13 classes) follow the same four-field contract —
`headline` / `money` / `next` / `retryable`. The `money` sentence is
deliberately near-identical for every class (it is the same true fact; varied
wording would imply the answer changes):

> "No money has left your account. If your bank showed a debit, that is a
> temporary hold and it is released automatically within 3 to 5 working days."

---

## 7. Design tokens

Visual language: **the Indian payment record** — cheque-stock paper, hairline
rules, a monospaced money register, money written the way a statement writes
it. Calm and official; never playful, never salesy, never a dark-pattern in
sight.

```css
:root {
  --slip:#F0F2EF; --field:#FFFFFF;            /* cheque stock / raised field   */
  --ink:#171A18; --quiet:#565E58;             /* note ink / secondary          */
  --rule:#C7CCC6; --rule-soft:#E1E5E0;        /* hairlines                     */
  --hold:#6E5A97; --settled:#0F6B3E; --halt:#A32424;  /* transit / paid / error */
  --hatch:#EDE9F3;                            /* unverified-money hatch fill   */
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
  --display:"IBM Plex Sans Condensed",-apple-system,Roboto,sans-serif;
  --text:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  --sp-1:.25rem; --sp-4:1rem; --sp-6:2rem; --r:3px;
}
/* Dark mode: same language after dark — only tokens move.
   --slip:#141815; --field:#1C211D; --ink:#E8EBE7; --quiet:#99A29B;
   --rule:#39413B; --hold:#A996CE; --settled:#4FBF87; --halt:#E88C8C; */
```

- Amount: `--mono`, 3rem (2rem when >9 chars — a crore must not overflow a
  320px phone), tabular numerals, Indian digit grouping (₹12,34,567).
- Primary button: full-width, min-height 56px, ink-on-field, one per page.
- Unverified money gets a SECOND non-colour channel: diagonal hatch fill
  (survives greyscale, colour-blindness, forced-colors).
- Focus: 2px solid ink outline, 3px offset — never subtle.
- `prefers-reduced-motion`: all animation off.

---

## 8. Anti-features (do NOT build)

- No login walls, no OTP-to-view (the token is the auth).
- No countdown-pressure dark patterns; the only deadline shown is real.
- No cross-sells, marketing, newsletters, social widgets.
- No card entry form — PCI stays on Razorpay's hosted checkout.
- No chatbot widgets, no third-party analytics scripts, no heavy JS.
- No "retry" offered on fraud blocks / expired cards / permanent declines —
  sending someone into a wall their bank built is how a recovery page
  becomes a complaint.

---

## 9. Acceptance checklist (verify before shipping)

- [ ] Forged, expired and unknown tokens return identical 404 bodies.
- [ ] Every non-recovered state carries "No money has left your account."
- [ ] `confirming` and `unknown` contain NO pay affordance; "securely" absent.
- [ ] The pay POST re-reads the case and refuses non-payable states.
- [ ] An existing live link is reused, never re-minted.
- [ ] Merchant name renders above the fold; absent setting degrades gracefully.
- [ ] Trust strip adjacent to the CTA; expiry line shows the real deadline in IST.
- [ ] UPI-recommended classes: "by UPI" verb + `upi_link: true` on the created link.
- [ ] `?lang=hi` renders Hindi; Accept-Language detects; default English; `<html lang>` matches.
- [ ] Opt-out POST closes ALL the customer's open cases; page confirms.
- [ ] WhatsApp button only when configured; `rel="nofollow noopener"`.
- [ ] `noindex, nofollow, no-referrer` meta present; page works with JS disabled.
- [ ] Dark mode renders; reduced-motion kills animation; focus rings visible.
- [ ] Rate limits return 429 per IP beyond the window budget.
- [ ] Total document <20KB; zero third-party scripts.
