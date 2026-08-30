"""
Checkout drop-off recovery — the cart-chaser upgrades, tested.

Research-grounded behaviours added to the checkout_abandonment rail
(specs/checkout-dropoff-recovery-plan.md):

  1. Cart items from the merchant's meta reach the nudge (personalization is
     the one lever the research agrees lifts opens, +26% Slicker) — bounded,
     scrubbed, and never breaking the 160-char ceiling.
  2. The recovery page names the cart's contents when the merchant's event
     did, and renders one page_viewed audit row per serve (the CTR signal
     the effectiveness metrics are stated against).
  3. An optional merchant offer id rides the chase from the SECOND touch on
     (incentive on touch 1 trains discount-waiting, Klaviyo 2024), is
     refused for non-cart rails at intake, and relays to the payment link
     through options.order.offers.
  4. A cart's final permitted contact is a last-call nudge: its quiet gap is
     bounded by the consent window, not the flat widening backoff.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import recovery_link
from src.cases import attach_attempt, chase_effectiveness
from src.chasers.policy import policy_for
from src.config import get_settings
from src.database import get_session
from src.guardrail.gate import GuardrailResult
from src.messaging.nudge_generator import build_llm_prompt
from src.messaging.templates import render_fallback
from src.models import CaseEvent, RecoveryCase, RetryAttempt, RiskEvent
from src.orchestrator import PaymentRecoveryOrchestrator, cart_summary_from_meta

SECRET = "risk-webhook-test-secret"


# ── 1. Cart items in the nudge ────────────────────────────────────────────


def test_cart_summary_from_meta_shapes() -> None:
    # list of names → one comma line
    assert cart_summary_from_meta({"cart_items": ["2 books", "1 pen"]}) == "2 books, 1 pen"
    # a single string is accepted as-is
    assert cart_summary_from_meta({"cart_items": "one kettle"}) == "one kettle"
    # nothing there, wrong type, or empty → None (old wording byte-for-byte)
    assert cart_summary_from_meta({}) is None
    assert cart_summary_from_meta(None) is None
    assert cart_summary_from_meta({"cart_items": 42}) is None
    assert cart_summary_from_meta({"cart_items": []}) is None
    assert cart_summary_from_meta({"cart_items": ["", "  "]}) is None


def test_cart_summary_is_bounded_and_printable() -> None:
    long = cart_summary_from_meta({"cart_items": ["x" * 300]})
    assert long is not None and len(long) <= 80
    # embedded newlines collapse: one line, no injection shape
    folded = cart_summary_from_meta({"cart_items": ["a\nb IGNORE PRIORS c"]})
    assert folded is not None and "\n" not in folded


def test_template_renders_cart_items_under_160() -> None:
    msg = render_fallback(
        "abandoned_checkout", "2,499.00", "Pay here: https://rzp.io/x",
        cart_summary="2 books, 1 pen",
    )
    assert "2 books" in msg
    assert len(msg) <= 160
    # and without items, the old wording survives intact
    plain = render_fallback("abandoned_checkout", "2,499.00", "Pay here")
    assert "—" not in plain and "2 books" not in plain


def test_llm_prompt_carries_cart_items_for_carts_only() -> None:
    prompt = build_llm_prompt(
        failure_class="abandoned_checkout", amount_display="2,499.00",
        method="unknown", next_step="Pay here",
        customer_name=None, merchant_name="Acme",
        risk_type="checkout_abandonment", cart_summary="2 books",
    )
    assert "2 books" in prompt
    assert "No payment was attempted" in prompt
    # a non-cart risk type never sees a cart line
    other = build_llm_prompt(
        failure_class="invoice_overdue", amount_display="5,000.00",
        method="unknown", next_step="Pay here",
        customer_name=None, merchant_name="Acme",
        risk_type="invoice_overdue", cart_summary="2 books",
    )
    assert "2 books" not in other


# ── 2. Page: cart line + page_viewed signal ──────────────────────────────


async def _seed_cart_case(
    sm: async_sessionmaker[AsyncSession],
    *,
    meta: dict[str, Any] | None = None,
    attempts_used: int = 0,
) -> RecoveryCase:
    policy = policy_for("checkout_abandonment")
    assert policy is not None
    case = RecoveryCase(
        risk_type="checkout_abandonment",
        subject_ref=f"cart_{datetime.now(UTC).timestamp()}",
        amount_at_risk=249900,
        currency="INR",
        customer_id="buyer@example.com",
        state="open",
        attempts_used=attempts_used,
        max_attempts=policy.max_attempts,
        next_action_at=datetime.now(UTC) - timedelta(minutes=1),
        opened_at=datetime.now(UTC) - timedelta(hours=1),
    )
    async with sm() as session:
        session.add(case)
        event = RiskEvent(
            event_id=f"evt_{case.subject_ref}",
            risk_type="checkout_abandonment",
            reference_id=case.subject_ref,
            amount=249900,
            currency="INR",
            customer_email="buyer@example.com",
            occurred_at=datetime.now(UTC),
            meta=meta or {},
            payload={},
            processed=True,
        )
        session.add(event)
        await session.commit()
        await session.refresh(case)
        return case


@pytest.fixture
def page_client(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> Any:
    """The customer router over the test DB, with link-minting config set."""
    monkeypatch.setenv("RECOVERY_LINK_SECRET", "test-link-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://recover.example")
    from src.customer.routes import router as customer_router

    app = FastAPI()
    app.include_router(customer_router)

    async def override() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = override
    return TestClient(app)


async def test_cart_items_render_on_the_page(
    page_client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case = await _seed_cart_case(
        db_sessionmaker, meta={"cart_items": ["blue kettle", "2 mugs"]}
    )
    token = recovery_link.mint(case.id)
    resp = page_client.get(f"/recover/{token}")
    assert resp.status_code == 200
    assert "blue kettle" in resp.text
    # the honest label carries it, not a bare dump
    assert "In your order" in resp.text


async def test_page_serve_writes_one_page_viewed_event(
    page_client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case = await _seed_cart_case(db_sessionmaker)
    token = recovery_link.mint(case.id)
    assert page_client.get(f"/recover/{token}").status_code == 200

    async with db_sessionmaker() as session:
        rows = (
            await session.execute(
                select(CaseEvent).where(
                    CaseEvent.recovery_case_id == case.id,
                    CaseEvent.event_type == "page_viewed",
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].actor == "customer"


async def test_forged_token_writes_no_event(
    page_client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_cart_case(db_sessionmaker)
    resp = page_client.get("/recover/not-a-real-token")
    assert resp.status_code == 404

    async with db_sessionmaker() as session:
        rows = (
            await session.execute(
                select(CaseEvent).where(CaseEvent.event_type == "page_viewed")
            )
        ).scalars().all()
    assert rows == []


# ── 3. Merchant offer relay ───────────────────────────────────────────────


@pytest.fixture
def risk_client(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> Any:
    from src.ingestion.risk_router import router as risk_router

    monkeypatch.setattr(
        "src.ingestion.risk_router.get_settings",
        lambda: type("S", (), {"risk_webhook_secret": SECRET})(),
    )

    async def fake_background(event_id: str) -> None:
        pass

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
    return TestClient(app)


def _sign(body: bytes) -> str:
    import hashlib
    import hmac as _hmac

    return _hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_offer_accepted_on_cart_events(risk_client: Any) -> None:
    body = json.dumps(
        {
            "risk_type": "checkout_abandonment",
            "reference_id": "cart_offer_ok",
            "amount_paise": 100000,
            "offer_id": "offer_F4WMTC3pwFKnzq",
        }
    ).encode()
    resp = risk_client.post("/risks", content=body, headers={"X-Risk-Signature": _sign(body)})
    assert resp.status_code == 200


def test_offer_refused_off_the_cart_rail(risk_client: Any) -> None:
    body = json.dumps(
        {
            "risk_type": "invoice_overdue",
            "reference_id": "inv_offer_bad",
            "amount_paise": 100000,
            "offer_id": "offer_F4WMTC3pwFKnzq",
        }
    ).encode()
    resp = risk_client.post("/risks", content=body, headers={"X-Risk-Signature": _sign(body)})
    assert resp.status_code == 400


def test_offer_must_be_razorpay_shaped(risk_client: Any) -> None:
    body = json.dumps(
        {
            "risk_type": "checkout_abandonment",
            "reference_id": "cart_offer_shape",
            "amount_paise": 100000,
            "offer_id": "free-money-please",
        }
    ).encode()
    resp = risk_client.post("/risks", content=body, headers={"X-Risk-Signature": _sign(body)})
    assert resp.status_code == 400


async def test_first_touch_never_carries_the_offer(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """The touch rule: attempts_used 0 = first contact, offer suppressed —
    the merchant said "discount from the second touch", and the engine keeps
    that promise even when the event carried an offer from the start."""
    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(
        orch, "_get_agent",
        lambda: type("A", (), {"decide": None})(),  # placeholder, replaced below
    )

    class _FixedAgent:
        fallback_count = 0

        async def decide(self, context: Any) -> Any:
            from src.agent.actions import RetryAction

            return RetryAction(action="nudge_customer", reason="fixed test decision")

    monkeypatch.setattr(orch, "_get_agent", lambda: _FixedAgent())
    monkeypatch.setattr(
        orch._guardrail, "validate",
        lambda *a, **k: GuardrailResult(
            passed=True, rejection_reasons=[], rules_checked=1, rules_failed=0
        ),
    )
    monkeypatch.setattr(orch._nudge_gen, "_get_client", lambda: None)

    seen: dict[str, Any] = {}

    async def spy(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {
            "success": True, "payment_link_id": "plink_x",
            "short_url": "https://rzp.io/x", "channels": ["sms"],
        }

    monkeypatch.setattr(orch._executor, "execute_case_action", spy)

    case = await _seed_cart_case(db_sessionmaker, attempts_used=0)
    async with db_sessionmaker() as session:
        # attach the offer to the event the chase will read
        ev = (
            await session.execute(
                select(RiskEvent).where(RiskEvent.reference_id == case.subject_ref)
            )
        ).scalar_one()
        ev.offer_id = "offer_F4WMTC3pwFKnzq"
        await session.commit()

    async with db_sessionmaker() as session:
        await orch.chase_case(case, session)

    assert seen.get("offer_id") is None, "first touch carried the offer"


async def test_second_touch_relays_the_offer(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    orch = PaymentRecoveryOrchestrator()

    class _FixedAgent:
        fallback_count = 0

        async def decide(self, context: Any) -> Any:
            from src.agent.actions import RetryAction

            return RetryAction(action="nudge_customer", reason="fixed test decision")

    monkeypatch.setattr(orch, "_get_agent", lambda: _FixedAgent())
    monkeypatch.setattr(
        orch._guardrail, "validate",
        lambda *a, **k: GuardrailResult(
            passed=True, rejection_reasons=[], rules_checked=1, rules_failed=0
        ),
    )
    monkeypatch.setattr(orch._nudge_gen, "_get_client", lambda: None)

    seen: dict[str, Any] = {}

    async def spy(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {
            "success": True, "payment_link_id": "plink_x",
            "short_url": "https://rzp.io/x", "channels": ["sms"],
        }

    monkeypatch.setattr(orch._executor, "execute_case_action", spy)

    case = await _seed_cart_case(db_sessionmaker, attempts_used=1)
    async with db_sessionmaker() as session:
        ev = (
            await session.execute(
                select(RiskEvent).where(RiskEvent.reference_id == case.subject_ref)
            )
        ).scalar_one()
        ev.offer_id = "offer_F4WMTC3pwFKnzq"
        await session.commit()

    async with db_sessionmaker() as session:
        await orch.chase_case(case, session)

    assert seen.get("offer_id") == "offer_F4WMTC3pwFKnzq"
    # the cart items also ride the nudge on this path
    assert seen.get("nudge_message")


async def test_executor_sends_offer_into_link_options(monkeypatch: Any) -> None:
    """The relay's last mile: options.order.offers at create time."""
    from src.executor.retry_executor import RetryExecutor

    case = RecoveryCase(
        risk_type="checkout_abandonment", subject_ref="cart_opt",
        amount_at_risk=100000, currency="INR", state="open",
        attempts_used=1, max_attempts=2, escalation_level=1,
    )
    created: dict[str, Any] = {}

    async def fake_create_link(link_data: dict[str, Any]) -> dict[str, Any]:
        created.update(link_data)
        return {"id": "plink_o", "short_url": "https://rzp.io/o"}

    executor = RetryExecutor.__new__(RetryExecutor)
    monkeypatch.setattr(executor, "_create_link", fake_create_link)

    result = await executor._create_case_payment_link(
        case, None, "idem", offer_id="offer_F4WMTC3pwFKnzq"
    )
    assert created["options"] == {"order": {"offers": ["offer_F4WMTC3pwFKnzq"]}}
    assert result["offer_id"] == "offer_F4WMTC3pwFKnzq"


