"""
The chasers — revenue recovery for the risk types with no inbound webhook.

A card decline announces itself; an abandoned cart, a halted subscription, an
overdue invoice and a failed mandate debit only exist in the merchant's own
systems, so the merchant POSTs them to /risks and the scheduler chases the
cases that open. What gets tested here:

  1. /risks authenticates by HMAC and fails closed, dedups re-deliveries, and
     refuses to accept payment_failure (that rail belongs to the gateway).
  2. process_risk_event opens the case with the per-type policy's budget and
     first-touch time — immediate for subscriptions/invoices, deferred for
     carts and mandates.
  3. chase_case runs the same discipline as the payment rail: the attempt row
     is committed BEFORE Razorpay is called, the ladder advances after every
     contact, the budget closes the case, and a pending attempt blocks the
     next chase.
  4. The scheduler's chase sweep finds due cases of the four types and never
     touches the webhook-driven payment rail.
  5. The recovery page renders risk cases honestly (no "payment failed" for a
     cart that never paid) and self-serve pay mints a case-driven link.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import scheduler
from src.agent.actions import ActionType, FailureContext, RetryAction
from src.chasers.policy import RISK_POLICIES, policy_for
from src.database import get_session
from src.guardrail.gate import GuardrailResult
from src.models import RecoveryCase, RetryAttempt, RetryLedger, RiskEvent
from src.orchestrator import PaymentRecoveryOrchestrator

SECRET = "risk-webhook-test-secret"


def _aware(ts: datetime) -> datetime:
    """SQLite hands back naive wall clocks; the assertions compare against
    aware now()."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


class _FixedAgent:
    """Stands in for PolicyAgent so one code path is exercised deterministically."""

    def __init__(self, action: ActionType = "retry_now", retry_at: datetime | None = None) -> None:
        self.fallback_count = 0
        self._action = action
        self._retry_at = retry_at

    async def decide(self, context: FailureContext) -> RetryAction:
        return RetryAction(
            action=self._action,
            retry_at=self._retry_at,
            reason="fixed test decision",
            confidence=0.9,
        )


def _orchestrator(monkeypatch: Any, passed: bool = True, action: ActionType = "retry_now") -> Any:
    """Orchestrator with the agent and guardrail pinned; executor left to caller."""
    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(orch, "_get_agent", lambda: _FixedAgent(action))
    # Pinned because the real gate consults the wall clock (IST retry blackout),
    # which would make tests pass or fail depending on the hour they run.
    monkeypatch.setattr(
        orch._guardrail,
        "validate",
        lambda *a, **k: GuardrailResult(
            passed=passed,
            rejection_reasons=[] if passed else ["pinned rejection"],
            rules_checked=1,
            rules_failed=0 if passed else 1,
        ),
    )
    # No LLM client: every executed chase now generates its message, and a
    # real provider call per test costs seconds and API spend. The template
    # fallback is deterministic — the LLM half has its own faked-client tests
    # in test_nudge_generator.py.
    monkeypatch.setattr(orch._nudge_gen, "_get_client", lambda: None)
    return orch


def _spy_executor(monkeypatch: Any, orch: Any, calls: list[dict[str, Any]]) -> None:
    """Record every execute_case_action call; hand back a successful link."""

    async def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "success": True,
            "payment_link_id": f"plink_{uuid.uuid4().hex[:8]}",
            "short_url": "https://rzp.io/test",
            "channels": ["sms"],
            "nudge_sent": True,
        }

    monkeypatch.setattr(orch._executor, "execute_case_action", spy)


async def _seed_risk_event(
    sm: async_sessionmaker[AsyncSession],
    *,
    risk_type: str = "invoice_overdue",
    reference_id: str | None = None,
    amount: int = 500000,
    customer_email: str = "accounts@acme.in",
    occurred_at: datetime | None = None,
    received_at: datetime | None = None,
    meta: dict[str, Any] | None = None,
    processed: bool = False,
) -> RiskEvent:
    event = RiskEvent(
        event_id=f"evt_{uuid.uuid4().hex[:8]}",
        risk_type=risk_type,
        reference_id=reference_id or f"ref_{uuid.uuid4().hex[:8]}",
        amount=amount,
        currency="INR",
        customer_email=customer_email,
        occurred_at=occurred_at or datetime.now(UTC),
        meta=meta or {},
        payload={"risk_type": risk_type},
        processed=processed,
    )
    if received_at is not None:
        event.received_at = received_at
    async with sm() as session:
        session.add(event)
        await session.commit()
        await session.refresh(event)
        return event


async def _case_for(
    sm: async_sessionmaker[AsyncSession], risk_type: str, subject_ref: str
) -> RecoveryCase | None:
    async with sm() as session:
        result = await session.execute(
            select(RecoveryCase).where(
                RecoveryCase.risk_type == risk_type,
                RecoveryCase.subject_ref == subject_ref,
            )
        )
        return result.scalar_one_or_none()


