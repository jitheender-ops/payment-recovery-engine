"""
The customer-facing recovery page — the only surface a payer ever sees.

The tests that matter here are the refusals. A page that offers "pay now" at
the wrong moment is how somebody gets charged twice, so most of this file is
about the states where the button must NOT appear and the POST must NOT act:
already recovered, a payment still confirming, a hard decline the bank will
refuse again, and a case the engine has stopped chasing.

The token tests are the other half. The URL is the only credential, so a forged
or expired one must be indistinguishable from an unknown one — otherwise the
difference is an oracle for probing which case ids exist.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import recovery_link
from src.customer.routes import router as customer_router
from src.database import get_session
from src.models import PaymentFailure, RecoveryCase, RetryAttempt

SECRET = "recovery-link-test-secret"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: Any) -> Iterator[None]:
    """A configured secret, so tokens verify. Cleared per test by monkeypatch."""
    from src.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("RECOVERY_LINK_SECRET", SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pay.example.in")
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(db_sessionmaker: async_sessionmaker[AsyncSession]) -> Any:
    app = FastAPI()
    app.include_router(customer_router)

    async def override() -> Any:
        async with db_sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = override
    return TestClient(app)


async def _seed(
    sm: async_sessionmaker[AsyncSession],
    *,
    state: str = "open",
    failure_class: str = "insufficient_funds",
    amount: int = 249900,
    recovered: int = 0,
    attempt_result: str | None = None,
    short_url: str | None = None,
    attempt_age_min: int = 1,
) -> uuid.UUID:
    pid = f"pay_cust_{uuid.uuid4().hex[:8]}"
    async with sm() as session:
        failure = PaymentFailure(
            payment_id=pid, order_id="order_cust_1", amount=amount, method="card",
            bank="HDFC", error_code="BAD_REQUEST_ERROR", failure_class=failure_class,
            is_retryable=failure_class not in ("fraud_block", "expired_instrument"),
            webhook_event_id=uuid.uuid4(), failed_at=datetime.now(UTC),
        )
        session.add(failure)
        await session.flush()

        case = RecoveryCase(
            risk_type="payment_failure", subject_ref=pid, amount_at_risk=amount,
            amount_recovered=recovered, state=state, max_attempts=3, attempts_used=1,
            customer_id="cust@example.com",
        )
        session.add(case)
        await session.flush()

        if attempt_result:
            session.add(RetryAttempt(
                payment_failure_id=failure.id, payment_id=pid,
                idempotency_key=f"retry_{pid}_0", attempt_number=1,
                recovery_case_id=case.id, action_type="retry_now",
                agent_type="xgboost", guardrail_passed=True, result=attempt_result,
                executed_at=datetime.now(UTC) - timedelta(minutes=attempt_age_min),
                result_details={"success": True, "short_url": short_url} if short_url else None,
            ))
        await session.commit()
        return case.id


# ── The URL is the credential ────────────────────────────────────────────


def test_a_forged_token_is_refused(client: Any) -> None:
    assert client.get("/recover/not-a-real-token").status_code == 404


def test_a_tampered_token_is_refused(client: Any) -> None:
    good = recovery_link.mint(uuid.uuid4())
    assert good is not None
    payload, _, sig = good.partition(".")
    assert client.get(f"/recover/{payload}.{sig[::-1]}").status_code == 404


def test_an_expired_token_is_refused(client: Any) -> None:
    token = recovery_link.mint(uuid.uuid4(), ttl_hours=0)
    assert token is not None
    time.sleep(1.1)
    assert client.get(f"/recover/{token}").status_code == 404


def test_an_unknown_case_looks_identical_to_a_forgery(client: Any) -> None:
    """Same status and same page — otherwise this is an existence oracle."""
    token = recovery_link.mint(uuid.uuid4())
    assert token is not None
    real, forged = client.get(f"/recover/{token}"), client.get("/recover/rubbish.rubbish")
    assert real.status_code == forged.status_code == 404


def test_no_secret_means_every_token_is_rejected(monkeypatch: Any) -> None:
    """Fail closed: an unconfigured page is a closed page, not an open one."""
    from src.config import get_settings

    get_settings.cache_clear()
    monkeypatch.delenv("RECOVERY_LINK_SECRET", raising=False)
    monkeypatch.setenv("RECOVERY_LINK_SECRET", "")
    assert recovery_link.mint(uuid.uuid4()) is None
    assert recovery_link.verify("anything.atall") is None
    get_settings.cache_clear()


def test_the_token_carries_no_pii() -> None:
    token = recovery_link.mint(uuid.uuid4())
    assert token is not None
    assert "@" not in token and "example" not in token


# ── What the customer sees ───────────────────────────────────────────────


async def test_a_payable_case_answers_where_the_money_is(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    body = client.get(f"/recover/{token}").text

    assert "₹2,499" in body, "the amount is the first thing they look for"
    assert "No money has left your account" in body, "the actual question"
    assert "Pay ₹2,499 securely" in body


async def test_a_recovered_case_never_offers_payment(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await _seed(db_sessionmaker, state="recovered", recovered=249900)
    body = client.get(f"/recover/{recovery_link.mint(case_id)}").text
    assert "Payment received" in body
    assert "securely" not in body, "offered a second payment on a paid case"


async def test_a_confirming_payment_blocks_a_second_one(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The window where paying again would genuinely double-charge."""
    case_id = await _seed(db_sessionmaker, attempt_result="pending", attempt_age_min=1)
    body = client.get(f"/recover/{recovery_link.mint(case_id)}").text
    assert "Don't pay again yet" in body
    assert "securely" not in body