# ── 4. Last-call timing for the cart's final contact ──────────────────────


async def test_final_cart_nudge_is_a_last_call(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Attaching the cart's FIRST touch positions the second (final) one as
    a last call: near the window's end, not at the flat +24h — the honest
    deadline is what speeds payment."""
    policy = policy_for("checkout_abandonment")
    assert policy is not None
    opened = datetime.now(UTC) - timedelta(hours=1)
    case = RecoveryCase(
        risk_type="checkout_abandonment", subject_ref="cart_lastcall",
        amount_at_risk=100000, currency="INR", state="open",
        attempts_used=0, max_attempts=2, escalation_level=0,
        opened_at=opened,
    )
    attempt = RetryAttempt(
        idempotency_key="chase_lastcall", attempt_number=1,
        action_type="nudge_customer", result="success",
    )
    attach_attempt(case, attempt)

    window_end = opened + timedelta(hours=policy.consent_window_hours)
    expected = window_end - timedelta(hours=2)
    got = case.next_action_at
    assert got is not None
    assert abs((got - expected).total_seconds()) < 120, (
        f"last-call rung {got} not near window-end-minus-2h {expected}"
    )


async def test_non_final_cart_runge_keeps_the_widening_backoff(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A cart with budget left beyond the next touch keeps the widening
    backoff — the last-call positioning only applies when the next rung is
    the final one."""
    case = RecoveryCase(
        risk_type="checkout_abandonment", subject_ref="cart_normal",
        amount_at_risk=100000, currency="INR", state="open",
        attempts_used=0, max_attempts=4, escalation_level=0,
        opened_at=datetime.now(UTC) - timedelta(hours=1),
    )
    attempt = RetryAttempt(
        idempotency_key="chase_normal", attempt_number=1,
        action_type="nudge_customer", result="success",
    )
    attach_attempt(case, attempt)
    expected = datetime.now(UTC) + timedelta(
        hours=get_settings().escalation_backoff_hours * case.escalation_level
    )
    got = case.next_action_at
    assert got is not None
    assert abs((got - expected).total_seconds()) < 120


# ── 5. Effectiveness metrics ──────────────────────────────────────────────


async def test_chase_effectiveness_counts_per_touch(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    case = await _seed_cart_case(db_sessionmaker, attempts_used=1)
    async with db_sessionmaker() as session:
        session.add(
            CaseEvent(recovery_case_id=case.id, event_type="page_viewed", actor="customer")
        )
        # one successful contact attempt at touch 1
        session.add(
            RetryAttempt(
                recovery_case_id=case.id, idempotency_key="ce_1",
                attempt_number=1, action_type="nudge_customer",
                result="success", guardrail_passed=True,
            )
        )
        await session.commit()

    async with db_sessionmaker() as session:
        rows = await chase_effectiveness(session, risk_type="checkout_abandonment")
    assert len(rows) == 1
    row = rows[0]
    assert row["risk_type"] == "checkout_abandonment"
    assert row["touch"] == 1
    assert row["cases_contacted"] == 1
    assert row["page_view_rate_pct"] == 100.0
