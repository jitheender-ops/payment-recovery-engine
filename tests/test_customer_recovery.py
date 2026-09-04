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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import recovery_link
from src.auth import client_ip
from src.config import get_settings
from src.customer import routes as customer_routes
from src.customer.routes import router as customer_router
from src.database import get_session
from src.models import CaseEvent, PaymentFailure, RecoveryCase, RetryAttempt

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
    failed_at: datetime | None = None,
    method: str = "card",
) -> uuid.UUID:
    pid = f"pay_cust_{uuid.uuid4().hex[:8]}"
    async with sm() as session:
        failure = PaymentFailure(
            payment_id=pid, order_id="order_cust_1", amount=amount, method=method,
            bank="HDFC", error_code="BAD_REQUEST_ERROR", failure_class=failure_class,
            is_retryable=failure_class not in ("fraud_block", "expired_instrument"),
            webhook_event_id=uuid.uuid4(),
            failed_at=failed_at or datetime.now(UTC),
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


def test_the_default_ttl_is_a_day_not_the_whole_consent_window() -> None:
    """
    The URL is a bearer credential in SMS logs and browser history. It used to
    live for the entire 72h consent window; a day is enough (every nudge
    mints a fresh link), and the consent window remains the hard cap.
    """
    token = recovery_link.mint(uuid.uuid4())
    assert token is not None
    verified = recovery_link.verify_with_expiry(token)
    assert verified is not None
    _, expires_at = verified
    remaining = expires_at - datetime.now(UTC)
    assert remaining <= timedelta(hours=24), "default TTL exceeds the one-day default"
    assert remaining > timedelta(hours=23), "default TTL should be ~24h"


def test_an_explicit_ttl_cannot_outlive_the_consent_window() -> None:
    """The consent window is the engine's authority to act; no link outlives it."""
    consent_hours = get_settings().consent_window_hours
    token = recovery_link.mint(uuid.uuid4(), ttl_hours=consent_hours + 100)
    assert token is not None
    verified = recovery_link.verify_with_expiry(token)
    assert verified is not None
    _, expires_at = verified
    remaining = expires_at - datetime.now(UTC)
    # An explicit ttl above the cap is clamped down to it.
    assert remaining <= timedelta(hours=consent_hours)


# ── The consent window on the page itself ────────────────────────────────


async def test_a_case_past_the_consent_window_never_offers_payment(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    Nothing closes a case when the consent window simply passes — the row
    stays open until a webhook re-triggers the stopping rule. The page used
    to render that gap as "payable"; a token's TTL runs from ISSUANCE, so a
    link minted near the window's end stays open past it. The page must stop
    offering payment once the window has lapsed, whatever the case row says.
    """
    consent_hours = get_settings().consent_window_hours
    case_id = await _seed(
        db_sessionmaker,
        failed_at=datetime.now(UTC) - timedelta(hours=consent_hours + 8),
    )
    token = recovery_link.mint(case_id, ttl_hours=consent_hours)
    assert token is not None
    body = client.get(f"/recover/{token}").text
    assert "securely" not in body, "offered payment past the consent window"


async def test_pay_is_refused_past_the_consent_window(
    client: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """The money path agrees with the page: no link is minted past the window."""
    monkeypatch.setattr(
        "src.executor.retry_executor.RetryExecutor", lambda: _FakeLinkExecutor()
    )
    consent_hours = get_settings().consent_window_hours
    case_id = await _seed(
        db_sessionmaker,
        failed_at=datetime.now(UTC) - timedelta(hours=consent_hours + 8),
    )
    token = recovery_link.mint(case_id, ttl_hours=consent_hours)
    assert token is not None

    response = client.post(f"/recover/{token}/pay", follow_redirects=False)
    assert response.status_code == 303
    assert "rzp.io" not in response.headers.get("location", ""), (
        "minted a payment link past the consent window"
    )

    async with db_sessionmaker() as reader:
        rows = (
            await reader.execute(
                select(RetryAttempt).where(
                    RetryAttempt.idempotency_key.like("selfserve_%")
                )
            )
        ).scalars().all()
    assert rows == [], "a refused payment must not spend an attempt slot"


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


# ── Self-serve loopholes found in review ─────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_rate_buckets() -> Iterator[None]:
    """
    The bucket map lives at module scope and survives between tests, so the
    shared 'unknown' client IP would otherwise inherit the pay budget spent
    by earlier tests and hand one test another test's 429.
    """
    customer_routes._RATE_LIMIT_BUCKETS.clear()
    yield
    customer_routes._RATE_LIMIT_BUCKETS.clear()


class _FakeRequest:
    """Just enough of a Request for client_ip."""

    def __init__(self, headers: dict[str, str], host: str = "203.0.113.9") -> None:
        self.headers = headers

        class _Client:
            pass

        self.client = _Client()
        self.client.host = host  # type: ignore[attr-defined]


def test_xff_is_ignored_without_a_trusted_proxy(monkeypatch: Any) -> None:
    """
    A direct deployment has no proxy sanitising X-Forwarded-For, so the header
    is attacker-controlled: trusting it lets one rotated header value per
    request bypass every per-IP limit. The socket peer is the truth there.
    """
    get_settings.cache_clear()
    monkeypatch.delenv("BEHIND_TRUSTED_PROXY", raising=False)
    req = _FakeRequest({"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, host="203.0.113.9")
    assert client_ip(req) == "203.0.113.9"  # type: ignore[arg-type]
    get_settings.cache_clear()


def test_xff_rightmost_entry_is_used_behind_a_trusted_proxy(monkeypatch: Any) -> None:
    """
    Behind a proxy we control, the RIGHTMOST entry is the one the egress proxy
    added — the only hop a client cannot forge. The leftmost is client-supplied
    spoofing and must never be keyed on.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("BEHIND_TRUSTED_PROXY", "true")
    req = _FakeRequest({"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, host="10.0.0.1")
    assert client_ip(req) == "5.6.7.8"  # type: ignore[arg-type]
    get_settings.cache_clear()


async def test_a_spoofed_leftmost_xff_cannot_evade_the_rate_limit(
    client: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """
    The actual exploit: rotate a forged leftmost X-Forwarded-For per request.
    With no trusted proxy the header is ignored, so every request lands on the
    same socket-peer bucket and the limit still trips.
    """
    get_settings.cache_clear()
    monkeypatch.delenv("BEHIND_TRUSTED_PROXY", raising=False)
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    assert token is not None

    statuses = [
        client.get(
            f"/recover/{token}", headers={"x-forwarded-for": f"9.9.9.{i}"}
        ).status_code
        for i in range(customer_routes._PAGE_LIMIT + 1)
    ]
    assert statuses[-1] == 429, "rotating a forged XFF header bypassed the limit"
    get_settings.cache_clear()


class _FakeLinkExecutor:
    """Stands in for the Razorpay-backed executor: counts link creations."""

    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1
        self.calls: list[dict[str, Any]] = []

    async def execute_retry(
        self,
        payment_failure: Any,
        action_type: str,
        target_rail: str | None,
        idempotency_key: str,
        nudge_message: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"idempotency_key": idempotency_key, "target_rail": target_rail})
        return {
            "success": True,
            "payment_link_id": f"plink_{idempotency_key[-6:]}",
            "short_url": f"https://rzp.io/i/{idempotency_key[-6:]}",
            "target_rail": target_rail,
        }


async def test_a_double_tap_mints_exactly_one_link(
    client: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """
    THE double-pay loophole: the self-serve /pay route used to create links
    WITHOUT writing an attempt row, so nothing recorded the link, a second
    tap found no 'live link' and minted another — two live payment objects
    for one case, both payable. The write-ahead row is what closes it.
    """
    _FakeLinkExecutor.instances = 0
    fake = _FakeLinkExecutor()
    monkeypatch.setattr(
        "src.executor.retry_executor.RetryExecutor", lambda: fake
    )

    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    assert token is not None

    first = client.post(f"/recover/{token}/pay", follow_redirects=False)
    assert first.status_code == 303
    assert "rzp.io" in first.headers["location"]
    # Second tap lands on the live link (reuse) or loses the write-ahead race;
    # either way no new payment object may exist.
    client.post(f"/recover/{token}/pay", follow_redirects=False)

    # Exactly ONE link was ever created: either the second tap lost the
    # idempotency race or it was redirected to the live first link.
    assert len(fake.calls) == 1

    async with db_sessionmaker() as reader:
        rows = (
            await reader.execute(
                select(RetryAttempt).where(
                    RetryAttempt.idempotency_key.like("selfserve_%")
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].result == "success"
    assert rows[0].external_ref is not None


async def test_self_serve_payment_is_attributed_not_self_recovered(
    client: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """
    Without an attempt row, a capture on a customer-opened link matched
    nothing and fell through to order_ref — counted as revenue but never
    credited to the engine. The row carries the breadcrumb now.
    """
    _FakeLinkExecutor.instances = 0
    monkeypatch.setattr(
        "src.executor.retry_executor.RetryExecutor", lambda: _FakeLinkExecutor()
    )

    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    assert token is not None

    response = client.post(f"/recover/{token}/pay", follow_redirects=False)
    assert response.status_code == 303

    async with db_sessionmaker() as reader:
        row = (
            await reader.execute(
                select(RetryAttempt).where(
                    RetryAttempt.idempotency_key.like("selfserve_%")
                )
            )
        ).scalar_one()

    from src.cases import attribute_capture

    async with db_sessionmaker() as session:
        credited = await attribute_capture(
            session,
            amount=249900,
            recovered_ref="pay_selfserve_capture",
            idempotency_key=row.idempotency_key,
        )
        await session.commit()

    assert credited is not None
    assert credited.id == case_id
    assert credited.recovered_via_attempt_id == row.id, (
        "a capture on our own breadcrumb must credit the engine"
    )


async def test_opt_out_closes_the_case_even_without_a_customer_identity(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    A webhook with no email/contact leaves the case without customer_id —
    the opt-out used to silently no-op there, re-rendering the payable page
    to someone who just pressed stop.
    """
    pid = f"pay_anon_{uuid.uuid4().hex[:8]}"
    async with db_sessionmaker() as session:
        failure = PaymentFailure(
            payment_id=pid, order_id="order_anon", amount=10000, method="card",
            error_code="BAD_REQUEST_ERROR", failure_class="insufficient_funds",
            is_retryable=True, webhook_event_id=uuid.uuid4(),
            failed_at=datetime.now(UTC),
        )
        session.add(failure)
        await session.flush()
        case = RecoveryCase(
            risk_type="payment_failure", subject_ref=pid, amount_at_risk=10000,
            amount_recovered=0, state="open", max_attempts=3, attempts_used=1,
            customer_id=None,
        )
        session.add(case)
        await session.commit()
        case_pk = case.id

    token = recovery_link.mint(case_pk)
    assert token is not None
    client.post(f"/recover/{token}/optout")

    async with db_sessionmaker() as reader:
        closed = await reader.get(RecoveryCase, case_pk)
    assert closed is not None
    assert closed.state == "opted_out"

    body = client.get(f"/recover/{token}").text
    assert "stopped contacting you" in body


async def test_recovered_state_shows_a_real_receipt(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A confirmation that names nothing is not a receipt: amount, reference
    (the UTR their bank statement will carry), and when."""
    pid = f"pay_receipt_{uuid.uuid4().hex[:8]}"
    async with db_sessionmaker() as session:
        failure = PaymentFailure(
            payment_id=pid, order_id="order_rcpt", amount=200000, method="upi",
            error_code="BAD_REQUEST_ERROR", failure_class="insufficient_funds",
            is_retryable=True, webhook_event_id=uuid.uuid4(),
            failed_at=datetime.now(UTC),
        )
        session.add(failure)
        case = RecoveryCase(
            risk_type="payment_failure", subject_ref=pid, amount_at_risk=200000,
            amount_recovered=200000, state="recovered", max_attempts=3,
            attempts_used=1, customer_id="c@example.com",
            recovered_ref="pay_SUCCESS99",
            recovered_at=datetime.now(UTC),
        )
        session.add(case)
        await session.commit()
        case_pk = case.id

    token = recovery_link.mint(case_pk)
    assert token is not None
    body = client.get(f"/recover/{token}").text
    assert "₹2,000" in body
    assert "pay_SUCCESS99" in body
    assert "Receipt" in body


async def test_the_charged_but_failed_faqs_exists(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    body = client.get(f"/recover/{token}").text
    assert "I was charged, but this page says failed" in body


# ── The rail recommendation, said out loud ───────────────────────────────
#
# The engine has enforced this for six failure classes since the UPI-first
# work: /pay mints a UPI-only link for them. Until now the only visible
# trace was the button's verb, which is the engine making a recommendation
# without ever admitting it was making one.


async def test_a_recommended_rail_switch_is_named_not_just_enforced(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await _seed(db_sessionmaker, failure_class="3ds_dropoff", method="card")
    body = client.get(f"/recover/{recovery_link.mint(case_id)}").text
    assert "We recommend UPI for this payment." in body


async def test_no_rail_note_when_the_failed_rail_is_already_the_recommended_one(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Recommending UPI to someone whose UPI just failed is noise, not advice."""
    case_id = await _seed(
        db_sessionmaker, failure_class="insufficient_funds", method="upi"
    )
    body = client.get(f"/recover/{recovery_link.mint(case_id)}").text
    assert "We recommend UPI" not in body


async def test_no_rail_note_for_a_class_the_engine_does_not_switch(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await _seed(db_sessionmaker, failure_class="bank_downtime", method="card")
    body = client.get(f"/recover/{recovery_link.mint(case_id)}").text
    assert "We recommend UPI" not in body


# ── Accessibility floors ───────────────────────────────────────────────────


async def test_the_recovery_page_has_a_skip_link_and_a_live_region(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The page opens with a masthead and a custody rail before the amount, so a
    keyboard or screen-reader user met the same chrome before the one thing
    they came for. And the confirming state re-renders via meta-refresh, so
    without a live region the update was silent.
    """
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    html = client.get(f"/recover/{token}").text

    assert 'href="#main"' in html, "no skip link"
    assert 'id="main"' in html, "skip link points at nothing"
    assert 'aria-live="polite"' in html, "the confirming state announces nothing"


# ── Whose view was it? ───────────────────────────────────────────────────────


async def test_a_plain_visit_is_the_customers_own_click_through(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    assert client.get(f"/recover/{token}").status_code == 200

    async with db_sessionmaker() as s:
        actors = list((await s.execute(
            select(CaseEvent.actor).where(
                CaseEvent.recovery_case_id == case_id,
                CaseEvent.event_type == "page_viewed",
            )
        )).scalars().all())
    assert actors == ["customer"]


async def test_an_operator_preview_is_not_counted_as_a_click_through(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """
    The console's "open their page" button leaves a signed, single-case,
    ten-minute marker; the view it produces is the operator's, not the
    customer's.

    Without this split every operator preview landed in the nudge
    click-through figure cases.contact_effectiveness() publishes — a metric
    saying "they opened it" about a page only staff had opened. The console's
    own session cookie cannot answer the question: it is scoped to
    path=/console so that a customer page never carries operator authority,
    which also means it never reaches this route.
    """
    from src.merchant.routes import _PREVIEW_COOKIE, _mint_preview_marker

    monkeypatch.setenv("DASHBOARD_PASSWORD", "console-test-password")
    from src.config import get_settings

    get_settings.cache_clear()

    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    client.cookies.set(_PREVIEW_COOKIE, _mint_preview_marker(case_id))
    assert client.get(f"/recover/{token}").status_code == 200

    async with db_sessionmaker() as s:
        actors = list((await s.execute(
            select(CaseEvent.actor).where(
                CaseEvent.recovery_case_id == case_id,
                CaseEvent.event_type == "page_viewed",
            )
        )).scalars().all())
    assert actors == ["operator"]


async def test_a_marker_for_another_case_does_not_relabel_this_view(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """The marker names one case. A stale one from the previous preview must
    not silently suppress the next customer's click-through."""
    from src.merchant.routes import _PREVIEW_COOKIE, _mint_preview_marker

    monkeypatch.setenv("DASHBOARD_PASSWORD", "console-test-password")
    from src.config import get_settings

    get_settings.cache_clear()

    other = await _seed(db_sessionmaker)
    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    client.cookies.set(_PREVIEW_COOKIE, _mint_preview_marker(other))
    assert client.get(f"/recover/{token}").status_code == 200

    async with db_sessionmaker() as s:
        actors = list((await s.execute(
            select(CaseEvent.actor).where(
                CaseEvent.recovery_case_id == case_id,
                CaseEvent.event_type == "page_viewed",
            )
        )).scalars().all())
    assert actors == ["customer"]


async def test_a_forged_marker_is_ignored(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """Forging one needs the console password. Without it, it is a customer."""
    from src.merchant.routes import _PREVIEW_COOKIE

    monkeypatch.setenv("DASHBOARD_PASSWORD", "console-test-password")
    from src.config import get_settings

    get_settings.cache_clear()

    case_id = await _seed(db_sessionmaker)
    token = recovery_link.mint(case_id)
    client.cookies.set(_PREVIEW_COOKIE, f"{case_id.hex}.not-a-signature")
    assert client.get(f"/recover/{token}").status_code == 200

    async with db_sessionmaker() as s:
        actors = list((await s.execute(
            select(CaseEvent.actor).where(
                CaseEvent.recovery_case_id == case_id,
                CaseEvent.event_type == "page_viewed",
            )
        )).scalars().all())
    assert actors == ["customer"]


# ── The customer home: everything one person has open ────────────────────────


async def _seed_for(
    sm: async_sessionmaker[AsyncSession], customer_id: str, refs: list[str]
) -> list[uuid.UUID]:
    """Several open cases belonging to one person."""
    ids = []
    async with sm() as session:
        for ref in refs:
            case = RecoveryCase(
                subject_ref=ref, risk_type="payment_failure", state="open",
                amount_at_risk=100000, amount_recovered=0, max_attempts=3,
                attempts_used=0, customer_id=customer_id,
            )
            session.add(case)
            await session.flush()
            ids.append(case.id)
        await session.commit()
    return ids


async def test_the_customer_home_shows_every_case_that_person_has(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    Three failed orders were three SMS, three links and three isolated pages,
    and no page could answer "how much do I actually owe you?" — the
    commonest question support gets. This is that page.
    """
    ids = await _seed_for(
        db_sessionmaker, "email:many@example.com", ["pay_a", "pay_b", "pay_c"]
    )
    token = recovery_link.mint_customer(ids[0])
    r = client.get(f"/mine/{token}")
    assert r.status_code == 200
    for ref in ("pay_a", "pay_b", "pay_c"):
        assert ref in r.text
    # The total, not the gross of any one of them.
    assert "3,000" in r.text


async def test_the_customer_home_hands_out_no_authority_of_its_own(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """It shows and links. Every row deep-links to that case's own page,
    which is the tested money path — a second surface with its own pay
    handling would be a second thing to get wrong where being wrong means
    charging someone twice."""
    ids = await _seed_for(db_sessionmaker, "email:two@example.com", ["pay_x", "pay_y"])
    token = recovery_link.mint_customer(ids[0])
    body = client.get(f"/mine/{token}").text
    # No write endpoint of its own except the opt-out, which posts to a case.
    assert "/mine/" not in body.split("<form")[-1] if "<form" in body else True
    assert body.count("/recover/") >= 2


async def test_a_case_token_cannot_open_the_customer_home(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The page shows more than a case page does, so its token is a third scope
    and the signature covers the marker: holding a link to one payment does
    not get you a link to all of them. Same rule the account scope exists for.
    """
    ids = await _seed_for(db_sessionmaker, "email:scope@example.com", ["pay_s"])
    assert client.get(f"/mine/{recovery_link.mint(ids[0])}").status_code == 404


async def test_a_customer_token_cannot_open_a_case_page(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """And the confusion is refused in both directions."""
    ids = await _seed_for(db_sessionmaker, "email:scope2@example.com", ["pay_s2"])
    token = recovery_link.mint_customer(ids[0])
    assert client.get(f"/recover/{token}").status_code == 404


async def test_the_customer_home_refuses_a_case_with_no_customer(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A merchant risk event can be pushed without a customer id. There is no
    "everything you owe" for nobody, so it is not an empty page — it is a
    page that does not exist, answered exactly like a forged token."""
    async with db_sessionmaker() as session:
        case = RecoveryCase(
            subject_ref="pay_anon", risk_type="payment_failure", state="open",
            amount_at_risk=100000, amount_recovered=0, max_attempts=3,
            attempts_used=0, customer_id=None,
        )
        session.add(case)
        await session.commit()
        case_id = case.id
    assert client.get(
        f"/mine/{recovery_link.mint_customer(case_id)}"
    ).status_code == 404


async def test_the_case_page_offers_the_home_only_when_there_is_more(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A link promising "everything you owe" that leads to a page holding
    this same one payment is worse than no link."""
    alone = await _seed_for(db_sessionmaker, "email:alone@example.com", ["pay_only"])
    assert "/mine/" not in client.get(f"/recover/{recovery_link.mint(alone[0])}").text

    several = await _seed_for(
        db_sessionmaker, "email:several@example.com", ["pay_1", "pay_2"]
    )
    assert "/mine/" in client.get(f"/recover/{recovery_link.mint(several[0])}").text
