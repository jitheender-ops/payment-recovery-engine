# Session record — security audit and remediation

**Date:** 2026-08-28
**Scope:** `payment-recovery-engine`
**Request:** audit the codebase for loopholes, then fix every finding.
**Outcome:** 400 tests passing, ruff clean, mypy clean, migration chain verified. Nothing committed.

---

## 1. What was asked

Three sequential instructions:

1. Explore the codebase for loopholes.
2. Fix every one found.
3. After a status check mid-repair, fix the pre-existing failures too — not just the regressions introduced during the work.

## 2. Test counts

| Point | Passing | Failing |
|---|---|---|
| Working tree at start | 372 | 9 (pre-existing, from `tests/test_security_attacks.py`) |
| End | 400 | 0 |

The 9 pre-existing failures were closed and 19 new regression tests added in `tests/test_audit_fixes.py`.

> Note: an early "289 passed" baseline measured during this session was **HEAD's**, not the working tree's, and is not a valid comparison. See §7.

---

## 3. Findings and fixes

### High

**H1 — Superseded payment links stayed live on open cases.**
A case that generated a second payment link left the first one payable. A customer could pay both. The engine caught this only afterwards, as an `overpayment` requiring a manual refund. `cancel_payment_link`'s own docstring promised this behaviour; there was no implementation and no caller.

Fix: `cancel_superseded_links()` in `src/scheduler.py`. On every open case, all attempts older than the newest link-bearing attempt are cancelled at Razorpay. Shares one `_cancel_links()` helper with the existing closed-case sweep. Idempotent via `link_cancelled_at` / `link_cancelled_because` stamps; a failed cancellation is left unstamped so the next sweep retries. Registered in `tick()` as `superseded_links_cancelled`.

**H2 — Rate limits and opt-out keyed on an unnormalized, rail-dependent identity.**
The payment rail and the risk rail spelled the same human differently, so one person could hold two ledgers: doubled contact budgets, and an opt-out that closed only half their cases.

Fix: `customer_key()` in `src/cases.py` — one canonical form, precedence `email:` → `phone:` → `id:`, with phone digits stripped of formatting and email lowercased. Both rails now call it. `ledger_keys()` returns canonical *plus* legacy spellings so reads still match un-migrated rows during a rolling deploy; writes always create under the canonical key.

Backfill: `alembic/versions/0006_canonical_customer_key.py`. Rewrites `retry_ledger.customer_id` and `recovery_cases.customer_id`, merging ledger rows that collide under the UNIQUE constraint. Merge policy:
- counters take **MAX**, not SUM — the windows overlap, and summing would lock the customer out of contact entirely;
- window anchors and `last_*` take whichever value keeps the window open longest;
- a single `opted_out` among the duplicates wins outright.

The migration carries its own standalone `_canonical()` copy and deliberately does not import application code — a migration must keep doing what it did on the day it was written.

**H3 — Dashboard password gate had no throttling.** Unlimited guesses against a single shared secret.

Fix: `dashboard/auth.py` — 6 failures in 60s triggers a 300s lockout, during which even the *correct* password is refused (tested). `dashboard/app.py` surfaces the remaining wait.

### Medium

**M4 — Unbounded `retry_at`.** An agent could park a case open indefinitely by scheduling a retry past the consent window — a payment the guardrail itself would refuse to initiate at fire time.
Fix: `check_retry_at_within_window()` in `src/guardrail/rules.py`, wired as gate rule 7 (later rules renumbered 8/9/10).

