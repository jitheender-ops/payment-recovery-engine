# Deploying: all of it on Render

The app runs entirely on **Render** — two web services from one Docker image,
plus a Render Postgres. Modal is used only for the LLM eval harness
(`eval/modal_llm_server.py`) and is not part of this path.

The engine needs a resident process: the scheduler is an in-process asyncio
loop that fires deferred retries, expires promises and runs the chase sweeps.
That single fact drives most of what follows.

---

## 1. The database

There is nothing to do. `render.yaml` declares it:

```yaml
databases:
  - name: recovery-db
    plan: free
    databaseName: payment_recovery
    user: recovery
```

Render provisions it with the blueprint and injects the connection string into
both services. `DATABASE_URL` and `DATABASE_URL_SYNC` receive the **same**
value on purpose — `src/config.py` gives each the driver it needs (`+asyncpg`
for the app, plain for Alembic), which is what its `_ensure_sync_driver`
validator exists for.

No pooler is involved, so the `SELECT … FOR UPDATE` row lock in
`orchestrator._get_ledger` behaves, and `DB_BEHIND_POOLER` stays at its default
of `false` — a normal pool with asyncpg's prepared-statement cache left on.

`property: connectionString` is the **internal** address, which resolves only
inside Render's network. The database needs no public endpoint. To reach it
from your laptop, read `externalConnectionString` from the dashboard rather
than changing the blueprint.

### The 30-day catch

**Render's free Postgres is deleted 30 days after creation.** Render warns by
email first, but on expiry the data is gone, not archived. That is the trade
for everything above being automatic.

For a prototype it is usually the right trade: the engine's state is
reproducible (re-push risk events, re-run `scripts/simulate_webhooks.py`) and
nothing here is a system of record yet. When it needs to outlive a month, pick
one:

* **`plan: 0.1c-256mb`** on the database in `render.yaml`. One line, no code,
  no migration, no new vendor.
* **Managed Postgres elsewhere.** See the appendix at the end of this file for
  the Supabase version — it is more setup, and the failure modes are quieter.

---

## 2. Render

New → Blueprint → point at this repo. `render.yaml` defines:

- **recovery-api** — FastAPI. The Razorpay webhook URL **and the whole
  console**: ledger, pipeline, routing, cases, engine, evidence, all under
  `/console/`.
- **recovery-db** — the Postgres from section 1.

There is no second service. The Streamlit dashboard used to be one; everything
it showed now lives under `/console/`, so there is one host, one password and
one cold start. `dashboard/` still runs locally for anyone who wants the plotly
charts — it is simply not deployed.

Migrations run in `docker-entrypoint.sh` before uvicorn starts, so
`alembic upgrade head` happens on every boot. It is idempotent.

### Set these by hand (`sync: false`)

Eight values, none of them a connection string.

**recovery-api**

| Key | Notes |
|---|---|
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Account & Settings → API Keys |
| `RAZORPAY_WEBHOOK_SECRET` | you invent it; must match the Razorpay dashboard exactly |
| `RISK_WEBHOOK_SECRET` | HMAC for `POST /risks`; shared with the merchant's systems. **Empty rejects everything** |
| `OPENAI_API_KEY` | a Groq key — `LLM_BASE_URL` points at Groq's OpenAI-compatible endpoint |
| `DASHBOARD_PASSWORD` | gates the whole console. One service now, so one value |
| `MERCHANT_NAME` | the trust anchor in every SMS — an unnamed payment link reads as phishing |
| `SUPPORT_WHATSAPP` | optional; empty hides the button |

`openssl rand -hex 32` generates the two you invent.

`API_KEY`, `RECOVERY_LINK_SECRET`, `PII_MASK_SECRET` and `AUDIT_CHAIN_SECRET`
are `generateValue` — Render mints them once and they never touch git.
`DATABASE_URL`, `DATABASE_URL_SYNC` and `PUBLIC_BASE_URL` are injected by the
platform.

**`AUDIT_CHAIN_SECRET` is a boot requirement, not a nicety.** Outside
development the service refuses to start without it
(`Settings.require_production_integrity`). The `case_events` hash chain is
keyed, so an unkeyed deployment leaves every audit row unstamped while the
console renders perfectly and money still moves — the tamper-evident trail a
compliance reviewer is shown would not exist. Never rotate it casually:
rotating invalidates every stamp already written, and the chain reads as broken
until you re-stamp with `python scripts/audit_chain.py --stamp`.

`ANTHROPIC_API_KEY` is wired but empty by default. The blueprint ships
`LLM_PROVIDER=openai` against Groq's free tier; to run the policy agent on
Claude instead, set that key, flip `LLM_PROVIDER` to `anthropic`, clear
`LLM_BASE_URL`, and set `LLM_MODEL` to a `claude-*` id. **With no key for the
selected provider the engine still works** — every decision falls back to the
XGBoost baseline — so the boot log says so explicitly rather than letting it
pass for an LLM that is running.

