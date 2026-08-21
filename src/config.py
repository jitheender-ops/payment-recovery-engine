"""
Application configuration via pydantic-settings.

All config is loaded from environment variables (or .env file).
Guardrail thresholds are configurable here so they can be tuned without code changes.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

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
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    # Per-request timeout for the Razorpay SDK. requests defaults to no timeout
    # at all, so without this a single hung connection blocks a worker forever.
    # Payment-link creation is a fast call; 10s is generous, not tight.
    razorpay_timeout_seconds: float = 10.0

    # ── LLM ──────────────────────────────────────────────────────────────
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
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
    api_key: str = ""
    # Gates the Streamlit dashboard. Empty means the dashboard refuses to render.
    dashboard_password: str = ""

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
    retry_blackout_start_hour: int = 23  # 11 PM IST
    retry_blackout_end_hour: int = 7  # 7 AM IST

    # ── Dashboard ────────────────────────────────────────────────────────
    streamlit_port: int = 8501

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
            if not getattr(self, name).strip()
        ]
        if missing:
            raise RuntimeError(
                "Missing required Razorpay settings: "
                + ", ".join(n.upper() for n in missing)
                + ". Copy .env.example to .env and fill them in."
            )


@lru_cache
def get_settings() -> Settings:
    """Cached singleton — call this instead of constructing Settings directly."""
    return Settings()
