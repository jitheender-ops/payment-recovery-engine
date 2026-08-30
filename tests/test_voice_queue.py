"""
The voice chaser's queue integration: opt-in at queue time, one row per
attempt, atomic claim, spoken opt-out closes cases through the same path
as every other channel.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import get_settings
from src.models import RetryAttempt, VoiceCallQueue
from src.voice.webhook import router as voice_router

SECRET = "voice-queue-test-secret"


@pytest.fixture
def client(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> Any:
    monkeypatch.setattr("src.voice.webhook.async_session_factory", db_sessionmaker)
    app = FastAPI()
    app.include_router(voice_router)
    return TestClient(app)


@pytest.fixture
def queue_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("VOICE_CHASER_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _signed(body: bytes) -> dict[str, str]:
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"x-voice-signature": sig, "content-type": "application/json"}


async def _make_attempt(
    session: AsyncSession, case_id: Any, *, n: int = 0
) -> RetryAttempt:
    attempt = RetryAttempt(
        idempotency_key=f"chase_voice_test_{case_id}_{n}",
        attempt_number=n,
        recovery_case_id=case_id,
        action_type="nudge_customer",
        guardrail_passed=True,
        result="success",
    )
    session.add(attempt)
    await session.flush()
    return attempt


# ── The orchestrator hook ──────────────────────────────────────────────────


async def test_no_queue_row_without_the_flag(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    The flag is the whole gate: with VOICE_CHASER_ENABLED unset, a
    successful nudge to a customer with a phone writes no queue row —
    a phone call must never appear because someone deployed new code.
    """
    from src.agent.actions import RetryAction
    from src.cases import open_case
    from src.orchestrator import PaymentRecoveryOrchestrator

    monkeypatch.delenv("VOICE_CHASER_ENABLED", raising=False)
    get_settings.cache_clear()

    async with db_sessionmaker() as s:
        case = await open_case(
            s, risk_type="checkout_abandonment", subject_ref="cart_vq1",
            customer_id="cust_vq1", amount_at_risk=100_000,
        )
        attempt = await _make_attempt(s, case.id)
        await s.commit()

        orch = PaymentRecoveryOrchestrator.__new__(PaymentRecoveryOrchestrator)
        await orch._queue_voice_call(
            s, case=case, attempt=attempt,
            customer_contact="+919812345678",
            action=RetryAction(action="nudge_customer", reason="test nudge"),
        )
        await s.commit()
        rows = (await s.execute(sa.select(VoiceCallQueue))).scalars().all()
    assert rows == []


async def test_a_successful_nudge_queues_one_call(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    client: Any,
    queue_env: None,
) -> None:
    from src.agent.actions import RetryAction
    from src.cases import open_case
    from src.orchestrator import PaymentRecoveryOrchestrator

    async with db_sessionmaker() as s:
        case = await open_case(
            s, risk_type="checkout_abandonment", subject_ref="cart_vq2",
            customer_id="cust_vq2", amount_at_risk=100_000,
        )
        attempt = await _make_attempt(s, case.id)
        await s.commit()

        # The hook is a pure session operation — call it as the orchestrator
        # does, through a bare instance shell (no __init__: it builds real
        # Razorpay/LLM clients the test does not want).
        orch = PaymentRecoveryOrchestrator.__new__(PaymentRecoveryOrchestrator)
        await orch._queue_voice_call(
            s, case=case, attempt=attempt,
            customer_contact="+919812345678",
            action=RetryAction(action="nudge_customer", reason="test nudge"),
        )
        await s.commit()

        rows = (await s.execute(sa.select(VoiceCallQueue))).scalars().all()
        assert len(rows) == 1
        assert rows[0].state == "queued"
        assert rows[0].customer_contact == "+919812345678"
        assert rows[0].amount_paise == 100_000

        # Idempotent: queueing again for the same attempt adds nothing.
        await orch._queue_voice_call(
            s, case=case, attempt=attempt,
            customer_contact="+919812345678",
            action=RetryAction(action="nudge_customer", reason="test nudge"),
        )
        await s.commit()
        rows = (await s.execute(sa.select(VoiceCallQueue))).scalars().all()
        assert len(rows) == 1

        # A non-nudge action never queues; a failed attempt never queues.
        await orch._queue_voice_call(
            s, case=case, attempt=attempt,
            customer_contact="+919812345678",
            action=RetryAction(action="abandon", reason="test nudge"),
        )
        await s.commit()
        rows = (await s.execute(sa.select(VoiceCallQueue))).scalars().all()
        assert len(rows) == 1


