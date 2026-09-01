"""
The optional webhook IP allowlist.

Razorpay's security guidance recommends allowlisting their webhook source
IPs as defence in depth. This is that, in the app, for a deployment with no
firewall in front of it.

Two properties matter more than the matching itself:

  1. **Unset means off.** Every other guard in this codebase fails closed,
     and this one deliberately does not. Those guard authentication, where
     an unconfigured guard must refuse everyone. This one only narrows what
     HMAC already guards, and a default-closed allowlist would reject every
     real webhook the moment someone upgraded without setting it — an
     outage, not a security posture.

  2. **HMAC stays the authenticator.** Being on the allowlist buys nothing
     without a valid signature. An IP is not an identity.

The proxy tests exist because getting that wrong is how an allowlist becomes
either an outage or a bypass: trust X-Forwarded-For when you shouldn't and
anyone can spoof their way in; ignore it when you should trust it and every
real webhook is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.auth import ip_allowed
from src.config import get_settings
from src.database import get_session
from src.ingestion.router import router as webhook_router

SECRET = "test_secret_key_12345"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _payload() -> dict[str, Any]:
    return {
        "entity": "event",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": {
            "id": "pay_allowlist_1", "entity": "payment", "amount": 10000,
            "currency": "INR", "status": "failed", "method": "card",
            "error_code": "BAD_REQUEST_ERROR", "error_reason": "card_declined",
            "error_source": "bank", "error_step": "payment_authorization",
            "created_at": 1,
        }}},
        "created_at": 1,
    }


def _client(
    sm: async_sessionmaker[AsyncSession], monkeypatch: Any, allowlist: str
) -> TestClient:
    monkeypatch.setattr(
        "src.ingestion.router.get_settings",
        lambda: type("S", (), {
            "razorpay_webhook_secret": SECRET,
            "webhook_ip_allowlist": allowlist,
        })(),
    )
    app = FastAPI()
    app.include_router(webhook_router, prefix="/webhooks")

    async def override() -> Any:
        async with sm() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = override
    return TestClient(app)


def _post(client: TestClient, signed: bool = True) -> Any:
    body = json.dumps(_payload()).encode()
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-Razorpay-Signature"] = _sign(body)
    return client.post("/webhooks/razorpay", content=body, headers=headers)


# ── The default: nothing changes for anyone who has not opted in ─────────


def test_an_unset_allowlist_lets_every_signed_webhook_through(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    assert _post(_client(db_sessionmaker, monkeypatch, "")).status_code == 200


def test_whitespace_is_still_unset(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """A blank env var must read as off, not as an empty deny-list."""
    assert _post(_client(db_sessionmaker, monkeypatch, "   ")).status_code == 200


# ── On: allowed and blocked ──────────────────────────────────────────────


def test_even_an_open_allowlist_refuses_an_unevaluable_peer(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    TestClient presents as the literal string 'testclient', not an address —
    which is the useful case here. Once the allowlist is ON, a peer that
    cannot be parsed is refused even against 0.0.0.0/0: an address we cannot
    evaluate is not one we can vouch for. The CIDR matching itself is
    exercised against real addresses in the ip_allowed() tests below.
    """
    client = _client(db_sessionmaker, monkeypatch, "0.0.0.0/0")
    assert _post(client).status_code == 403


def test_a_blocked_address_never_reaches_the_signature_check(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    client = _client(db_sessionmaker, monkeypatch, "203.0.113.0/24")
    assert _post(client).status_code == 403


def test_the_allowlist_is_not_a_substitute_for_a_signature(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """The property that keeps this defence in depth rather than an
    alternative: an IP is not an identity."""
    client = _client(db_sessionmaker, monkeypatch, "")
    assert _post(client, signed=False).status_code == 401


# ── Matching, and the proxy question ─────────────────────────────────────


@pytest.fixture
def _direct(monkeypatch: Any) -> Any:
    """No trusted proxy: the socket peer is the truth and XFF is ignored."""
    monkeypatch.setenv("BEHIND_TRUSTED_PROXY", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _request(peer: str, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({
        "type": "http", "headers": headers, "client": (peer, 12345),
        "method": "POST", "path": "/webhooks/razorpay", "scheme": "http",
        "query_string": b"", "server": ("test", 80),
    })


def test_a_bare_address_and_a_cidr_both_match(_direct: Any) -> None:
    assert ip_allowed(_request("203.0.113.7"), "203.0.113.7")
    assert ip_allowed(_request("203.0.113.7"), "203.0.113.0/24")
    assert not ip_allowed(_request("198.51.100.9"), "203.0.113.0/24")


def test_a_list_matches_on_any_entry(_direct: Any) -> None:
    allow = "198.51.100.1, 203.0.113.0/24 ,2001:db8::/32"
    assert ip_allowed(_request("203.0.113.99"), allow)
    assert ip_allowed(_request("198.51.100.1"), allow)
    assert ip_allowed(_request("2001:db8::1"), allow)
    assert not ip_allowed(_request("192.0.2.1"), allow)


def test_a_malformed_entry_is_skipped_rather_than_raising(_direct: Any) -> None:
    """A typo in one CIDR must not turn every incoming payment event into a
    500 — the rest of the list still has to work."""
    assert ip_allowed(_request("203.0.113.7"), "not-an-ip, 203.0.113.0/24")
    assert not ip_allowed(_request("192.0.2.1"), "not-an-ip")


def test_an_unevaluable_peer_is_refused_while_the_allowlist_is_on(
    _direct: Any,
) -> None:
    assert not ip_allowed(_request("unknown"), "203.0.113.0/24")
    # ...but is fine when the allowlist is off, since off means off.
    assert ip_allowed(_request("unknown"), "")


def test_a_spoofed_forwarded_header_cannot_talk_its_way_in(_direct: Any) -> None:
    """
    With no trusted proxy, X-Forwarded-For is attacker-controlled and
    client_ip ignores it. If the allowlist honoured it anyway, the allowlist
    would be worse than useless — it would be a header anyone can send.
    """
    req = _request("192.0.2.1", xff="203.0.113.7")
    assert not ip_allowed(req, "203.0.113.0/24")


def test_behind_a_trusted_proxy_the_forwarded_address_is_used(
    monkeypatch: Any,
) -> None:
    """The other half: refusing to read XFF where it IS trustworthy would
    reject every real webhook, because the peer is then the proxy."""
    monkeypatch.setenv("BEHIND_TRUSTED_PROXY", "true")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    get_settings.cache_clear()
    try:
        req = _request("10.0.0.5", xff="203.0.113.7")
        assert ip_allowed(req, "203.0.113.0/24")
        assert not ip_allowed(req, "198.51.100.0/24")
    finally:
        get_settings.cache_clear()
