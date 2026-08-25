"""
Application configuration via pydantic-settings.

All config is loaded from environment variables (or .env file).
Guardrail thresholds are configurable here so they can be tuned without code changes.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
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
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # ── Guardrail Thresholds ─────────────────────────────────────────────
    max_retries_per_payment: int = 3
    max_retries_per_customer_24h: int = 5
    amount_ceiling_inr: int = 5_000_000  # ₹50,000 in paise
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

    # ── Dashboard ────────────────────────────────────────────────────────
    streamlit_port: int = 8501

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