### Free plan: what it costs you, and how to live with it

**This deployment runs on the free plan.** That is a deliberate prototype
trade, and it has one consequence worth understanding before you demo.

Render's free web services sleep after ~15 minutes without traffic, and the
scheduler is an in-process asyncio loop. While asleep:

- a `retry_at` scheduled for +4h fires whenever something next wakes the
  service, not at +4h;
- `expire_promises`, `remind_promises` and the chase sweeps stop on the same
  clock;
- a cold start can outrun Razorpay's webhook timeout.

**Nothing is lost.** A scheduled attempt stays `scheduled` and fires late; a
timed-out webhook is not a 200, so Razorpay re-sends it, and
`reconcile_events` picks up anything stored but unprocessed. What you lose is
punctuality: "retry in 4 hours" becomes "retry in 4 hours, or whenever
someone next visits".

**Keeping it awake (optional, zero code).** Point a free external cron
(cron-job.org, UptimeRobot) at `GET /health` every 10 minutes. Two caveats:
the free tier is ~750 instance-hours a month and a month is 744, so one
always-on service consumes essentially the whole allowance — which is now the
whole deployment, since there is only one service. And it is keeping a service
awake by pretending to be traffic, which is fine for a prototype and is not a deployment posture.

**Demoing the chasers.** Cart, subscription, invoice and mandate recovery all
run on the tick, so hit `/health` a minute beforehand. The console's heartbeat
strip tells you whether the engine is actually ticking before you show
anything that depends on it.

**When this stops being a prototype:** put `recovery-api` on the smallest paid
instance (`plan: 0.5c-512mb`). One line, no code — the scheduler is a core feature and a
service designed to sleep is the wrong host for it. Splitting the scheduler
into its own Render worker is the step after that, and the seam already
exists: `SCHEDULER_ENABLED` gates the in-process loop, and
`docker-entrypoint.sh` already dispatches on a mode argument. It buys
isolation (a slow sweep stops adding latency to webhook handling) and unpins
`WEB_CONCURRENCY=1`, which is set partly to avoid N scheduler loops. Neither
is worth doing before one instance stops being enough.

---

## 3. Point Razorpay at it

Webhook URL: `https://<recovery-api>.onrender.com/webhooks/razorpay`
Events: `payment.failed`, `payment.captured`
Secret: the same value as `RAZORPAY_WEBHOOK_SECRET`.

`payment.captured` is not optional — it is how recovered money gets attributed
back to the case that earned it.

---

## 4. Verify

One command checks every claim this document makes. It writes nothing, so it
is safe against a live deployment:

```bash
python scripts/check_deployment.py \
    --host https://<recovery-api>.onrender.com \
    --password "$DASHBOARD_PASSWORD"
```

It verifies the service is reachable, the public landing renders, **the console
actually redirects when signed out**, `/risks` **rejects an unsigned event**,
`/docs` is closed, the database is readable, and the scheduler has ticked. Exit
code 0 only if every required check passed, so it also works in a deploy hook.
Each of those is a promise made in this file or in PRODUCT.md, and each fails
quietly — the page still renders either way.

Then drive traffic through it:

```bash
python scripts/simulate_webhooks.py --host https://<recovery-api>.onrender.com --count 24
python scripts/run_risk_batch.py    --host https://<recovery-api>.onrender.com --count 24
```

Doing it in that order matters: if the console is ungated or `/risks` is taking
unsigned events, you want to know before you push data in, not after.

### 4a. Promise-backed autopay stays OFF until you prove it

`PROMISE_MANDATE_ENABLED=false` ships in the blueprint. Turning it on needs
exactly one thing: **Razorpay Recurring Payments enabled on the account.**
Without it `registration_link.create` and `payment.createRecurring` fail, and a
page that offers a customer autopay it cannot charge has made a promise of its
own that it will break.

Two things that are NOT prerequisites, and why:

- **No webhook event registration is required for confirmation.** An earlier
  draft matched Razorpay's event names to learn that a mandate had been
  authorised, and the reachable documentation never carried the event
  catalogue — so it held a guessed list that would have silently never
  collected if the real names differed. That is gone. `reconcile_pending_mandates`
  asks the gateway for the token's own status each tick
  (`RetryExecutor.fetch_mandate_status`), which is authoritative and works
  whether or not any webhook is registered. You still want
  `payment.captured` registered, as ever, for attribution.
- **`MANDATE_MAX_AUTO_DEBIT_PAISE` is already correct at ₹15,000.** That is
  RBI's general limit for debits exempt from per-transaction authentication,
  which is what merchant collection is. The raised ceilings are
  category-specific (card bills, insurance, mutual funds). Leave it alone
  unless you operate in one of those.

### 4b. The one that proves it is real

Both scripts above POST events we constructed. They exercise the whole engine,
but the gateway never spoke — so they cannot prove Razorpay is wired to this
deployment at all. One real payment does, and it costs nothing:

