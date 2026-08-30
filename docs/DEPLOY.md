# Deploying: Render + Supabase

The app runs on **Render** (two web services from one Docker image) against
**Supabase Postgres**. Modal is used only for the LLM eval harness
(`eval/modal_llm_server.py`) and is not part of this path.

The engine needs a resident process — the scheduler is an in-process asyncio
loop that fires deferred retries, expires promises and runs the chase sweeps.
That single fact drives most of what follows.

---

## 1. Supabase

Create the project, then collect **two different connection strings**. Getting
these the right way round is the whole integration.

| Variable | Which string | Where |
|---|---|---|
| `DATABASE_URL` | **Session pooler**, port **5432** | Connect → Session pooler |
| `DATABASE_URL_SYNC` | **Direct connection**, port 5432 | Connect → Direct connection |

```
DATABASE_URL      postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres
DATABASE_URL_SYNC postgresql://postgres:<pw>@db.<ref>.supabase.co:5432/postgres
```

Paste them unedited — `src/config.py` normalises each to the driver it needs
(`+asyncpg` for the app, plain for Alembic).

**Not the transaction pooler on 6543.** `orchestrator._get_ledger` holds a
`SELECT … FOR UPDATE` row lock to close the contact-limit TOCTOU, and a row
lock only means anything while one transaction keeps one backend. Under
transaction pooling two concurrent webhooks can both read "4 of 5 contacts
used" and both send — silently, and only under load.

**`DATABASE_URL_SYNC` must be direct.** Alembic runs DDL on boot; DDL does not
go through a pooler.

**Turn the Data API off** (Settings → API). This app speaks Postgres directly
through SQLAlchemy and never uses supabase-py, PostgREST or RLS. Leaving it
off removes a public HTTP surface onto tables holding customer emails and
phone numbers, and costs nothing.

---

## 2. Render

New → Blueprint → point at this repo. `render.yaml` defines:

- **recovery-api** — FastAPI. This is the Razorpay webhook URL and the
  merchant console.
- **recovery-dashboard** — the Streamlit ops console.

Migrations run in `docker-entrypoint.sh` before uvicorn starts, so
`alembic upgrade head` happens on every boot. It is idempotent.

### Set these by hand (`sync: false`)

**recovery-api**

| Key | Notes |
|---|---|
| `DATABASE_URL` | Supabase **session pooler** |
| `DATABASE_URL_SYNC` | Supabase **direct** |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | |
| `RAZORPAY_WEBHOOK_SECRET` | must match the Razorpay dashboard |
| `RISK_WEBHOOK_SECRET` | HMAC for `POST /risks`; shared with the merchant's systems |
| `OPENAI_API_KEY` | a Groq key — `LLM_BASE_URL` points at Groq's OpenAI-compatible endpoint |
| `DASHBOARD_PASSWORD` | gates the console; **same value** on both services |
| `MERCHANT_NAME` | the trust anchor in every SMS — an unnamed payment link reads as phishing |
| `SUPPORT_WHATSAPP` | optional; empty hides the button |

`API_KEY`, `RECOVERY_LINK_SECRET` and `PII_MASK_SECRET` are `generateValue` —
Render mints them once and they never touch git.

**recovery-dashboard**

| Key | Notes |
|---|---|
| `DATABASE_URL_SYNC` | the **session pooler** string, not the direct one — this service only reads, and Supabase meters direct connections |
| `DASHBOARD_PASSWORD` | same value as the API |

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
always-on service consumes essentially the whole allowance — let
`recovery-dashboard` sleep. And it is keeping a service awake by pretending to
be traffic, which is fine for a prototype and is not a deployment posture.

**Demoing the chasers.** Cart, subscription, invoice and mandate recovery all
run on the tick, so hit `/health` a minute beforehand. The console's heartbeat
strip tells you whether the engine is actually ticking before you show
anything that depends on it.

**When this stops being a prototype:** put `recovery-api` on Starter
(`plan: starter`). One line, no code — the scheduler is a core feature and a
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

```bash
curl https://<recovery-api>.onrender.com/health
```

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
