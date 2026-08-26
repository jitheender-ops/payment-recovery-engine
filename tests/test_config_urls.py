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

import pytest

from src.config import Settings

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