1. Mint a Payment Link from the Razorpay **test** dashboard (or let the engine
   mint one) and open it.
2. Pay with a **forcing test card** from `src/classifier/error_codes.yaml`
   (`razorpay_test_cards`) and pick **failure** on the success/failure screen.
   Razorpay emits that exact `error_reason`.
3. Razorpay — not us — POSTs `payment.failed` to
   `/webhooks/razorpay`, signed with the real secret.

Watch `/console/live`: a case opens, the classifier lands the reason the card
forced, the agent decides, the guardrail records what it checked. Then pay the
recovery link the engine sends and Razorpay fires `payment.captured`, which is
what attributes the money back to the case.

That round trip — real gateway, real signature, real webhook, real
attribution — is the only evidence that the deployment is live rather than
merely running. Nothing in it is simulated except the money.

**Confirm the audit chain is actually being stamped**, since it is the one
subsystem that fails silently:

```sql
SELECT count(*) FROM case_events WHERE event_hash IS NULL;
```

Should settle at 0 within one scheduler tick (60s). Anything else means
`AUDIT_CHAIN_SECRET` changed under a chain that was already stamped.

Then open `/console/live`, sign in with `DASHBOARD_PASSWORD`, and read the
heartbeat strip at the top of the page:

- **Engine running** — the scheduler is ticking; every figure below is current.
- **Engine stopped** — no sweep has run recently. Check `SCHEDULER_ENABLED` and
  whether the service is asleep on the free plan.

An empty ledger says so explicitly. Zeros would be indistinguishable from a
broken deployment, which is why the console refuses to render them.

---

## Notes

**`WEB_CONCURRENCY=1`.** Two things assume it. Each worker runs its own
scheduler loop — safe (every sweep claims rows with a conditional UPDATE) but
wasteful — and the recovery page's rate-limit buckets are per-process, so N
workers quietly multiply every per-IP limit by N.

**`BEHIND_TRUSTED_PROXY=true` and `TRUSTED_PROXY_HOPS=1`.** Render terminates
TLS at its proxy and appends the client to `X-Forwarded-For`. Without these the
rate limits key on the socket peer — Render's load balancer — making the whole
internet one client. Raise the hop count to 2 if you put a CDN in front.

**`PUBLIC_BASE_URL`** comes from the service itself, so links work the moment
it exists. Override it with a custom domain when you have one: an
`onrender.com` host in an SMS asking for money is not a link most people trust.

**Rotating `DASHBOARD_PASSWORD`** invalidates every open console session — it
is the cookie's signing key, not just a compared string.

---

## Appendix: managed Postgres elsewhere (Supabase)

Only needed when the 30-day free database is not enough and you would rather
not pay Render for one. It is more setup and the failure modes are quieter, so
prefer `plan: 0.1c-256mb` unless you have a reason.

Delete the `databases:` block from `render.yaml`, change the three
`fromDatabase:` entries to `sync: false`, add `DB_BEHIND_POOLER=true` to
`recovery-api`, and set two strings by hand.

**Finding them.** The `Connect` button in the Supabase project header — not
Settings. The panel lists three connection strings; the two pooler entries are
otherwise identical, so read the **port**, not the label.

| Variable | Which string | Host | Port |
|---|---|---|---|
| `DATABASE_URL` | Session pooler | `…pooler.supabase.com` | **5432** |
| `DATABASE_URL_SYNC` | Direct connection | `db.<ref>.supabase.co` | 5432 |
| — | Transaction pooler | `…pooler.supabase.com` | **6543 — never** |

Each arrives with a literal `[YOUR-PASSWORD]` placeholder to substitute
(Settings → Database → Database password → Reset if you no longer have it).
URL-encode any of `@ : / ? # [ ] %` in that password. Otherwise paste unedited —
`src/config.py` normalises the driver.

**Never the transaction pooler on 6543.** `orchestrator._get_ledger` holds a
`SELECT … FOR UPDATE` row lock to close the contact-limit TOCTOU, and a row
lock only means anything while one transaction keeps one backend. Under
transaction pooling two concurrent webhooks can both read "4 of 5 contacts
used" and both send — silently, and only under load.

**Check the direct connection is reachable.** On Supabase's free tier it is
typically IPv6-only (IPv4 is a paid add-on) while Render's outbound is IPv4, in
which case the API boots, runs `alembic upgrade head`, and dies on a connection
error before uvicorn starts. If so, point `DATABASE_URL_SYNC` at the **session
pooler** as well: session mode holds one backend per client for the life of the
connection, so DDL and transactions behave. `alembic/env.py` is that variable's
only consumer inside the app, and `DB_BEHIND_POOLER` shapes only the async
engine. "Direct for DDL" is a metering preference, not a correctness rule.

**Turn the Data API off** (Settings → API). This app speaks Postgres directly
through SQLAlchemy and never uses supabase-py, PostgREST or RLS. Leaving it off
removes a public HTTP surface onto tables holding customer emails and phone
numbers, and costs nothing.
