"""
Application configuration via pydantic-settings.

All config is loaded from environment variables (or .env file).
Guardrail thresholds are configurable here so they can be tuned without code changes.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Central application settings — loaded from env vars / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Razorpay ─────────────────────────────────────────────────────────
    # Empty by default so a missing/incomplete .env fails CLOSED.
    # verify_webhook_signature() rejects an empty secret outright, but it will
    # happily validate an HMAC computed with a non-empty default — and a default
    # committed to this repo is a publicly-known key. A placeholder here turns
    # webhook authentication into an open door for anyone who reads the source.
    # key_id is the public half of the pair (it ships in payment link payloads
    # and is logged prefixed at startup) and stays a plain str. The other two
    # are SecretStr: pydantic renders those as `SecretStr('**********')` in
    # repr, so a Settings object reaching a log line, a traceback frame or a
    # FastAPI validation dump does not carry the webhook signing key with it.
    # Read them with .get_secret_value().
    razorpay_key_id: str = ""
    razorpay_key_secret: SecretStr = SecretStr("")
    razorpay_webhook_secret: SecretStr = SecretStr("")
    # Signs merchant-pushed risk events (POST /risks: abandoned carts, failed
    # subscription charges, overdue invoices, failed mandate debits). A
    # DEDICATED secret, not the Razorpay one: the merchant's systems compute
    # this HMAC, and a leak of either key must not be a leak of both. Empty
    # means the /risks surface is OFF and every event is rejected — fail
    # closed, same as the Razorpay secrets above.
    risk_webhook_secret: SecretStr = SecretStr("")
    # Optional network-layer allowlist for POST /webhooks/razorpay, as
    # comma-separated IPs or CIDRs. Razorpay's security guidance recommends
    # allowlisting their webhook source IPs as defence in depth; this is that,
    # in the app, for a deployment with no firewall in front of it.
    #
    # EMPTY MEANS OFF, and that is deliberate — the opposite of the fail-closed
    # rule the secrets above follow. Those guard authentication, and an
    # unconfigured authenticator must refuse everyone. This does not: HMAC
    # signature verification is the authenticator and is always on. An
    # allowlist that defaulted to closed would reject every real webhook the
    # moment someone upgraded without setting it, which is an outage, not a
    # security posture. It only ever narrows what HMAC already guards.
    #
    # Razorpay publishes the current IPs; they change, so this is not shipped
    # with a baked-in list that would silently rot into an outage.
    webhook_ip_allowlist: str = ""
    # Per-request timeout for the Razorpay SDK. requests defaults to no timeout
    # at all, so without this a single hung connection blocks a worker forever.
    # Payment-link creation is a fast call; 10s is generous, not tight.
    razorpay_timeout_seconds: float = 10.0

    # ── LLM ──────────────────────────────────────────────────────────────
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    # Any OpenAI-compatible endpoint. Set this to use OpenRouter, Together,
    # Groq, a local Ollama/vLLM server, etc. through the existing "openai"
    # provider branch — the wire format is identical, only the host differs.
    #   OpenRouter: https://openrouter.ai/api/v1
    #   Ollama:     http://localhost:11434/v1
    # Leave empty for api.openai.com.
    llm_base_url: str = ""
    # Model IDs on current Claude models carry no date suffix — the previous
    # "claude-sonnet-4-20250514" was a dated snapshot of a superseded model.
    llm_model: str = "claude-opus-5"
    # Thinking depth / token spend. This is a constrained classification into a
    # 5-action space, not open-ended reasoning, so "low" is the right tier and
    # keeps 1000s of eval calls affordable. Raise it if decisions look shallow.
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # OpenAI only. Sampling params (temperature/top_p/top_k) were REMOVED on
    # current Claude models and return a 400 — the Anthropic path must not send
    # temperature at all. Depth is controlled by llm_effort instead.
    llm_temperature: float = 0.1
    # Thinking is on by default on Claude Opus 5 and its tokens count toward
    # max_tokens, so 1024 risked truncating the JSON mid-object.
    llm_max_tokens: int = 2048
    llm_timeout_seconds: float = 30.0
    # Off by default. The classifier (src/classifier/mapper.py) is
    # deterministic and stays that way for anything it can match — this only
    # ever fires for the tail it already gave up on (FailureClass.UNKNOWN),
    # and only when explicitly turned on. See src/classifier/llm_tail.py.
    classifier_llm_tail_enabled: bool = False

    # ── Database ─────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://recovery:recovery@localhost:5432/payment_recovery"
    database_url_sync: str = "postgresql://recovery:recovery@localhost:5432/payment_recovery"

    # A connection pooler sits in front of Postgres (Supabase Supavisor,
    # PgBouncer, RDS Proxy). Shrinks the app-side pool — the pooler is already
    # the pool, and a second one under it holds server-side connections
    # hostage — and disables asyncpg's prepared-statement cache, which a
    # TRANSACTION-mode pooler breaks: statements are prepared by name on a
    # backend the next transaction may not be given. The failure is
    # intermittent and load-dependent ("prepared statement _asyncpg_stmt_N
    # does not exist"), which is exactly the kind that reaches production.
    #
    # False for the deployment in render.yaml, which uses a Render Postgres —
    # a direct connection with nothing in front of it. Set this only when
    # DATABASE_URL is repointed at a pooled provider.
    #
    # If you do: the pooler must be in SESSION mode, never TRANSACTION mode.
    # src/orchestrator._get_ledger takes a SELECT ... FOR UPDATE row lock to
    # close the contact-limit TOCTOU, and a row lock is only meaningful while
    # one transaction holds one backend — transaction pooling hands the next
    # statement a different one, and the damage is silent. On Supabase that is
    # port 5432, not 6543. DATABASE_URL_SYNC is best pointed at the direct
    # connection so a once-per-boot migration does not spend a metered pooler
    # connection, but the session pooler works for DDL too — see the appendix
    # in docs/DEPLOY.md for when that distinction matters.
    db_behind_pooler: bool = False

    # SQLAlchemy statement logging. Off by default: echo logs bound parameters,
    # and those include customer_email / customer_contact / vpa on every
    # payment_failures insert. Never tie this to an env that defaults to on.
    sql_echo: bool = False

    # ── API access ───────────────────────────────────────────────────────
    # Guards the non-webhook surface (see src/auth.py). Empty means DENY, not
    # allow — same reasoning as the Razorpay secrets above. The webhook route is
    # deliberately NOT covered by this: Razorpay cannot be told to send a custom
    # header, so that endpoint authenticates by HMAC over the raw body instead.
    api_key: SecretStr = SecretStr("")
    # Keys the case_events hash chain (src/audit_chain.py) so that someone
    # with database write access cannot silently re-stamp a rewritten chain:
    # the algorithm is in the repo, but this key is not in the database.
    # Empty means stamp/verify refuse rather than produce a forgeable chain.
    # Rotating it invalidates every existing stamp — stamp the chain fresh
    # after rotating (existing rows stay readable, their hashes just stop
    # verifying until re-stamped).
    audit_chain_secret: SecretStr = SecretStr("")
    # HMAC key for the voice webhook (POST /voice/turn). Telephony
    # providers sign their callbacks the way Razorpay signs webhooks; empty
    # means the endpoint refuses every request — closed until configured,
    # same fail-closed rule as every other signed surface.
    voice_webhook_secret: SecretStr = SecretStr("")
    # Opt in to the LLM answer path for voice turns. Off = the extractive
    # path only: replies assembled verbatim from retrieved passages and case
    # facts, verified by gate 4. On = an LLM rephrases the same passages
    # (temperature-disciplined, JSON-constrained) and the numeric grounding
    # gate still applies — a number absent from the passages is a refusal.
    voice_llm_enabled: bool = False
    # Opt in to queueing voice follow-up calls after a successful nudge (see
    # orchestrator._queue_voice_call). Default off: a call is the highest-
    # friction touch the engine can make and carries its own compliance
    # posture (DoT/TCPB registration, AI disclosure at call start).
    voice_chaser_enabled: bool = False
    # Sarvam AI (STT saaras:v3 auto-detect + TTS bulbul:v2) — the voice
    # demo and any provider leg that wants the engine to handle audio.
    # Empty = the demo page shows the error; the webhook's text path keeps
    # working without it.
    sarvam_api_key: SecretStr = SecretStr("")
    # ── Plivo call leg (src/voice/plivo_bridge.py) ──────────────────────
    # The bridge claims queued voice calls, dials them through Plivo's REST
    # API, and serves the XML Plivo fetches during the call. All three empty
    # = the bridge refuses to run (fail-closed, same rule as every signed
    # surface). PLIVO_AUTH_ID/TOKEN come from the Plivo console; the caller
    # number is the bought/verified one in E.164.
    plivo_auth_id: str = ""
    plivo_auth_token: SecretStr = SecretStr("")
    plivo_caller_number: str = ""
    # Where Plivo fetches the bridge's XML and TTS audio — the PUBLIC https
    # URL this service is reachable on (same value PUBLIC_BASE_URL carries,
    # declared separately because the bridge may run on its own host).
    plivo_bridge_base_url: str = ""
    # Where the bridge reaches the ENGINE's signed endpoints (/voice/turn,
    # /voice/queue/*). Empty = same host as the bridge (the single-process
    # deployment); set it when the bridge runs beside a separate engine.
    plivo_engine_base_url: str = ""
    # How long the bridge polls the queue when it is empty before looping.
    plivo_bridge_poll_seconds: int = 10
    # Gates the Streamlit dashboard. Empty means the dashboard refuses to render.
    # The dashboard itself reads DASHBOARD_PASSWORD straight from the
    # environment (dashboard/auth.py) — it is a separate process that holds no
    # import on this package. Declared here so run.sh's generated value has a
    # schema to validate against.
    dashboard_password: SecretStr = SecretStr("")

    # ── Application ──────────────────────────────────────────────────────
    app_env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # ── Demo mode ────────────────────────────────────────────────────────
    # Swaps the Razorpay SDK client for an in-process fake so the whole
    # engine — link minting, the pay redirect, the capture webhook,
    # attribution — runs on a laptop with no credentials and no network.
    # Off by default, and REFUSED outside development by the validator
    # below: a stub that can run in production is a worse bug than no stub,
    # because everything looks like it is working while no money moves.
    demo_mode: bool = False

    # ── Guardrail Thresholds ─────────────────────────────────────────────
    max_retries_per_payment: int = 3
    max_retries_per_customer_24h: int = 5
    # Named in PAISE and valued in paise. The old name said INR while the number
    # was paise, and keeping it as an alias was worse than either: policy.yaml
    # published `amount_ceiling_inr: 250000` next to it, so anyone who trusted
    # the published bound and set that env var got 250000 PAISE — a ₹2,500
    # ceiling, 100x tighter than the ₹2,50,000 they read. The alias is gone and
    # the old name is now REFUSED rather than reinterpreted: silently loosening
    # a legacy deployment 100x is the worse half of that guess. See
    # _reject_legacy_ceiling_name below.
    amount_ceiling_paise: int = Field(default=5_000_000)  # ₹50,000

    # A sanity floor on the ceiling, because the unit is the thing people get
    # wrong. Below ₹1,000 the value is far likelier to be rupees typed into a
    # paise field than a real policy, and nothing downstream can tell the two
    # apart — a ₹500 ceiling just quietly refuses every retry the engine would
    # ever make and looks like a working deployment doing nothing. A merchant
    # who genuinely wants to automate nothing sets MAX_RETRIES_PER_PAYMENT=0.
    @field_validator("amount_ceiling_paise", mode="after")
    @classmethod
    def _ceiling_is_paise_not_rupees(cls, paise: int) -> int:
        if 0 < paise < 100_000:
            raise ValueError(
                f"AMOUNT_CEILING_PAISE is {paise} paise (₹{paise / 100:,.2f}) — "
                f"below the ₹1,000 sanity floor. The unit is PAISE: ₹50,000 is "
                f"5000000. To automate nothing, set MAX_RETRIES_PER_PAYMENT=0."
            )
        return paise
    consent_window_hours: int = 72
    max_nudges_per_customer_24h: int = 2
    # The window the two rate limits above actually count over. The columns are
    # named _24h but a counter that only ever increments is a lifetime tally —
    # this setting is what the rolling reset reads, so "24h" in the name and
    # "24h" in behaviour cannot drift apart.
    rate_limit_window_hours: int = 24
    retry_blackout_start_hour: int = 23  # 11 PM IST
    retry_blackout_end_hour: int = 7  # 7 AM IST
    # Minimum quiet period after a customer-facing contact, multiplied by the
    # escalation level: 24h before the second message, 48h before the third.
    # Widening rather than flat, because nobody complains about the first nudge
    # — they complain about the fourth arriving as fast as the first.
    escalation_backoff_hours: int = 24

    # ── Promises to pay ──────────────────────────────────────────────────
    # A promise breaks on the CLOCK, never on suspicion — but a payment
    # initiated on the due date can post a day late (bank settlement, UPI
    # pending states), and breaking a promise the customer actually kept is
    # the one lie this ledger must never tell. The break condition is
    # therefore due_at + grace, and a capture inside grace keeps the promise
    # with kept_late_days recording how late. 24h covers Indian posting
    # delays without turning "by Friday" into "by Sunday".
    promise_grace_hours: int = 24
    # Ceiling on promises per case. A promise buys total silence until its
    # date, so an unlimited right to re-promise is an unlimited free
    # deferral loop — a serial promise-breaker can park a case forever with
    # words. Two broken promises and the third ask is refused by
    # record_promise (audited as promise_refused); the case stays on its
    # normal chase ladder, which is the honest path for someone whose words
    # stopped predicting money.
    max_promises_per_case: int = 3
    # How far before due_at the one reminder fires. 48h is the dunning
    # research default (memory decay is the top break cause, and a reminder
    # the day before arrives when it can still be acted on). The reminder
    # spends one real contact slot through the normal chase pipeline — a
    # promise buys silence for the CHASE, never a free lane to nag from.
    promise_reminder_lead_hours: int = 48
    # The farthest-out promise date a capture surface may accept. Kept rate
    # decays with horizon length (it holds up for ~3-5 days and falls off
    # after), so "I'll pay in six weeks" is recorded as noise about intent,
    # not as a promise that silences the case for six weeks.
    promise_max_horizon_days: int = 14

    # ── Promise-backed UPI Autopay mandate ───────────────────────────────
    # Off by default, and that is not caution for its own sake: debiting a
    # mandate needs Razorpay's Recurring Payments explicitly enabled on the
    # account, and an engine that offers a customer an autopay it cannot
    # actually charge has made a promise of its own that it will break. On =
    # the recovery page offers to authorise a mandate alongside the plain
    # date promise; off = every promise is the trust-based one, exactly as
    # before.
    promise_mandate_enabled: bool = False
    # The ceiling on a SINGLE unattended debit. RBI's e-mandate framework
    # exempts debits below a threshold from per-transaction additional
    # authentication; above it each debit needs the customer present, which is
    # not something a sweep can arrange at 9 AM on the promised date.
    #
    # ₹15,000 is the general limit and therefore the default here. The raised
    # ceilings apply to specific categories — card bills, insurance premiums,
    # mutual fund subscriptions — and NOT to general merchant collection, which
    # is what this engine does. A merchant operating in one of those categories
    # can raise this; nobody else should, and raising it does not change what
    # the regulator allows, only what this engine will attempt.
    #
    # Above the ceiling the mandate is never OFFERED, rather than offered and
    # then refused: the fallback trust-based promise is the only lawful path
    # for a larger amount, so the page must not imply otherwise.
    mandate_max_auto_debit_paise: int = 1_500_000  # ₹15,000

    # Expected-value stopping rule: attempt only while
    # confidence * amount > retry_cost + annoyance_cost. Only enforced when
    # the agent supplies a confidence — an agent that gives no estimate is
    # not asserting a false one, so the rule stays silent rather than
    # inventing a number to reject on. See guardrail/rules.py:check_expected_value.
    retry_attempt_cost_paise: int = 200  # ₹2 — matches the eval's default retry cost
    retry_annoyance_cost_paise: int = 0  # off by default; a real cost is a product call, not ours

    # RBI e-mandate pre-debit notifications are valid between the 24h minimum
    # notice and this ceiling. The framework's notice is per-debit: a
    # notification from weeks ago says nothing about a charge presented today,
    # so a stale one must be re-sent (nudge_customer) before retry_now is
    # allowed again. 168h = 7 days, conservative by default.
    mandate_predebit_notification_valid_hours: int = 168

    # ── Customer recovery page ───────────────────────────────────────────
    # Trust X-Forwarded-For when identifying the client for the recovery
    # page's per-IP rate limits. Set ONLY behind a reverse proxy you control
    # (Render's LB, nginx, an ALB): the code reads the RIGHTMOST entry, the
    # one your egress proxy added, which is the only hop a client cannot
    # forge. The leftmost entry is whatever the client sent, so trusting it
    # lets an attacker rotate one header value per request and bypass every
    # limit on the one public unauthenticated surface. With no trusted proxy
    # (a direct docker deployment) the header is ignored entirely and the
    # socket peer is used — which is exactly right there.
    behind_trusted_proxy: bool = False
    # How many proxies YOU control sit in front of this app. The client IP is
    # read this many entries from the RIGHT of X-Forwarded-For, because each
    # trusted hop appends the peer it saw. One hop (a bare Render/ALB/nginx) is
    # the common case and the default. Get this too LOW behind a two-hop stack
    # (a CDN in front of the platform LB) and every visitor keys on the same
    # internal address — one bucket for the whole internet, so the limit stops
    # being a defence and becomes a denial of service. Too HIGH and you read an
    # entry the client wrote. Count the hops on the real deployment; a header
    # with fewer entries than this falls back to the socket peer rather than
    # trusting a forgeable one.
    trusted_proxy_hops: int = 1
    # The merchant's display name, shown as the page's trust anchor: an SMS
    # link asking for money with no visible merchant name reads as phishing,
    # and the UPI app studies put interface identity at the top of the trust
    # stack. Public information — a plain str, not a secret. Empty falls back
    # to a neutral phrase, but every real deployment should set it.
    merchant_name: str = ""
    # Optional support deep-link, digits only (country code + number, as in a
    # wa.me URL). A human escalation path is a top dunning best practice, and
    # WhatsApp is where Indian customers expect to reach a business. Empty
    # hides the button and the page falls back to "reply to our message".
    support_whatsapp: str = ""
    # The receivables writeback: where the engine POSTs HMAC-signed alerts
    # (promise made/broken, dispute opened, plan defaulting, external
    # payment, chase exhausted) so the merchant's ERP reacts without anyone
    # watching a console. Signed with the SAME RISK_WEBHOOK_SECRET the
    # merchant signs their pushes with — one identity, both directions.
    # Empty means the outbound leg is off; alerts stay queued and visible on
    # the console's alerts panel, never silently dropped.
    merchant_webhook_url: str = ""
    # Signs the /recover/<token> links handed to customers. A DEDICATED secret,
    # not the Razorpay webhook one: a leak of either must not be a leak of both,
    # and they authorise completely different things — one proves Razorpay's
    # identity, the other lets a stranger view somebody's failed payment.
    #
    # Empty means the page is OFF and every token is rejected. Fail closed, for
    # the same reason api_key and the Razorpay secrets do.
    recovery_link_secret: SecretStr = SecretStr("")
    # How long a /recover/<token> link stays alive, in hours. The URL is a
    # bearer credential — it ends up in SMS logs, browser history and backup
    # archives — so the default is one day, NOT the full consent window.
    # Nothing in the flow needs the longer life: every nudge mints a FRESH
    # link, so a shorter life only shrinks the window a leaked URL stays
    # useful. mint() caps any value at the consent window regardless: a link
    # must never outlive the engine's own authority to act on the case.
    recovery_link_ttl_hours: int = 24
    # Absolute origin the customer reaches us on, e.g. https://pay.acme.in —
    # needed because a link inside an SMS cannot be relative. Without it,
    # url_for() returns None and messaging falls back to the raw payment link.
    public_base_url: str = ""

    # ── PII pseudonymisation ─────────────────────────────────────────────
    # Keys the HMAC that turns customer_id (a raw email or phone number) into
    # the pseudonym the LLM prompts carry. A DEDICATED secret, for the same
    # reason recovery_link_secret is: the webhook secret is shared with the
    # Razorpay dashboard and proves RAZORPAY'S identity — reusing it for
    # pseudonymisation meant one leak unmasked customers too. Empty falls back
    # to the webhook secret so existing deployments keep stable pseudonyms
    # until they set this explicitly; new deployments should always set it.
    pii_mask_secret: SecretStr = SecretStr("")

    # ── ML baseline ──────────────────────────────────────────────────────
    # Where the trained model lives. The README calls this policy the "XGBoost
    # baseline"; without a file here it silently runs the rule-based heuristic
    # instead, and the comparison the README makes is then between the LLM and
    # a pile of if-statements. Train it with scripts/train_xgboost.py.
    xgboost_model_path: str = "models/xgboost_baseline.joblib"
    # SHA-256 of the trained model file. joblib.load is pickle — it executes
    # whatever the file says, so a swapped-in model is arbitrary code
    # execution. Empty means no pin is enforced (the file ships inside the
    # image and the path is operator-set); the moment a model can arrive
    # from outside the build, set this — xgboost_baseline.py refuses to
    # load any file whose digest does not match. scripts/train_xgboost.py
    # prints it after every training run.
    xgboost_model_sha256: str = ""

    # ── Scheduler ────────────────────────────────────────────────────────
    # The worker that fires deferred `retry_at` attempts, reconciles webhook
    # events whose background task never ran, and expires promises to pay.
    # Off means those three things silently never happen — which is exactly the
    # state this codebase was in before src/scheduler.py existed.
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 60
    # Rows per sweep per tick. A cap so one backlog cannot hold the loop for
    # minutes; the next tick picks up where this one stopped.
    scheduler_batch_size: int = 50
    # How stale a `processed=False` webhook event must be before the reconciler
    # treats it as dropped. Must exceed the time a legitimate background task
    # takes, or the sweep races the task still doing the work.
    event_reconcile_after_seconds: int = 300
    # How stale a result='pending' attempt must be before the scheduler resolves
    # it as failed. A pending row is the write-ahead intent log; the executor's
    # own timeout bounds how long a legitimate call can hold one open, and this
    # threshold only has to clear that with margin. Stale pendings occupy their
    # attempt slot either way (fail-closed); this sweep is what makes the ledger
    # say so instead of leaving them "in flight" forever.
    attempt_stale_after_seconds: int = 900

    @field_validator("database_url", mode="after")
    @classmethod
    def _ensure_async_driver(cls, url: str) -> str:
        """
        Force an async driver onto the URL the platform handed us.

        Render, Railway, Heroku and Fly all inject a plain
        `postgresql://user:pass@host/db` (older ones still emit the `postgres://`
        scheme SQLAlchemy removed outright). Both blow up here rather than
        anywhere useful: create_async_engine on a sync driver raises
        InvalidRequestError at import time, which surfaces as a container that
        exits before it logs anything about why.

        Rewriting is safe and total — this field is only ever passed to
        create_async_engine, so there is no caller that wants the sync form.
        """
        url = url.replace("postgres://", "postgresql://", 1)
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)

    @field_validator("database_url_sync", mode="after")
    @classmethod
    def _ensure_sync_driver(cls, url: str) -> str:
        """The mirror of the above: strip an async driver, normalise the scheme.

        Lets a deployment point both DATABASE_URL and DATABASE_URL_SYNC at the
        same platform-provided connection string and have each end up with the
        driver it needs — which is exactly what render.yaml does.
        """
        url = url.replace("postgres://", "postgresql://", 1)
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)

    @field_validator("public_base_url", mode="after")
    @classmethod
    def _ensure_scheme(cls, url: str) -> str:
        """
        Prepend https:// when the platform handed us a bare host.

        render.yaml sources this via `fromService: {property: host}`, which
        returns a hostname with no scheme (e.g. "recovery-api.onrender.com").
        url_for() does f"{base}/recover/{token}" with no scheme of its own, so
        without this every link sent to a customer would render as
        "recovery-api.onrender.com/recover/..." — not a URL an SMS app will
        reliably linkify, and not what the field's own docstring promises.
        """
        if url and not url.startswith(("http://", "https://")):
            return f"https://{url}"
        return url

    @model_validator(mode="after")
    def _reject_legacy_ceiling_name(self) -> Settings:
        """
        AMOUNT_CEILING_INR used to be an alias for the paise field. Refuse it.

        Reinterpreting it as rupees would multiply a legacy deployment's ceiling
        by 100 — the engine would start auto-retrying amounts a human was meant
        to approve. Continuing to read it as paise leaves the trap that made
        policy.yaml's published ₹2,50,000 mean ₹2,500. Neither is safe to guess,
        so the operator says which they meant.
        """
        if "AMOUNT_CEILING_INR" in os.environ:
            raise ValueError(
                "AMOUNT_CEILING_INR is no longer read — its name said rupees "
                "while its value was paise, and the two readings differ by 100x "
                "on the guardrail that decides what gets auto-retried. Set "
                "AMOUNT_CEILING_PAISE instead, in paise (₹50,000 = 5000000), "
                "and remove AMOUNT_CEILING_INR."
            )
        return self

    @model_validator(mode="after")
    def _demo_mode_is_development_only(self) -> Settings:
        """
        Refuse demo mode anywhere but development.

        Demo mode replaces the payment gateway with a fake that always
        succeeds. Every downstream signal — a minted link, a captured
        payment, a recovered case, money on the dashboard — looks exactly
        like the real thing. Reaching staging or production with this on
        would not fail loudly; it would report a healthy, recovering
        business while no money moved at all. That is the one failure this
        codebase's fail-closed discipline exists to prevent, so it is a
        startup error rather than a warning.
        """
        if self.demo_mode and self.app_env != "development":
            raise ValueError(
                f"DEMO_MODE is on with APP_ENV={self.app_env}. Demo mode fakes "
                "the payment gateway and is development-only — every recovery "
                "it reports would be fictional. Unset DEMO_MODE, or set "
                "APP_ENV=development."
            )
        return self

    def require_razorpay_credentials(self) -> None:
        """
        Fail fast if the credentials needed to serve webhooks are missing.

        Called at app startup. Without this the service boots happily with empty
        secrets and every webhook is rejected at signature check — a silent
        misconfiguration that looks like "Razorpay isn't sending anything".
        """
        missing = [
            name
            for name in (
                "razorpay_key_id",
                "razorpay_key_secret",
                "razorpay_webhook_secret",
            )
            if not reveal(getattr(self, name)).strip()
        ]
        if missing:
            raise RuntimeError(
                "Missing required Razorpay settings: "
                + ", ".join(n.upper() for n in missing)
                + ". Copy .env.example to .env and fill them in."
            )


    def require_production_integrity(self) -> None:
        """
        Fail fast on the things that are silently OFF rather than loudly broken.

        `require_razorpay_credentials` catches an engine that cannot talk to the
        gateway — obvious within a minute. This catches the opposite failure:
        a deployment that runs, serves, recovers money, and quietly is not doing
        one of the things it claims. Development is exempt; staging and
        production are not.

        AUDIT_CHAIN_SECRET is the one that hard-fails. The hash chain is keyed
        on purpose, so with no key `stamp_unhashed_events` refuses and every
        `case_events` row keeps a NULL `event_hash`. The console still renders,
        the money still moves, and the tamper-evident trail the product offers a
        compliance reviewer does not exist. That is exactly the class of quiet
        untruth demo mode is refused for, so it gets the same treatment.
        """
        if self.app_env == "development":
            return

        if not reveal(self.audit_chain_secret).strip():
            raise RuntimeError(
                f"AUDIT_CHAIN_SECRET is empty with APP_ENV={self.app_env}. The "
                "case_events hash chain is keyed, so without it every audit row "
                "stays unstamped and the trail proves nothing — while everything "
                "else keeps working, which is why this is a startup error rather "
                "than a warning. Set it once and keep it: rotating invalidates "
                "every existing stamp."
            )

        # Not fatal — the engine decides fine on the XGBoost baseline — but a
        # missing key means the LLM policy agent never runs, and the fallback
        # is silent by design. Say so once at boot instead.
        provider_key = (
            self.anthropic_api_key if self.llm_provider == "anthropic"
            else self.openai_api_key
        )
        if not reveal(provider_key).strip():
            logger.warning(
                "%s_API_KEY is empty — every decision will fall back to the "
                "XGBoost baseline. The engine works, but the LLM policy agent "
                "is not running.",
                self.llm_provider.upper(),
            )


def reveal(value: SecretStr | str) -> str:
    """
    Unwrap a setting that may or may not be a SecretStr.

    One helper rather than `.get_secret_value()` at each call site, because the
    call sites mix the two — `razorpay_key_id` is a plain str next to a
    `razorpay_key_secret` that is not, and a bare `.get_secret_value()` there is
    an AttributeError waiting for whoever changes a field's type later.
    """
    return value.get_secret_value() if isinstance(value, SecretStr) else value


@lru_cache
def get_settings() -> Settings:
    """Cached singleton — call this instead of constructing Settings directly."""
    return Settings()
