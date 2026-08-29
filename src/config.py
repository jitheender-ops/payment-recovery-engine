"""
Application configuration via pydantic-settings.

All config is loaded from environment variables (or .env file).
Guardrail thresholds are configurable here so they can be tuned without code changes.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # ── Database ─────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://recovery:recovery@localhost:5432/payment_recovery"
    database_url_sync: str = "postgresql://recovery:recovery@localhost:5432/payment_recovery"

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
    # Gates the Streamlit dashboard. Empty means the dashboard refuses to render.
    # The dashboard itself reads DASHBOARD_PASSWORD straight from the
    # environment (dashboard/auth.py) — it is a separate process that holds no
    # import on this package. Declared here so run.sh's generated value has a
    # schema to validate against.
    dashboard_password: SecretStr = SecretStr("")

    # ── Application ──────────────────────────────────────────────────────
    app_env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # ── Guardrail Thresholds ─────────────────────────────────────────────
    max_retries_per_payment: int = 3
    max_retries_per_customer_24h: int = 5
    # Named in PAISE and valued in paise: the previous name said INR while the
    # number was paise — one operator-set env var away from a ₹500 ceiling.
    # The legacy AMOUNT_CEILING_INR env var still loads (AliasChoices) so
    # existing deployments keep working; new config should use the honest name.
    amount_ceiling_paise: int = Field(
        default=5_000_000,  # ₹50,000
        validation_alias=AliasChoices("amount_ceiling_paise", "amount_ceiling_inr"),
    )
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
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url[len("postgresql://"):]
        return url

    @field_validator("database_url_sync", mode="after")
    @classmethod
    def _ensure_sync_driver(cls, url: str) -> str:
        """The mirror of the above: strip an async driver, normalise the scheme.

        Lets a deployment point both DATABASE_URL and DATABASE_URL_SYNC at the
        same platform-provided connection string and have each end up with the
        driver it needs — which is exactly what render.yaml does.
        """
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
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