# ── policy lookup ─────────────────────────────────────────────────────────


def test_policy_for_covers_exactly_the_four_chased_types() -> None:
    assert set(RISK_POLICIES) == {
        "checkout_abandonment",
        "subscription_failure",
        "invoice_overdue",
        "mandate_failure",
    }
    for risk_type in RISK_POLICIES:
        assert policy_for(risk_type) is not None


def test_policy_for_returns_none_for_the_webhook_driven_rail() -> None:
    # payment_failure is event-driven: sweeping it would run the pipeline a
    # second time on every failure. Unknown strings fail closed the same way.
    assert policy_for("payment_failure") is None
    assert policy_for("something_else") is None


# ── /risks ingestion ──────────────────────────────────────────────────────


@pytest.fixture
def risk_client(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> Any:
    """The risk router alone, over the test database — same shape as the
    webhook router's fixture."""
    from src.ingestion.risk_router import router as risk_router

    monkeypatch.setattr(
        "src.ingestion.risk_router.get_settings",
        lambda: type("S", (), {"risk_webhook_secret": SECRET})(),
    )
    queued: list[str] = []

    async def fake_background(event_id: str) -> None:
        queued.append(event_id)

    monkeypatch.setattr(
        "src.ingestion.risk_router._process_risk_event_background", fake_background
    )

    app = FastAPI()
    app.include_router(risk_router, prefix="/risks")

    async def override() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = override
    test_client = TestClient(app)
    test_client.queued = queued  # type: ignore[attr-defined]
    return test_client


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _risk_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "risk_type": "checkout_abandonment",
        "reference_id": "cart_123",
        "amount_paise": 249900,
        "customer_email": "buyer@example.com",
    }
    body.update(overrides)
    return body


def test_risk_event_without_signature_is_rejected(risk_client: Any) -> None:
    resp = risk_client.post("/risks", json=_risk_body())
    assert resp.status_code == 401


def test_risk_event_with_bad_signature_is_rejected(risk_client: Any) -> None:
    body = json.dumps(_risk_body()).encode()
    resp = risk_client.post(
        "/risks", content=body, headers={"X-Risk-Signature": "deadbeef"}
    )
    assert resp.status_code == 401


def test_risk_event_is_stored_and_queued(risk_client: Any) -> None:
    body = json.dumps(_risk_body()).encode()
    resp = risk_client.post(
        "/risks", content=body, headers={"X-Risk-Signature": _sign(body)}
    )
    assert resp.status_code == 200
    assert len(risk_client.queued) == 1  # type: ignore[attr-defined]