async def test_a_nudge_without_a_phone_number_queues_nothing(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    queue_env: None,
) -> None:
    from src.agent.actions import RetryAction
    from src.cases import open_case
    from src.orchestrator import PaymentRecoveryOrchestrator

    async with db_sessionmaker() as s:
        case = await open_case(
            s, risk_type="checkout_abandonment", subject_ref="cart_vq3",
            customer_id="cust_vq3", amount_at_risk=100_000,
        )
        attempt = await _make_attempt(s, case.id)
        await s.commit()

        orch = PaymentRecoveryOrchestrator.__new__(PaymentRecoveryOrchestrator)
        await orch._queue_voice_call(
            s, case=case, attempt=attempt,
            customer_contact=None,
            action=RetryAction(action="nudge_customer", reason="test nudge"),
        )
        await s.commit()
        rows = (await s.execute(sa.select(VoiceCallQueue))).scalars().all()
        assert rows == []


# ── The queue endpoints ────────────────────────────────────────────────────


async def test_claim_requires_a_signature(client: Any) -> None:
    r = client.post("/voice/queue/claim", json={"worker": "w1"})
    assert r.status_code == 401


async def test_claim_returns_the_oldest_queued_call(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession], queue_env: None
) -> None:
    from src.cases import open_case

    async with db_sessionmaker() as s:
        case = await open_case(
            s, risk_type="checkout_abandonment", subject_ref="cart_vq4",
            customer_id="cust_vq4", amount_at_risk=250_000,
        )
        await s.commit()
        s.add(
            VoiceCallQueue(
                recovery_case_id=case.id,
                retry_attempt_id=_make_attempt_id(),
                customer_contact="+919812345678",
                risk_type=case.risk_type,
                amount_paise=250_000,
            )
        )
        await s.commit()

    body = b'{"worker": "bridge-1"}'
    r = client.post("/voice/queue/claim", content=body, headers=_signed(body))
    assert r.status_code == 200
    call = r.json()["call"]
    assert call is not None
    assert call["phone"] == "+919812345678"
    assert call["amount"] == "₹2,500"
    assert call["turn_endpoint"] == "/voice/turn"

    # Claimed once — the second claim finds nothing.
    r2 = client.post("/voice/queue/claim", content=body, headers=_signed(body))
    assert r2.status_code == 200
    assert r2.json()["call"] is None


def _make_attempt_id() -> Any:
    import uuid

    return uuid.uuid4()


async def test_report_marks_done_and_opted_out_closes_cases(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession], queue_env: None
) -> None:
    import uuid as _uuid

    from src.cases import open_case

    async with db_sessionmaker() as s:
        case = await open_case(
            s, risk_type="checkout_abandonment", subject_ref="cart_vq5",
            customer_id="cust_vq5", amount_at_risk=100_000,
        )
        await s.commit()
        call_id = _uuid.uuid4()
        s.add(
            VoiceCallQueue(
                id=call_id,
                recovery_case_id=case.id,
                retry_attempt_id=_uuid.uuid4(),
                customer_contact="+919812345678",
                risk_type=case.risk_type,
                amount_paise=100_000,
            )
        )
        await s.commit()

    # Plain completion.
    body = f'{{"call_id": "{call_id}", "result": "done"}}'.encode()
    r = client.post("/voice/queue/report", content=body, headers=_signed(body))
    assert r.status_code == 200
    assert r.json()["state"] == "done"

    # Opt-out: a second queue row whose report closes the customer's cases.
    async with db_sessionmaker() as s:
        call_id2 = _uuid.uuid4()
        s.add(
            VoiceCallQueue(
                id=call_id2,
                recovery_case_id=case.id,
                retry_attempt_id=_uuid.uuid4(),
                customer_contact="+919812345678",
                risk_type=case.risk_type,
                amount_paise=100_000,
            )
        )
        await s.commit()

    body2 = f'{{"call_id": "{call_id2}", "result": "done", "opted_out": true}}'.encode()
    r2 = client.post("/voice/queue/report", content=body2, headers=_signed(body2))
    assert r2.status_code == 200
    assert r2.json()["state"] == "opted_out"

    async with db_sessionmaker() as s:
        row = await s.get(VoiceCallQueue, call_id2)
        assert row is not None and row.state == "opted_out"
        fresh = await s.get(type(case), case.id)
        assert fresh is not None and fresh.state == "opted_out"
