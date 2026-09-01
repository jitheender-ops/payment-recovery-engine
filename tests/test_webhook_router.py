"""
The webhook endpoint — the entry point for every rupee this system touches.

Four things have to hold, and each was under-covered:

  1. An unsigned or wrongly-signed body never reaches the pipeline.
  2. The event is COMMITTED before the 200, because a 200 tells Razorpay never
     to send it again — acknowledging what we have not durably stored loses the
     payment silently.
  3. A re-delivery does not re-enter the pipeline.
  4. payment.captured resolves attribution through the payment LINK, not through
     the captured payment id, which we have never seen.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database import get_session
from src.ingestion.router import router as webhook_router
from src.models import ProcessedEvent, WebhookEvent

SECRET = "test_secret_key_12345"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> Any:
    """
    The webhook router alone, over the test database.

    Not the real app: main.py's lifespan demands Razorpay credentials and runs
    create_all against Postgres. This mounts the one router under test so the
    endpoint's own behaviour is what is being measured.
    """
    monkeypatch.setattr(
        "src.ingestion.router.get_settings",
        # webhook_ip_allowlist empty = the allowlist is off, which is the
        # default and what every test in this file is about: these measure the
        # signature/idempotency/attribution behaviour, not the network filter.
        # tests/test_webhook_allowlist.py covers that on its own.
        lambda: type(
            "S", (), {"razorpay_webhook_secret": SECRET, "webhook_ip_allowlist": ""}
        )(),
    )
    # The background task would run the whole orchestrator against Razorpay.
    # Recorded instead, so this file tests the endpoint and not the pipeline.
    queued: list[tuple[str, str]] = []

    async def fake_background(event_id: str, event_type: str, payload: dict[str, Any]) -> None:
        queued.append((event_id, event_type))

    monkeypatch.setattr("src.ingestion.router._process_event_background", fake_background)

    app = FastAPI()
    app.include_router(webhook_router, prefix="/webhooks")

    async def override() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = override
    test_client = TestClient(app)
    test_client.queued = queued  # type: ignore[attr-defined]
    return test_client


def _payload(payment_id: str = "pay_router_001") -> dict[str, Any]:
    return {
        "entity": "event",
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 50000,
                    "currency": "INR",
                    "method": "card",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_reason": "insufficient_funds",
                    "created_at": int(time.time()),
                }
            }
        },
        "created_at": 1700000000,
    }


def _post(client: Any, payload: dict[str, Any], *, signature: str | None = "valid") -> Any:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "valid":
        headers["X-Razorpay-Signature"] = _sign(body)
    elif signature is not None:
        headers["X-Razorpay-Signature"] = signature
    return client.post("/webhooks/razorpay", content=body, headers=headers)


# ── Signature ────────────────────────────────────────────────────────────


def test_a_body_with_no_signature_is_rejected(client: Any) -> None:
    assert _post(client, _payload(), signature=None).status_code == 401
    assert client.queued == []


def test_a_wrong_signature_is_rejected(client: Any) -> None:
    assert _post(client, _payload(), signature="deadbeef").status_code == 401
    assert client.queued == []


def test_a_signature_over_different_bytes_is_rejected(client: Any) -> None:
    """Signing a re-serialised body is the classic way this check gets defeated."""
    other = json.dumps(_payload("pay_other")).encode()
    assert _post(client, _payload(), signature=_sign(other)).status_code == 401


def test_malformed_json_with_a_valid_signature_is_a_400_not_a_500(client: Any) -> None:
    body = b"{not json"
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": _sign(body)},
    )
    assert resp.status_code == 400


# ── Storage and acknowledgement ──────────────────────────────────────────


async def test_the_event_is_committed_before_the_200(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    A 200 tells Razorpay never to send this again. Reading on a fresh connection
    after the response proves the row survived the request, not just the session.
    """
    assert _post(client, _payload()).status_code == 200

    async with db_sessionmaker() as reader:
        rows = (await reader.execute(select(WebhookEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "payment.failed"
    assert rows[0].payload["payload"]["payment"]["entity"]["id"] == "pay_router_001"


def test_a_stored_failed_event_is_queued_for_processing(client: Any) -> None:
    _post(client, _payload())
    assert len(client.queued) == 1
    assert client.queued[0][1] == "payment.failed"


async def test_a_redelivery_is_acknowledged_without_reprocessing(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    payload = _payload()
    assert _post(client, payload).status_code == 200
    assert _post(client, payload).status_code == 200

    assert len(client.queued) == 1, "a re-delivery re-entered the pipeline"
    async with db_sessionmaker() as reader:
        events = (await reader.execute(select(WebhookEvent))).scalars().all()
        processed = (await reader.execute(select(ProcessedEvent))).scalars().all()
    assert len(events) == 1
    assert len(processed) == 1


async def test_an_unhandled_event_type_is_stored_but_not_queued(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    Storing without processing is deliberate: the event store is the replay log,
    so a type we do not handle today is still replayable when we do.
    """
    payload = _payload()
    payload["event"] = "payment.authorized"
    assert _post(client, payload).status_code == 200

    assert client.queued == []
    async with db_sessionmaker() as reader:
        rows = (await reader.execute(select(WebhookEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "payment.authorized"


def test_captured_events_are_queued_for_attribution(client: Any) -> None:
    payload = _payload("pay_captured_001")
    payload["event"] = "payment.captured"
    assert _post(client, payload).status_code == 200
    assert [t for _, t in client.queued] == ["payment.captured"]


async def test_distinct_payments_produce_distinct_events(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _post(client, _payload("pay_a"))
    _post(client, _payload("pay_b"))
    async with db_sessionmaker() as reader:
        rows = (await reader.execute(select(WebhookEvent))).scalars().all()
    assert len(rows) == 2
    assert len(client.queued) == 2
