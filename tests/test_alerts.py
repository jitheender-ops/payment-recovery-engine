"""
Merchant alert delivery (src/receivables/alerts.py) — the writeback half
of the B2B chaser.

raise_alert is exercised incidentally by every receivables test (any
transition queues an alert); what had NO tests anywhere is the drain:
deliver_pending_alerts. A regression there is the quietest failure in
the product — every promise_made / dispute_opened / plan_defaulted row
piles up delivered=False and the merchant's ERP simply stops hearing
about anything. These tests pin each leg of that behaviour:

  * the happy delivery marks the row delivered
  * a failure counts ONE attempt and keeps the row queued
  * the 3-attempt cap rests the alert instead of hammering forever
  * an unset RISK_WEBHOOK_SECRET or MERCHANT_WEBHOOK_URL fails closed
    (0 delivered, queue intact — the visible panel, never a silent drop)
  * the outbound body is HMAC-signed with the SAME secret /risks uses
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.receivables.alerts import deliver_pending_alerts, raise_alert
from src.receivables.models import MerchantAlert

SECRET = "alerts-test-secret"


class _Settings:
    """The two knobs deliver_pending_alerts reads, overridable per test."""

    def __init__(self, url: str, secret: str) -> None:
        self.merchant_webhook_url = url
        self.risk_webhook_secret = __import__("pydantic").SecretStr(secret)


async def _queued(
    db_sessionmaker: async_sessionmaker[AsyncSession], n: int = 1
) -> list[MerchantAlert]:
    async with db_sessionmaker() as session:
        rows = [
            await raise_alert(
                session,
                event_type="promise_made",
                case_ref=f"INV-{i}",
                detail={"amount": 100_000},
            )
            for i in range(n)
        ]
        await session.commit()
        ids = [r.id for r in rows]
    async with db_sessionmaker() as reader:
        listed = (await reader.execute(
            select(MerchantAlert).where(MerchantAlert.id.in_(ids))
        )).scalars().all()
    return list(listed)


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


# ── The happy path ────────────────────────────────────────────────────────


async def test_a_delivered_alert_is_marked_and_carries_a_valid_signature(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """The outbound POST is signed the same way POST /risks verifies
    inbound — one identity, one secret, both directions. The merchant's
    side can verify it with the same code."""
    seen: dict[str, Any] = {}

    async def fake_post(self: Any, url: str, content: bytes = b"",
                        headers: dict[str, str] | None = None, **kw: Any
                        ) -> Any:
        seen["url"] = url
        seen["body"] = content
        seen["headers"] = headers or {}
        return type("R", (), {"status_code": 200})()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(
        "src.receivables.alerts.get_settings",
        lambda: _Settings("https://merchant.example/hook", SECRET),
    )

    (alert,) = await _queued(db_sessionmaker)
    async with db_sessionmaker() as session:
        delivered = await deliver_pending_alerts(session)
    assert delivered == 1

    # The signature the merchant verifies.
    assert seen["headers"].get("X-Alert-Signature") == _sign(seen["body"])
    payload = json.loads(seen["body"])
    assert payload["event_type"] == "promise_made"
    assert payload["case_ref"] == alert.case_ref
    # PII-free by construction: refs and amounts only.
    assert set(payload) == {"event_type", "account_ref", "case_ref",
                            "detail", "occurred_at"}

    async with db_sessionmaker() as reader:
        fresh = await reader.get(MerchantAlert, alert.id)
        assert fresh is not None
        assert fresh.delivered is True
        assert fresh.delivered_at is not None


# ── Fail-soft delivery ─────────────────────────────────────────────────────


async def test_a_failed_delivery_counts_one_attempt_and_stays_queued(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    async def boom(self: Any, url: str, **kw: Any) -> Any:
        return type("R", (), {"status_code": 503})()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    monkeypatch.setattr(
        "src.receivables.alerts.get_settings",
        lambda: _Settings("https://merchant.example/hook", SECRET),
    )

    (alert,) = await _queued(db_sessionmaker)
    async with db_sessionmaker() as session:
        assert await deliver_pending_alerts(session) == 0

    async with db_sessionmaker() as reader:
        fresh = await reader.get(MerchantAlert, alert.id)
        assert fresh is not None
        assert fresh.delivered is False, "a 503 must not mark the row delivered"
        assert fresh.delivery_attempts == 1
        assert fresh.last_error is not None


async def test_the_cap_rests_an_alert_instead_of_hammering_forever(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """Three strikes: past the cap the row leaves the delivery sweep and
    rests visible on the console for a human — the shared re-arm
    discipline (EVENT_RECONCILE_MAX_ATTEMPTS) applies to our outbound
    copies too."""
    async def down(self: Any, url: str, **kw: Any) -> Any:
        return type("R", (), {"status_code": 503})()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", down)
    monkeypatch.setattr(
        "src.receivables.alerts.get_settings",
        lambda: _Settings("https://merchant.example/hook", SECRET),
    )

    (alert,) = await _queued(db_sessionmaker)
    for _ in range(5):  # sweep more times than the cap
        async with db_sessionmaker() as session:
            await deliver_pending_alerts(session)

    async with db_sessionmaker() as reader:
        fresh = await reader.get(MerchantAlert, alert.id)
        assert fresh is not None
        assert fresh.delivery_attempts == 3, (
            "the sweep kept hammering a dead endpoint past the cap"
        )
        assert fresh.delivered is False


async def test_recovery_on_a_later_sweep_still_delivers(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """Fail, fail, then the endpoint recovers: the third sweep delivers.
    The cap must not write the row off while it still has attempts."""
    calls = {"n": 0}

    async def flaky(self: Any, url: str, **kw: Any) -> Any:
        calls["n"] += 1
        code = 503 if calls["n"] < 3 else 200
        return type("R", (), {"status_code": code})()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", flaky)
    monkeypatch.setattr(
        "src.receivables.alerts.get_settings",
        lambda: _Settings("https://merchant.example/hook", SECRET),
    )

    (alert,) = await _queued(db_sessionmaker)
    async with db_sessionmaker() as session:
        await deliver_pending_alerts(session)  # attempt 1 — fail
    async with db_sessionmaker() as session:
        await deliver_pending_alerts(session)  # attempt 2 — fail
    async with db_sessionmaker() as session:
        delivered = await deliver_pending_alerts(session)  # attempt 3 — pass

    assert delivered == 1
    async with db_sessionmaker() as reader:
        fresh = await reader.get(MerchantAlert, alert.id)
        assert fresh is not None and fresh.delivered is True


# ── Fail-closed configuration ─────────────────────────────────────────────


async def test_an_unset_secret_fails_closed_and_keeps_the_queue(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """The queue still fills (raise_alert always appends); only the
    outbound leg is off. 0 delivered, nothing dropped, nothing marked."""
    posted: list[bytes] = []

    async def spy(self: Any, url: str, content: bytes = b"", **kw: Any) -> Any:
        posted.append(content)
        return type("R", (), {"status_code": 200})()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", spy)
    monkeypatch.setattr(
        "src.receivables.alerts.get_settings",
        lambda: _Settings("https://merchant.example/hook", ""),
    )

    (alert,) = await _queued(db_sessionmaker)
    async with db_sessionmaker() as session:
        assert await deliver_pending_alerts(session) == 0
    assert posted == [], "an unsigned alert was sent to the merchant"

    async with db_sessionmaker() as reader:
        fresh = await reader.get(MerchantAlert, alert.id)
        assert fresh is not None
        assert fresh.delivered is False
        assert fresh.delivery_attempts == 0, "the fail-closed path counted attempts"


async def test_no_webhook_url_configured_sends_nothing(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        "src.receivables.alerts.get_settings",
        lambda: _Settings("", SECRET),
    )
    (alert,) = await _queued(db_sessionmaker)
    async with db_sessionmaker() as session:
        assert await deliver_pending_alerts(session) == 0
    async with db_sessionmaker() as reader:
        fresh = await reader.get(MerchantAlert, alert.id)
        assert fresh is not None and fresh.delivered is False


# ── Ordering ───────────────────────────────────────────────────────────────


async def test_delivery_drains_oldest_first(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """A FIFO writeback queue: the alert the merchant has been waiting
    longest for goes first, and limit bounds each pass."""
    order: list[str] = []

    async def fake(self: Any, url: str, content: bytes = b"", **kw: Any) -> Any:
        order.append(json.loads(content)["case_ref"])
        return type("R", (), {"status_code": 200})()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake)
    monkeypatch.setattr(
        "src.receivables.alerts.get_settings",
        lambda: _Settings("https://merchant.example/hook", SECRET),
    )

    await _queued(db_sessionmaker, n=3)
    async with db_sessionmaker() as session:
        assert await deliver_pending_alerts(session, limit=2) == 2
    assert order == ["INV-0", "INV-1"], "not oldest-first or not limited"

    async with db_sessionmaker() as session:
        assert await deliver_pending_alerts(session) == 1
    assert order == ["INV-0", "INV-1", "INV-2"]