async def test_risk_event_row_is_durably_stored(
    risk_client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    body = json.dumps(_risk_body(reference_id="cart_stored")).encode()
    resp = risk_client.post(
        "/risks", content=body, headers={"X-Risk-Signature": _sign(body)}
    )
    assert resp.status_code == 200

    async with db_sessionmaker() as session:
        rows = (await session.execute(select(RiskEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].risk_type == "checkout_abandonment"
        assert rows[0].reference_id == "cart_stored"
        assert rows[0].amount == 249900
        assert rows[0].processed is False


def test_risk_event_redelivery_is_deduplicated(risk_client: Any) -> None:
    body = json.dumps(_risk_body(event_id="evt_dup_1")).encode()
    headers = {"X-Risk-Signature": _sign(body)}
    assert risk_client.post("/risks", content=body, headers=headers).status_code == 200
    resp = risk_client.post("/risks", content=body, headers=headers)
    assert resp.status_code == 200
    assert "Already received" in resp.text
    # Only the first delivery queued a background task.
    assert len(risk_client.queued) == 1  # type: ignore[attr-defined]


def test_payment_failure_cannot_be_pushed_to_risks(risk_client: Any) -> None:
    # That rail belongs to the gateway's webhook; accepting it here would let
    # a caller bypass classification.
    body = json.dumps(_risk_body(risk_type="payment_failure")).encode()
    resp = risk_client.post(
        "/risks", content=body, headers={"X-Risk-Signature": _sign(body)}
    )
    assert resp.status_code == 400


def test_risk_event_with_bad_schema_is_rejected(risk_client: Any) -> None:
    body = json.dumps(_risk_body(amount_paise=0)).encode()
    resp = risk_client.post(
        "/risks", content=body, headers={"X-Risk-Signature": _sign(body)}
    )
    assert resp.status_code == 400


def test_risk_event_invalid_json_is_rejected(risk_client: Any) -> None:
    body = b"this is not json"
    resp = risk_client.post(
        "/risks", content=body, headers={"X-Risk-Signature": _sign(body)}
    )
    assert resp.status_code == 400


def test_risk_event_over_the_integer_ceiling_is_rejected(risk_client: Any) -> None:
    # The amount column is a 32-bit integer in paise; past that the insert
    # would 500. The schema boundary answers with a clean 400 instead.
    body = json.dumps(_risk_body(amount_paise=2_147_483_648)).encode()
    resp = risk_client.post(
        "/risks", content=body, headers={"X-Risk-Signature": _sign(body)}
    )
    assert resp.status_code == 400


def test_risk_event_with_non_inr_currency_is_rejected(risk_client: Any) -> None:
    # The customer-facing stack (₹ page, UPI rail, IST blackout) is INR-only;
    # accepting another currency would mint a link in it and then show ₹.
    body = json.dumps(_risk_body(currency="USD")).encode()
    resp = risk_client.post(
        "/risks", content=body, headers={"X-Risk-Signature": _sign(body)}
    )
    assert resp.status_code == 400


# ── the lost-event safety paths (failure case 13) ─────────────────────────


async def test_background_failure_rearms_the_risk_event(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """A crash in background processing must re-arm the event, not bury it —
    the merchant will not re-deliver after our 200."""
    from src.ingestion import risk_router as risk_router_mod

    monkeypatch.setattr(
        risk_router_mod, "async_session_factory", db_sessionmaker
    )

    async def boom(event: Any, session: Any) -> None:
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr("src.orchestrator.process_risk_event", boom)

    event = await _seed_risk_event(
        db_sessionmaker, risk_type="invoice_overdue", reference_id="inv_rearm"
    )
    await risk_router_mod._process_risk_event_background(event.event_id)

    async with db_sessionmaker() as session:
        fresh = (
            await session.execute(
                select(RiskEvent).where(RiskEvent.event_id == event.event_id)
            )
        ).scalar_one()
        assert fresh.processed is False
        assert fresh.processing_attempts == 1
        assert fresh.processing_error is not None


async def test_reconcile_risk_events_recovers_a_dropped_event(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """The sweep is the only retry mechanism there is for an event whose
    background task died: it must re-run it and mark it processed."""
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)
    monkeypatch.setattr("src.orchestrator.get_orchestrator", lambda: orch)

    event = await _seed_risk_event(
        db_sessionmaker,
        risk_type="invoice_overdue",
        reference_id="inv_dropped",
        occurred_at=datetime.now(UTC) - timedelta(minutes=10),
        received_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    async with db_sessionmaker() as session:
        recovered = await scheduler.reconcile_risk_events(session)

    assert recovered == 1
    case = await _case_for(db_sessionmaker, "invoice_overdue", "inv_dropped")
    assert case is not None
    assert case.attempts_used == 1  # immediate type chased during reconcile
    async with db_sessionmaker() as session:
        fresh = (
            await session.execute(
                select(RiskEvent).where(RiskEvent.event_id == event.event_id)
            )
        ).scalar_one()
        assert fresh.processed is True


async def test_reconcile_risk_events_rearms_instead_of_consuming(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """A transient failure during reconcile counts one attempt and re-arms —
    a database blip must not permanently skip a real risk event."""
    orch = _orchestrator(monkeypatch)
    monkeypatch.setattr("src.orchestrator.get_orchestrator", lambda: orch)

    async def boom(event: Any, session: Any) -> None:
        raise RuntimeError("transient")

    monkeypatch.setattr(orch, "process_risk_event", boom)

    event = await _seed_risk_event(
        db_sessionmaker,
        risk_type="invoice_overdue",
        reference_id="inv_blip",
        occurred_at=datetime.now(UTC) - timedelta(minutes=10),
        received_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    async with db_sessionmaker() as session:
        recovered = await scheduler.reconcile_risk_events(session)

    assert recovered == 0
    async with db_sessionmaker() as session:
        fresh = (
            await session.execute(
                select(RiskEvent).where(RiskEvent.event_id == event.event_id)
            )
        ).scalar_one()
        assert fresh.processed is False
        assert fresh.processing_attempts == 1
        assert "re-armed" in (fresh.processing_error or "")


# ── process_risk_event: opening the case ──────────────────────────────────


async def test_process_risk_event_opens_case_with_policy_budget(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)
    monkeypatch.setattr("src.orchestrator.get_orchestrator", lambda: orch)

    event = await _seed_risk_event(
        db_sessionmaker, risk_type="checkout_abandonment", reference_id="cart_abc"
    )
    async with db_sessionmaker() as session:
        await orch.process_risk_event(event, session)

    case = await _case_for(db_sessionmaker, "checkout_abandonment", "cart_abc")
    assert case is not None
    policy = policy_for("checkout_abandonment")
    assert policy is not None
    assert case.max_attempts == policy.max_attempts
    assert case.amount_at_risk == event.amount
    # The risk rail carries a merchant customer_id ("acme_cust_1") AND an
    # email. cases.customer_key() prefers the CONTACT CHANNEL, so this case
    # keys the same way a Razorpay payment failure for the same person would —
    # one ledger, one contact budget, one opt-out across both rails.
    assert case.customer_id == "email:accounts@acme.in"
    # The cart waits an hour before the first touch — the chase must NOT have
    # run yet, and next_action_at carries the first-touch time.
    assert case.attempts_used == 0
    assert calls == []
    assert case.next_action_at is not None


async def test_process_risk_event_chases_immediate_types_right_away(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)

    event = await _seed_risk_event(
        db_sessionmaker, risk_type="invoice_overdue", reference_id="inv_204"
    )
    async with db_sessionmaker() as session:
        await orch.process_risk_event(event, session)

    case = await _case_for(db_sessionmaker, "invoice_overdue", "inv_204")
    assert case is not None
    # first_action_hours == 0 → the first chase step ran during ingestion.
    assert case.attempts_used == 1
    assert len(calls) == 1
    assert calls[0]["case"].id == case.id


async def test_process_risk_event_is_idempotent(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)

    event = await _seed_risk_event(
        db_sessionmaker, risk_type="invoice_overdue", reference_id="inv_300"
    )
    async with db_sessionmaker() as session:
        await orch.process_risk_event(event, session)
    async with db_sessionmaker() as session:
        await orch.process_risk_event(event, session)

    async with db_sessionmaker() as session:
        rows = (
            await session.execute(
                select(RecoveryCase).where(
                    RecoveryCase.risk_type == "invoice_overdue",
                    RecoveryCase.subject_ref == "inv_300",
                )
            )
        ).scalars().all()
    # One case, not two — a second case would double the attempt budget.
    assert len(rows) == 1


# ── chase_case: the bounded pipeline ──────────────────────────────────────


async def _open_chase_case(
    sm: async_sessionmaker[AsyncSession],
    risk_type: str,
    *,
    subject_ref: str | None = None,
    attempts_used: int = 0,
    next_action_at: datetime | None = None,
    customer_id: str | None = "accounts@acme.in",
) -> RecoveryCase:
    policy = policy_for(risk_type)
    assert policy is not None
    case = RecoveryCase(
        risk_type=risk_type,
        subject_ref=subject_ref or f"ref_{uuid.uuid4().hex[:8]}",
        amount_at_risk=500000,
        currency="INR",
        customer_id=customer_id,
        state="open",
        attempts_used=attempts_used,
        max_attempts=policy.max_attempts,
        next_action_at=next_action_at or datetime.now(UTC) - timedelta(minutes=1),
        opened_at=datetime.now(UTC) - timedelta(hours=1),
    )
    async with sm() as session:
        session.add(case)
        await session.commit()
        await session.refresh(case)
        return case


async def test_chase_writes_attempt_ahead_of_execution(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """The money-safety invariant, on the chase path too: the attempt row must
    be committed BEFORE Razorpay is called."""
    orch = _orchestrator(monkeypatch)
    seen: dict[str, Any] = {}

    async def spy(**kwargs: Any) -> dict[str, Any]:
        async with db_sessionmaker() as reader:
            rows = (
                await reader.execute(
                    select(RetryAttempt).where(
                        RetryAttempt.recovery_case_id == kwargs["case"].id
                    )
                )
            ).scalars().all()
        seen["at_call_time"] = list(rows)
        seen["kwargs"] = kwargs
        return {"success": True, "payment_link_id": "plink_wa", "short_url": "https://rzp.io/wa"}

    monkeypatch.setattr(orch._executor, "execute_case_action", spy)

    case = await _open_chase_case(db_sessionmaker, "invoice_overdue")
    async with db_sessionmaker() as session:
        await orch.chase_case(case, session)

    pre = seen["at_call_time"]
    assert len(pre) == 1, "no committed attempt row existed when Razorpay was called"
    assert pre[0].result == "pending"
    assert pre[0].idempotency_key.startswith("chase_invoice_overdue_")
    assert pre[0].payment_failure_id is None
    # A case retry delivers a link, and a link without a message is a bare
    # demand for money — the message rides with every non-abandon action.
    assert seen["kwargs"]["nudge_message"], "retry action reached the executor mute"
    assert pre[0].payment_id is None


async def test_chase_advances_the_ladder_after_a_link_action(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)

    case = await _open_chase_case(db_sessionmaker, "invoice_overdue")
    async with db_sessionmaker() as session:
        await orch.chase_case(case, session)

    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        assert fresh is not None
        assert fresh.attempts_used == 1
        assert fresh.state == "open"
        # The re-chase floor: the sweep must not re-chase on the next tick.
        policy = policy_for("invoice_overdue")
        assert policy is not None
        assert fresh.next_action_at is not None
        assert _aware(fresh.next_action_at) > datetime.now(UTC) + timedelta(
            hours=policy.re_chase_hours - 1
        )


async def test_chase_closes_case_when_budget_spent(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)

    policy = policy_for("checkout_abandonment")
    assert policy is not None
    case = await _open_chase_case(
        db_sessionmaker, "checkout_abandonment",
        attempts_used=policy.max_attempts - 1,
    )
    async with db_sessionmaker() as session:
        await orch.chase_case(case, session)

    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        assert fresh is not None
        assert fresh.state == "exhausted"
        assert fresh.attempts_used == policy.max_attempts


async def test_chase_refuses_to_act_on_top_of_a_pending_attempt(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)

    case = await _open_chase_case(db_sessionmaker, "invoice_overdue")
    async with db_sessionmaker() as session:
        session.add(RetryAttempt(
            payment_failure_id=None, payment_id=None,
            idempotency_key=f"chase_{case.risk_type}_{case.subject_ref}_0",
            attempt_number=1, recovery_case_id=case.id,
            action_type="retry_now", agent_type="xgboost",
            guardrail_passed=True, result="pending",
        ))
        await session.commit()

    async with db_sessionmaker() as session:
        await orch.chase_case(case, session)

    # Nothing executed; the case was deferred, not chased.
    assert calls == []
    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        assert fresh is not None
        assert fresh.attempts_used == 0
        assert fresh.next_action_at is not None
        assert _aware(fresh.next_action_at) > datetime.now(UTC)


async def test_chase_defers_during_ist_blackout_without_burning_a_slot(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """The chaser picks its own moment, so it must not walk into the IST
    quiet hours and let the guardrail burn a slot on a rejection it could
    see coming: defer to the window's edge with budget intact."""
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)

    case = await _open_chase_case(db_sessionmaker, "invoice_overdue")
    night = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)  # 23:30 IST — blackout

    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        assert fresh is not None
        await orch.chase_case(fresh, session, now=night)

    assert calls == []
    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        assert fresh is not None
        assert fresh.state == "open"
        assert fresh.attempts_used == 0
        # Wakes at the blackout's edge: 07:05 IST next day = 01:35 UTC.
        assert _aware(fresh.next_action_at) == datetime(
            2026, 8, 29, 1, 35, tzinfo=UTC
        )


async def test_chase_expires_a_case_past_its_consent_window(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """Past the per-type window the case CLOSES as expired — no agent call,
    no attempt slot burned, and the sweep stops knocking on a dead case."""
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)

    policy = policy_for("checkout_abandonment")
    assert policy is not None
    case = await _open_chase_case(db_sessionmaker, "checkout_abandonment")
    # The cart window is 48h; open the case three days back.
    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        assert fresh is not None
        fresh.opened_at = datetime.now(UTC) - timedelta(hours=72)
        await session.commit()

    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        assert fresh is not None
        await orch.chase_case(fresh, session)

    assert calls == []
    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        assert fresh is not None
        assert fresh.state == "expired"
        assert fresh.attempts_used == 0
        assert "consent window" in (fresh.close_reason or "")


async def test_chase_respects_opt_out(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)

    case = await _open_chase_case(db_sessionmaker, "invoice_overdue")
    async with db_sessionmaker() as session:
        session.add(RetryLedger(
            customer_id="accounts@acme.in", consent_status="opted_out",
            opted_out_at=datetime.now(UTC),
        ))
        await session.commit()

    async with db_sessionmaker() as session:
        await orch.chase_case(case, session)

    assert calls == []
    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        assert fresh is not None
        assert fresh.attempts_used == 0


async def test_chase_is_idempotent_per_budget_slot(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """Two workers chasing the same case build the same key; the second must
    not execute again."""
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)

    case = await _open_chase_case(db_sessionmaker, "invoice_overdue")
    async with db_sessionmaker() as session:
        await orch.chase_case(case, session)

    # Re-chase with the SAME attempts_used (as a concurrent worker would see):
    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        assert fresh is not None
        fresh.attempts_used = 0  # rewind to the slot the first chase filled
        fresh.next_action_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        assert fresh is not None
        await orch.chase_case(case, session)

    # The executor was called once — the replay collided on the idempotency key.
    assert len(calls) == 1


async def test_chase_parks_retry_at_and_sets_next_action(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    retry_at = datetime.now(UTC) + timedelta(hours=24)
    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(orch, "_get_agent", lambda: _FixedAgent("retry_at", retry_at))
    monkeypatch.setattr(
        orch._guardrail,
        "validate",
        lambda *a, **k: GuardrailResult(passed=True, rejection_reasons=[], rules_checked=1),
    )

    case = await _open_chase_case(db_sessionmaker, "mandate_failure")
    async with db_sessionmaker() as session:
        await orch.chase_case(case, session)

    async with db_sessionmaker() as session:
        fresh = await session.get(RecoveryCase, case.id)
        attempt = (
            await session.execute(
                select(RetryAttempt).where(RetryAttempt.recovery_case_id == case.id)
            )
        ).scalar_one()
        assert fresh is not None
        assert attempt.result == "scheduled"
        # The case's next rung is when the parked retry fires.
        assert fresh.next_action_at is not None


# ── the scheduler's chase sweep ───────────────────────────────────────────


async def test_chase_due_cases_chases_the_four_types(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: orch)

    refs = {}
    for risk_type in RISK_POLICIES:
        case = await _open_chase_case(db_sessionmaker, risk_type)
        refs[risk_type] = case.subject_ref

    async with db_sessionmaker() as session:
        chased = await scheduler.chase_due_cases(session)

    assert chased == 4
    assert len(calls) == 4


async def test_chase_due_cases_never_touches_the_payment_rail(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: orch)

    # A webhook-driven case whose escalation backoff has elapsed. It waits for
    # its next webhook — the sweep must not run the pipeline on it again.
    async with db_sessionmaker() as session:
        session.add(RecoveryCase(
            risk_type="payment_failure", subject_ref="pay_sweep_1",
            amount_at_risk=100000, currency="INR", state="open",
            attempts_used=1, max_attempts=3,
            next_action_at=datetime.now(UTC) - timedelta(hours=1),
        ))
        await session.commit()

    async with db_sessionmaker() as session:
        chased = await scheduler.chase_due_cases(session)

    assert chased == 0
    assert calls == []


async def test_chase_due_cases_skips_cases_not_yet_due(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: orch)

    await _open_chase_case(
        db_sessionmaker, "invoice_overdue",
        next_action_at=datetime.now(UTC) + timedelta(hours=24),
    )

    async with db_sessionmaker() as session:
        chased = await scheduler.chase_due_cases(session)

    assert chased == 0
    assert calls == []


async def test_chase_due_cases_serves_oldest_first_across_types(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """One oldest-first query across all four types: under a backlog the
    longest-waiting case is chased first, whatever its type — the per-type
    loop used to serve the first type in the dict until it was empty."""
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: orch)

    # The cart went cold more recently; the invoice has been waiting longer.
    await _open_chase_case(
        db_sessionmaker, "checkout_abandonment",
        next_action_at=datetime.now(UTC) - timedelta(hours=1),
    )
    invoice = await _open_chase_case(
        db_sessionmaker, "invoice_overdue",
        next_action_at=datetime.now(UTC) - timedelta(hours=5),
    )

    async with db_sessionmaker() as session:
        chased = await scheduler.chase_due_cases(session)

    assert chased == 2
    assert calls[0]["case"].id == invoice.id


async def test_chase_due_cases_yields_at_its_time_budget(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """An already-passed deadline defers every case to the next tick — the
    sweep must not run the tick past its share of the interval."""
    import time as time_mod

    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: orch)

    await _open_chase_case(db_sessionmaker, "invoice_overdue")
    await _open_chase_case(db_sessionmaker, "subscription_failure")

    async with db_sessionmaker() as session:
        chased = await scheduler.chase_due_cases(
            session, deadline=time_mod.monotonic() - 1.0
        )

    assert chased == 0
    assert calls == []
    # Both cases are exactly as they were — due again on the next tick.
    async with db_sessionmaker() as session:
        fresh = (
            await session.execute(
                select(RecoveryCase).where(RecoveryCase.state == "open")
            )
        ).scalars().all()
        assert len(fresh) == 2
        assert all(c.attempts_used == 0 for c in fresh)


async def test_report_due_cases_counts_only_the_payment_rail(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The report filters in SQL: due chaser cases must not push payment-
    failure rows out of the fetch window (nor be counted themselves)."""
    await _open_chase_case(db_sessionmaker, "invoice_overdue")
    async with db_sessionmaker() as session:
        session.add(RecoveryCase(
            risk_type="payment_failure", subject_ref="pay_report_1",
            amount_at_risk=100000, currency="INR", state="open",
            attempts_used=1, max_attempts=3,
            next_action_at=datetime.now(UTC) - timedelta(hours=1),
        ))
        await session.commit()

    async with db_sessionmaker() as session:
        reported = await scheduler.report_due_cases(session)

    assert reported == 1


# ── firing a parked case retry ────────────────────────────────────────────


async def test_fire_due_retries_fires_a_scheduled_case_attempt(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: orch)

    case = await _open_chase_case(db_sessionmaker, "mandate_failure")
    await _seed_risk_event(
        db_sessionmaker, risk_type="mandate_failure", reference_id=case.subject_ref
    )
    async with db_sessionmaker() as session:
        session.add(RetryAttempt(
            payment_failure_id=None, payment_id=None,
            idempotency_key=f"chase_{case.risk_type}_{case.subject_ref}_0",
            attempt_number=1, recovery_case_id=case.id,
            action_type="retry_at", agent_type="xgboost",
            guardrail_passed=True, result="scheduled",
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
        ))
        await session.commit()

    async with db_sessionmaker() as session:
        fired = await scheduler.fire_due_retries(session)

    assert fired == 1
    assert len(calls) == 1
    async with db_sessionmaker() as session:
        attempt = (
            await session.execute(
                select(RetryAttempt).where(RetryAttempt.recovery_case_id == case.id)
            )
        ).scalar_one()
        assert attempt.result == "success"


async def test_fire_case_attempt_cancels_when_customer_opted_out(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = _orchestrator(monkeypatch)
    calls: list[dict[str, Any]] = []
    _spy_executor(monkeypatch, orch, calls)
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: orch)

    case = await _open_chase_case(db_sessionmaker, "mandate_failure")
    async with db_sessionmaker() as session:
        session.add(RetryLedger(
            customer_id=case.customer_id, consent_status="opted_out",
            opted_out_at=datetime.now(UTC),
        ))
        session.add(RetryAttempt(
            payment_failure_id=None, payment_id=None,
            idempotency_key=f"chase_{case.risk_type}_{case.subject_ref}_0",
            attempt_number=1, recovery_case_id=case.id,
            action_type="retry_at", agent_type="xgboost",
            guardrail_passed=True, result="scheduled",
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
        ))
        await session.commit()

    async with db_sessionmaker() as session:
        fired = await scheduler.fire_due_retries(session)

    assert fired == 0
    assert calls == []
    async with db_sessionmaker() as session:
        attempt = (
            await session.execute(
                select(RetryAttempt).where(RetryAttempt.recovery_case_id == case.id)
            )
        ).scalar_one()
        assert attempt.result == "cancelled"


# ── attribution through a case attempt ────────────────────────────────────


async def test_capture_attributes_through_a_case_link(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """A risk case has no payment_id — attribution must still work through the
    link's external_ref."""
    from src.cases import attribute_capture

    case = await _open_chase_case(db_sessionmaker, "invoice_overdue")
    async with db_sessionmaker() as session:
        session.add(RetryAttempt(
            payment_failure_id=None, payment_id=None,
            idempotency_key=f"chase_{case.risk_type}_{case.subject_ref}_0",
            attempt_number=1, recovery_case_id=case.id,
            action_type="retry_now", agent_type="xgboost",
            guardrail_passed=True, result="success",
            external_ref="plink_attr_1",
        ))
        await session.commit()

    async with db_sessionmaker() as session:
        credited = await attribute_capture(
            session, amount=500000, recovered_ref="pay_new_1", link_id="plink_attr_1"
        )
        await session.commit()

    assert credited is not None
    assert credited.id == case.id
    assert credited.state == "recovered"
    assert credited.amount_recovered == 500000
    assert credited.recovered_via_attempt_id is not None


# ── the recovery page for risk cases ──────────────────────────────────────


@pytest.fixture
def page_client(db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any) -> Any:
    from src.config import get_settings
    from src.customer.routes import router as customer_router

    get_settings.cache_clear()
    monkeypatch.setenv("RECOVERY_LINK_SECRET", "page-test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pay.example.in")
    # The hero (and the risk-type label in it) only renders with a merchant
    # name — it is the page's trust anchor.
    monkeypatch.setenv("MERCHANT_NAME", "Acme Books")

    app = FastAPI()
    app.include_router(customer_router)

    async def override() -> Any:
        async with db_sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = override
    client = TestClient(app)
    yield client
    get_settings.cache_clear()


async def _seed_risk_case(
    sm: async_sessionmaker[AsyncSession],
    risk_type: str = "invoice_overdue",
    *,
    opened_hours_ago: float = 1.0,
    state: str = "open",
) -> RecoveryCase:
    case = RecoveryCase(
        risk_type=risk_type,
        subject_ref=f"ref_{uuid.uuid4().hex[:8]}",
        amount_at_risk=500000,
        currency="INR",
        customer_id="accounts@acme.in",
        state=state,
        attempts_used=0,
        max_attempts=4,
        opened_at=datetime.now(UTC) - timedelta(hours=opened_hours_ago),
    )
    async with sm() as session:
        session.add(case)
        await session.commit()
        await session.refresh(case)
        return case


async def test_risk_case_page_renders_honest_copy(
    page_client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from src import recovery_link

    case = await _seed_risk_case(db_sessionmaker, "checkout_abandonment")
    token = recovery_link.mint(case.id)
    assert token is not None
    resp = page_client.get(f"/recover/{token}")
    assert resp.status_code == 200
    html = resp.text
    # The order's copy, not the payment's: no "payment attempted" story for a
    # cart that never paid.
    assert "About your order from" in html
    assert "Payment attempted" not in html
    assert "Your order was left incomplete" in html
    # And the pay button is offered — the case is inside its window.
    assert "/pay" in html


async def test_risk_case_page_ignores_a_colliding_payment_id(
    page_client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """subject_ref is merchant-chosen for risk cases. If it collides with a
    real payment id, the page must still render the risk type's story — the
    failure lookup only runs for the payment rail."""
    from src import recovery_link
    from src.models import PaymentFailure

    case = await _seed_risk_case(db_sessionmaker, "invoice_overdue")
    async with db_sessionmaker() as session:
        session.add(PaymentFailure(
            payment_id=case.subject_ref,  # the collision
            order_id="order_collide",
            amount=500000, method="card",
            error_code="BAD_REQUEST_ERROR",
            failure_class="insufficient_funds", is_retryable=True,
            webhook_event_id=uuid.uuid4(),
            failed_at=datetime.now(UTC),
        ))
        await session.commit()

    token = recovery_link.mint(case.id)
    assert token is not None
    resp = page_client.get(f"/recover/{token}")
    assert resp.status_code == 200
    html = resp.text
    assert "About your invoice from" in html
    assert "About your payment to" not in html
    assert "The bank declined it" not in html


async def test_risk_case_page_stops_past_its_window(
    page_client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from src import recovery_link

    # checkout_abandonment's window is 48h; open the case three days back.
    case = await _seed_risk_case(
        db_sessionmaker, "checkout_abandonment", opened_hours_ago=72.0
    )
    token = recovery_link.mint(case.id)
    assert token is not None
    resp = page_client.get(f"/recover/{token}")
    assert resp.status_code == 200
    assert "/pay" not in resp.text


async def test_self_serve_pay_for_a_risk_case_mints_a_link(
    page_client: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    from src import recovery_link
    from src.orchestrator import get_orchestrator

    case = await _seed_risk_case(db_sessionmaker, "invoice_overdue")
    # The risk event supplies the customer's email for the link.
    await _seed_risk_event(
        db_sessionmaker, risk_type="invoice_overdue", reference_id=case.subject_ref
    )

    orch = get_orchestrator()

    async def fake_case_action(**kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "payment_link_id": "plink_selfserve",
            "short_url": "https://rzp.io/selfserve",
        }

    monkeypatch.setattr(orch._executor, "execute_case_action", fake_case_action)

    token = recovery_link.mint(case.id)
    assert token is not None
    resp = page_client.post(f"/recover/{token}/pay", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "https://rzp.io/selfserve"


# ── xgboost heuristic branches for the risk classes ───────────────────────


def test_xgboost_heuristic_covers_the_risk_classes() -> None:
    from src.agent.xgboost_baseline import XGBoostBaseline

    now = datetime.now(UTC)

    def ctx(failure_class: str, **kw: Any) -> FailureContext:
        base = dict(
            risk_type="invoice_overdue",
            payment_id="ref_1",
            failure_class=failure_class,
            error_code=failure_class.upper(),
            amount=500000,
            method="unknown",
            failed_at=now,
            current_time=now,
            hour_of_day=14,
            day_of_week=2,
        )
        base.update(kw)
        return FailureContext(**base)  # type: ignore[arg-type]

    agent = XGBoostBaseline(model_path="")  # force the rule heuristic

    fresh_cart = agent.predict(ctx("abandoned_checkout", previous_retry_outcomes=[]))
    assert fresh_cart.action == "nudge_customer"

    chased_cart = agent.predict(
        ctx("abandoned_checkout", previous_retry_outcomes=["success"])
    )
    assert chased_cart.action == "abandon"

    invoice = agent.predict(ctx("invoice_overdue"))
    assert invoice.action == "nudge_customer"

    mandate = agent.predict(ctx("mandate_debit_failed", previous_retry_outcomes=[]))
    assert mandate.action == "retry_at"

    subscription = agent.predict(ctx("subscription_charge_failed"))
    assert subscription.action == "switch_rail"


# ── guardrail consent-window override ─────────────────────────────────────


def test_guardrail_consent_window_override() -> None:
    from src.guardrail.rules import GuardrailRules

    rules = GuardrailRules()
    failed_at = datetime.now(UTC) - timedelta(hours=100)
    now = datetime.now(UTC)

    # Past the global 72h window…
    passed, _ = rules.check_consent_window(failed_at, now)
    assert passed is False
    # …but inside a 720h receivables window.
    passed, _ = rules.check_consent_window(failed_at, now, window_hours=720)
    assert passed is True
