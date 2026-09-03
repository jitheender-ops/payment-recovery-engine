"""
Platform-provided database URLs must not need hand-editing.

Render, Railway, Heroku and Fly all inject a plain `postgresql://` connection
string, and older ones still emit the `postgres://` scheme SQLAlchemy removed.
Handed straight to create_async_engine, both raise at import time — so the
symptom is a container that exits before logging anything about why, which is
the worst possible place to discover a one-token difference.

Both fields are normalised so a deployment can point them at the SAME injected
string and have each end up with the driver it needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.config import Settings, get_settings

RAW = "user:pw@db.internal:5432/payment_recovery"


@pytest.mark.parametrize(
    "given",
    [
        f"postgres://{RAW}",            # Heroku's legacy scheme
        f"postgresql://{RAW}",          # what Render and Railway inject
        f"postgresql+asyncpg://{RAW}",  # already correct
    ],
)
def test_async_url_always_ends_up_on_asyncpg(given: str) -> None:
    settings = Settings(database_url=given)
    assert settings.database_url == f"postgresql+asyncpg://{RAW}"


@pytest.mark.parametrize(
    "given",
    [
        f"postgres://{RAW}",
        f"postgresql://{RAW}",
        f"postgresql+asyncpg://{RAW}",  # the same string the async field got
    ],
)
def test_sync_url_always_ends_up_without_an_async_driver(given: str) -> None:
    settings = Settings(database_url_sync=given)
    assert settings.database_url_sync == f"postgresql://{RAW}"


def test_one_injected_string_serves_both_fields() -> None:
    """The property render.yaml depends on: same value in, right driver out."""
    injected = f"postgresql://{RAW}"
    settings = Settings(database_url=injected, database_url_sync=injected)
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.database_url_sync.startswith("postgresql://")
    assert "asyncpg" not in settings.database_url_sync


def test_sqlite_is_left_alone() -> None:
    """The test harness uses aiosqlite; normalisation must not touch it."""
    settings = Settings(database_url="sqlite+aiosqlite:///./test.db")
    assert settings.database_url == "sqlite+aiosqlite:///./test.db"


def test_public_base_url_gets_a_scheme_when_render_hands_us_a_bare_host() -> None:
    """render.yaml sources this via fromService {property: host} — no scheme."""
    settings = Settings(public_base_url="recovery-api.onrender.com")
    assert settings.public_base_url == "https://recovery-api.onrender.com"


def test_public_base_url_with_a_scheme_is_left_alone() -> None:
    settings = Settings(public_base_url="https://pay.example.in")
    assert settings.public_base_url == "https://pay.example.in"


def test_public_base_url_empty_stays_empty() -> None:
    """Empty means unconfigured; url_for() depends on this to fail closed."""
    assert Settings(public_base_url="").public_base_url == ""


# ── The amount ceiling: one number, one unit ────────────────────────────────


def test_the_legacy_rupee_named_ceiling_is_refused(monkeypatch: Any) -> None:
    """
    AMOUNT_CEILING_INR was an alias for the PAISE field while policy.yaml
    published a rupee value under that name, so anyone trusting the published
    bound got a ceiling 100x too tight. Reinterpreting it as rupees instead
    would loosen legacy deployments 100x. Neither is safe to guess.
    """
    monkeypatch.setenv("AMOUNT_CEILING_INR", "250000")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="AMOUNT_CEILING_INR is no longer read"):
        Settings()
    get_settings.cache_clear()


def test_a_rupee_shaped_paise_ceiling_is_refused(monkeypatch: Any) -> None:
    """₹500 quietly refuses every retry and looks like a working deployment."""
    monkeypatch.delenv("AMOUNT_CEILING_INR", raising=False)
    monkeypatch.setenv("AMOUNT_CEILING_PAISE", "50000")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="sanity floor"):
        Settings()
    get_settings.cache_clear()


def test_policy_yaml_publishes_the_enforced_ceiling() -> None:
    """
    PRODUCT.md: "the engine states only what it enforces." policy.yaml is the
    human-readable copy of the bounds and drifted to 5x the enforced value.
    """
    import yaml

    monkeypatch_free = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "policy.yaml").read_text()
    )
    published = monkeypatch_free["global"]["amount_ceiling_paise"]
    assert published == Settings().amount_ceiling_paise