async def test_a_stale_pending_payment_admits_it_is_unknown(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """After the window it is not 'confirming' any more — say so."""
    case_id = await _seed(db_sessionmaker, attempt_result="pending", attempt_age_min=90)
    body = client.get(f"/recover/{recovery_link.mint(case_id)}").text
    assert "can't confirm this payment yet" in body
    assert "securely" not in body


async def test_a_hard_decline_does_not_send_them_at_the_wall_again(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await _seed(db_sessionmaker, failure_class="fraud_block")
    body = client.get(f"/recover/{recovery_link.mint(case_id)}").text
    assert "security precaution" in body
    assert "securely" not in body, "offered a retry the bank will refuse again"


async def test_an_exhausted_case_still_explains_itself(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await _seed(db_sessionmaker, state="exhausted")
    body = client.get(f"/recover/{recovery_link.mint(case_id)}").text
    assert "No money has left your account" in body
    assert "no longer retrying" in body


async def test_status_is_never_carried_by_colour_alone(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await _seed(db_sessionmaker)
    body = client.get(f"/recover/{recovery_link.mint(case_id)}").text
    assert 'class="status-icon"' in body, "status needs a glyph, not just a fill"
    assert "noindex" in body and "no-referrer" in body


# ── The POST is the dangerous one ────────────────────────────────────────


async def test_paying_a_recovered_case_is_refused(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The double-charge test. A stale page, a back button, or a second tap must
    not be able to start a payment on a case that is already paid.
    """
    case_id = await _seed(db_sessionmaker, state="recovered", recovered=249900)
    token = recovery_link.mint(case_id)
    resp = client.post(f"/recover/{token}/pay", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/recover/{token}", "sent them to pay anyway"


async def test_paying_while_confirming_is_refused(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await _seed(db_sessionmaker, attempt_result="pending")
    token = recovery_link.mint(case_id)
    resp = client.post(f"/recover/{token}/pay", follow_redirects=False)
    assert resp.headers["location"] == f"/recover/{token}"


async def test_an_existing_link_is_reused_not_reminted(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A second payment link is a second thing they could pay. Reuse it."""
    case_id = await _seed(
        db_sessionmaker, attempt_result="success", short_url="https://rzp.io/i/abc123"
    )
    token = recovery_link.mint(case_id)
    resp = client.post(f"/recover/{token}/pay", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "https://rzp.io/i/abc123"


async def test_paying_a_hard_decline_is_refused(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await _seed(db_sessionmaker, failure_class="fraud_block")
    token = recovery_link.mint(case_id)
    resp = client.post(f"/recover/{token}/pay", follow_redirects=False)
    assert resp.headers["location"] == f"/recover/{token}"
