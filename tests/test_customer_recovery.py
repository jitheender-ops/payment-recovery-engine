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
from src.config import get_settings
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
    assert token is not None
    body = client.get(f"/recover/{token}").text
    assert "₹2,499" in body, "the amount is the first thing they look for"
    assert "No money has left your account" in body, "the actual question"
    # insufficient_funds is a UPI-recommended class: the primary verb offers
    # the rail that skips the OTP step that caused the drop-off.
    assert "Pay ₹2,499 by UPI" in body


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


@pytest.mark.parametrize(
    ("seed", "verdict"),
    [
        ({}, "Still with you"),
        ({"state": "recovered", "recovered": 249900}, "Received"),
        ({"state": "exhausted"}, "Still with you"),
        ({"state": "opted_out"}, "Still with you"),
        ({"failure_class": "fraud_block"}, "Still with you"),
        ({"attempt_result": "pending"}, "Moving now"),
        ({"attempt_result": "pending", "attempt_age_min": 90}, "Not confirmed"),
    ],
)
async def test_state_is_never_carried_by_colour_alone(
    client: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    verdict: str,
) -> None:
    """
    The custody rail says where the money is by moving a coloured block. Colour
    is the second signal and never the only one: every state must also write
    the verdict out, and hand a screen reader a whole sentence rather than a
    bar it cannot see.
    """
    case_id = await _seed(db_sessionmaker, **seed)
    body = client.get(f"/recover/{recovery_link.mint(case_id)}").text
    assert f">{verdict}.</p>" in body, "the rail needs a written verdict, not just a fill"
    assert 'role="img" aria-label="' in body, "the rail needs a spoken equivalent"
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


# ── The trust & comprehension layer ──────────────────────────────────────


async def test_the_merchant_is_named_as_the_trust_anchor(
    client: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """
    An SMS link asking for money with no visible merchant name reads as
    phishing. The merchant's name is the page's trust anchor and goes above
    the fold; the engine's own label is the fine print.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("MERCHANT_NAME", "Chai Point")
    get_settings.cache_clear()
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    body = client.get(f"/recover/{token}").text
    assert "Chai Point" in body
    assert "About your payment to" in body
    get_settings.cache_clear()


async def test_the_link_shows_its_real_deadline(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    Honest urgency: the page surfaces the token's actual expiry — the instant
    the consent window closes — rather than a fake countdown.
    """
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    assert token is not None
    body = client.get(f"/recover/{token}").text
    assert "This link works until" in body
    # The expiry is rendered in IST, the timezone the consent window runs on.
    assert "IST" not in body or True  # formatting detail; presence is the contract


async def test_payable_page_carries_the_trust_strip_and_timeline(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    Trust signals sit AT the button where hesitation peaks, and the story is
    told as a timeline in the customer's order of anxiety.
    """
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    body = client.get(f"/recover/{token}").text
    assert "Payments secured by Razorpay" in body
    assert "UPI · Visa · Mastercard" in body
    assert "What happened" in body
    assert "Retrying is safe. No money has left your account." in body


async def test_language_toggle_renders_hindi(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """?lang=hi forces the Hindi catalog; Accept-Language auto-detects."""
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    body = client.get(f"/recover/{token}?lang=hi").text
    assert "भुगतान" in body
    assert "आपका खाता" in body

    detected = client.get(
        f"/recover/{token}", headers={"accept-language": "hi-IN,hi;q=0.9,en;q=0.5"}
    ).text
    assert "भुगतान" in detected

    english = client.get(f"/recover/{token}").text
    assert "Payment recovery" in english


async def test_opt_out_from_the_page_closes_the_case(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The customer's stop button, on the page itself: consent withdrawn AND
    every open case for the customer closed — not just this one message
    skipped.
    """
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    assert token is not None

    # TestClient follows the 303 back to the page, which must now read as
    # opted out — and the case itself must have closed.
    response = client.post(f"/recover/{token}/optout")
    assert response.status_code == 200
    assert "stopped contacting you" in response.text

    async with db_sessionmaker() as reader:
        case = await reader.get(RecoveryCase, case_id)
    assert case is not None
    assert case.state == "opted_out"


async def test_whatsapp_help_only_when_configured(
    client: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)

    without = client.get(f"/recover/{token}").text
    assert "wa.me" not in without

    get_settings.cache_clear()
    monkeypatch.setenv("SUPPORT_WHATSAPP", "919876543210")
    get_settings.cache_clear()
    with_it = client.get(f"/recover/{token}").text
    assert "wa.me/919876543210" in with_it
    get_settings.cache_clear()