**M5 — No clickjacking or cache headers on the money page.**
Fix: middleware in `src/main.py` sets `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `Cache-Control: no-store, private`, `Referrer-Policy: no-referrer` on `/recover*` — including on rejected tokens.

**M6 — The recovery bearer token was written to the access log.** The URL *is* the credential.
Fix: a `logging.Filter` on `uvicorn.access` rewrites `/recover/<token>` to a stable 12-char SHA-256 prefix plus `…redacted`. Unrelated paths untouched.

**M7 — Per-IP limits assumed exactly one proxy hop.**
Fix: `trusted_proxy_hops` setting; `_client_ip` counts from the *right* of `X-Forwarded-For`, since each trusted hop appends the peer it saw. A header shorter than the configured hop count falls back to the socket peer rather than trusting a forgeable entry. Documented in `.env.example` and `render.yaml`, with the warning that setting it too low behind a CDN puts the entire internet in one rate-limit bucket.

### Low

**L8 — `_live_link` / `_view_state` read only the newest attempt.** A newer `scheduled` row could hide an in-flight `pending` one, and a cancelled link could still be offered.
Fix: `_load()` returns a 4-tuple and scans up to 20 attempts; `_blocking_attempt()` finds any pending row, `_live_link()` skips links with `link_cancelled_at`.

**L9 — `_escape_kw` was a no-op that corrupted output.** `str.format` never re-processes what it substitutes, so escaping braces in the *values* could not prevent an error — it only rendered a merchant name containing `{` as literal `{{` on the customer's page. Deleted.

**L10 — `joblib.load` is pickle.** Documented, not guarded — see §5.

**L11 — Prompt injection into the customer-visible checkout description.** `method` is webhook-supplied free text and `customer_name` is whatever the merchant's checkout collected; whatever the nudge LLM writes becomes the text the payer reads.
Fix: `_scrub()` in `src/messaging/nudge_generator.py` collapses to one line of printable characters, removing the newlines, fake role markers and braces injected instructions need — without touching legitimate values like `card` or `netbanking`.

**L12 — No webhook body caps.** Fix: `MAX_BODY_BYTES = 1 MiB` and `body_too_large()` in `src/ingestion/signature.py`, checked before and after `await request.body()` on both signed surfaces, returning 413.

---

## 4. Pre-existing failures (`tests/test_security_attacks.py`)

Six real holes, three test bugs.

**Real:**

- `attribute_capture` accepted `amount=-500000`, which **erased recorded recoveries without refunding anything**; `amount=0`, which fabricated an "attributed" audit event and resolved promises as kept; and `amount="lots"`, which raised `TypeError` on `+=`, re-arming the event and consuming reconcile attempts. Now validated at point of use, with `bool` checked before `int`.
- A replayed capture double-credited a case. Guarded before the terminal branch — a replay is not an overpayment; there is no second payment to refund. Razorpay redeliveries are already deduped by `event_id` at ingestion; this is the defence-in-depth layer for the reconcile path.
- `attribute_captured_payload` defaulted a missing amount to `0`. Now refuses.
- `/risks` accepted unbounded `meta` (now 32 keys / 4096 chars) and control characters in identifiers (now rejected). Both 400.

**Test bugs — source was already correct:**

- An XSS assertion looked for an `alert(1)` string its own payload never contained. Jinja escaping was verified working against a live response before the assertion was touched.
- A `FailureContext` was constructed without the required `failed_at`. Fixed in the test rather than defaulting the field — defaulting it would have made every consent window count from "now".
- A 5 MB `meta` blob could only ever hit the 1 MB body cap, which is the sibling test's job. Resized to ~100 KB so it exercises the schema bound it names.

The prompt fence the third test asserted already existed in `src/agent/prompts.py`.

---

## 5. Deliberately not changed

`joblib.load` on `XGBOOST_MODEL_PATH` in `src/agent/xgboost_baseline.py` is pickle deserialization — it executes whatever the file says. Left as-is with a `ponytail:` comment naming the ceiling and the upgrade path: the path is operator-set and the file ships inside the image, so anyone who can write there already has the process. **Pin a checksum before that line if a model ever arrives from outside the build.**

---

## 6. Two things worth a second opinion

**The dashboard lockout is process-global, not per-client.** Streamlit gives that predicate no trustworthy client identity, and there is exactly one shared password — so the thing worth rate-limiting is guesses against that password, not guesses per visitor. Consequence: a burst of wrong entries locks out the legitimate operator for five minutes too. Marked `ponytail:` with the upgrade path.

**Migration 0006's `downgrade()` is lossy and says so.** It strips the prefixes back off, but the ledger merge deleted duplicate rows and lowercased addresses. Rolling back the *code* is safe; rolling back the *data* needs a backup.

---

## 7. Mistakes made during the session

Recorded because they affect how the numbers above should be read.

1. **`git stash push -u` reverted a large body of pre-existing uncommitted work** — `tests/test_security_attacks.py`, `src/chasers/`, `src/ingestion/risk_router.py`, the `recovery_link_ttl_hours` feature. Restored immediately with `git stash pop` and verified. The lesson: the baseline measured at that moment was HEAD's, not the working tree's.
2. **Self-inflicted split-ledger regression.** Canonicalizing inside `_get_ledger` without also matching legacy keys made a legacy row invisible, so a second row was created beside it — reintroducing the exact bug being fixed, in a new form, as `MultipleResultsFound`. Fixed in three parts: `_get_ledger` tries all `ledger_keys()` candidates; `_update_retry_ledger` looks up with the *original* value (pre-canonicalizing there discarded the fallback); `ledger_keys` derives the pre-migration spelling by stripping the prefix, since every caller now passes an already-canonical key.
3. **Duplicated helpers twice** — near-identical `_canonical`/`_ledger_keys` in both `cases.py` and `orchestrator.py`. Both times the orchestrator copy was deleted in favour of importing from `cases.py`, the module that owns identity.
4. **A heredoc wrote literal control bytes** into `_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")`, producing `SyntaxError: source code string cannot contain null bytes` and 5 collection errors. Fixed at the bytes level.

---

## 8. Files touched

**New:**
- `alembic/versions/0006_canonical_customer_key.py`
- `tests/test_audit_fixes.py` (19 tests)

**Modified (source):**
`src/config.py`, `src/cases.py`, `src/orchestrator.py`, `src/scheduler.py`, `src/main.py`, `src/customer/routes.py`, `src/customer/i18n.py`, `src/guardrail/rules.py`, `src/guardrail/gate.py`, `src/ingestion/signature.py`, `src/ingestion/router.py`, `src/ingestion/risk_router.py`, `src/messaging/nudge_generator.py`, `src/agent/xgboost_baseline.py`, `dashboard/auth.py`, `dashboard/app.py`, `.env.example`, `render.yaml`

**Modified (tests — identity spelling, not behaviour):**
`tests/test_cases.py`, `tests/test_chasers.py`, `tests/test_scheduler.py`, `tests/test_security_attacks.py`

---

## 9. Verification

```
pytest    400 passed
ruff      All checks passed!
mypy      Success: no issues found in 50 source files
scripts/check_migrations.py   4/4 PASS — chain consistent with the models
```

## 10. Not done

- `graphify update .` (CLAUDE.md requires it after modifying code)
- No commit — not requested.
