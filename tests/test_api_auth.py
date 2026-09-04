"""
The non-webhook surface must not be an open door.

This service is meant to sit behind a public tunnel so Razorpay can reach the
webhook, which means everything else on that host is public too. What is
reachable there is covered here: the docs trio, which enumerates every route and
schema, and the API-key guard that replaces it outside development.

src.main is reloaded per case because the docs URLs are decided once, at
FastAPI() construction time. Asserting against a hand-built app instead would
test a copy that can silently drift from the real one.

TestClient is used WITHOUT its context manager on purpose: entering it runs
lifespan, which calls require_razorpay_credentials() and opens a database
connection. None of that is needed to answer "is this endpoint reachable".
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import src.main
from src.config import get_settings


@pytest.fixture(autouse=True)
def _restore_main() -> Iterator[None]:
    """Reloading a module is global; put the real one back for other tests."""
    yield
    get_settings.cache_clear()
    importlib.reload(src.main)


def _client(monkeypatch: Any, env: str, api_key: str = "") -> TestClient:
    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.setenv("API_KEY", api_key)
    get_settings.cache_clear()
    return TestClient(importlib.reload(src.main).app)


def test_docs_are_open_in_development(monkeypatch: Any) -> None:
    client = _client(monkeypatch, "development")

    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    # And /status advertises them, because they exist.
    assert client.get("/status").json()["docs"] == "/docs"


def test_docs_ui_is_gone_in_production(monkeypatch: Any) -> None:
    client = _client(monkeypatch, "production", api_key="k" * 32)

    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    # /status must not advertise a 404.
    assert "docs" not in client.get("/status").json()


def test_root_sends_a_human_to_the_product(monkeypatch: Any) -> None:
    client = _client(monkeypatch, "production", api_key="k" * 32)

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    # /foundation, the scroll-told product story — the console stays a
    # click deeper for the operator.
    assert resp.headers["location"] == "/foundation"


def test_schema_requires_the_key_in_production(monkeypatch: Any) -> None:
    key = "k" * 32
    client = _client(monkeypatch, "production", api_key=key)

    assert client.get("/openapi.json").status_code == 401
    assert client.get("/openapi.json", headers={"X-API-Key": "wrong"}).status_code == 401

    ok = client.get("/openapi.json", headers={"X-API-Key": key})
    assert ok.status_code == 200
    assert ok.json()["info"]["title"] == "Payment Failure Recovery Engine"


def test_unset_key_denies_rather_than_allows(monkeypatch: Any) -> None:
    """A guard that waves everyone through when unconfigured is not a guard."""
    client = _client(monkeypatch, "production", api_key="")

    assert client.get("/openapi.json").status_code == 401
    assert client.get("/openapi.json", headers={"X-API-Key": ""}).status_code == 401
    assert client.get("/openapi.json", headers={"X-API-Key": "anything"}).status_code == 401


def test_non_ascii_key_header_is_a_401_not_a_500(monkeypatch: Any) -> None:
    """
    compare_digest raises TypeError on a non-ASCII str, and the header is
    attacker-controlled. Sent as raw bytes because that is what reaches the
    server: ASGI headers are bytes, and Starlette hands them over latin-1
    decoded, so any high byte lands in the comparison as a non-ASCII str.
    """
    client = _client(monkeypatch, "production", api_key="k" * 32)

    resp = client.get("/openapi.json", headers={b"X-API-Key": "ké¥".encode()})
    assert resp.status_code == 401


def test_webhook_is_not_behind_the_api_key(monkeypatch: Any) -> None:
    """
    Razorpay cannot send a custom header, so gating this route would take the
    integration down. It authenticates by HMAC instead — the 401 here must come
    from the signature check, not from the key guard.
    """
    client = _client(monkeypatch, "production", api_key="k" * 32)

    resp = client.post("/webhooks/razorpay", content=b"{}")
    assert resp.status_code == 401
    assert resp.text == "Missing signature"


def test_health_does_not_leak_the_environment(monkeypatch: Any) -> None:
    """The env name told an anonymous caller whether the docs gate was open."""
    client = _client(monkeypatch, "production", api_key="k" * 32)

    body = client.get("/health").json()
    assert body == {"status": "healthy"}


# ── The voice demo is a paid, unsigned surface ──────────────────────────────


def test_voice_demo_is_open_in_development(monkeypatch: Any) -> None:
    client = _client(monkeypatch, "development")
    assert client.get("/voice/demo").status_code == 200


def test_voice_demo_is_gone_outside_development(monkeypatch: Any) -> None:
    """
    /voice/demo/stt takes an unauthenticated file upload and spends real Sarvam
    quota per request — /voice/turn is HMAC-checked, these three are not. The
    control used to be a comment saying they were "not linked from the console
    nav", which left the URLs live in production.
    """
    for env in ("staging", "production"):
        client = _client(monkeypatch, env, api_key="k" * 32)
        assert client.get("/voice/demo").status_code == 404, env
        assert client.post("/voice/demo/turn", json={"text": "hi"}).status_code == 404, env
        assert client.post(
            "/voice/demo/stt", files={"audio": ("a.webm", b"x", "audio/webm")}
        ).status_code == 404, env
