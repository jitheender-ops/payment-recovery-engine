"""
Tests for the retry executor's HTTP boundary.

These guard two defects that are invisible in normal operation and only surface
under a slow or hung Razorpay endpoint: a missing request timeout, and a
synchronous SDK call made directly from a coroutine.
"""

from __future__ import annotations

import threading
from typing import Any

import requests

from src.config import get_settings
from src.executor.retry_executor import (
    RetryExecutor,
    _TimeoutSession,
    sanitize_customer_contact,
    sanitize_customer_email,
)
from src.models import PaymentFailure, RecoveryCase


def _failure() -> PaymentFailure:
    """An unsaved PaymentFailure — enough for the executor, no DB needed."""
    return PaymentFailure(
        payment_id="pay_test_exec_001",
        order_id="order_test_exec_001",
        amount=50000,
        currency="INR",
        method="card",
        failure_class="network_error",
        customer_email="test@example.com",
        customer_contact="+919876543210",
    )


def _case() -> RecoveryCase:
    """An unsaved RecoveryCase — enough for execute_case_action, no DB."""
    return RecoveryCase(
        risk_type="invoice_overdue",
        subject_ref="inv_san_1",
        amount_at_risk=50000,
        currency="INR",
        state="open",
        attempts_used=0,
        max_attempts=4,
        escalation_level=0,
    )


# ── Merchant-typed contact data must not be able to kill a chase ─────────


def test_the_contact_sanitizers_keep_real_shapes_and_drop_junk() -> None:
    assert sanitize_customer_email("a@b.co") == "a@b.co"
    assert sanitize_customer_email(" a@b.co ") == "a@b.co"
    assert sanitize_customer_email(None) is None
    assert sanitize_customer_email("nope") is None
    assert sanitize_customer_email("a@b") is None
    assert sanitize_customer_email("a b@c.co") is None

    assert sanitize_customer_contact("+919876543210") == "+919876543210"
    assert sanitize_customer_contact("+91 98765-43210") == "+919876543210"
    assert sanitize_customer_contact("(022) 4000 1234") == "02240001234"
    assert sanitize_customer_contact(None) is None
    assert sanitize_customer_contact("12345") is None
    assert sanitize_customer_contact("call me maybe") is None


async def test_case_link_survives_a_misshapen_email(monkeypatch: Any) -> None:
    """A typo'd merchant email must not 400 the link call and burn the case's
    attempt budget — the link mints without a prefilled customer."""
    executor = RetryExecutor()
    seen: dict[str, Any] = {}

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        seen["data"] = data
        return {"id": "plink_san_1", "short_url": "https://rzp.io/i/san"}

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)

    result = await executor.execute_case_action(
        case=_case(),
        action_type="retry_now",
        target_rail=None,
        idempotency_key="chase_invoice_overdue_inv_san_1_0",
        customer_email="not-an-email",
        customer_contact="12345",
    )

    assert result["success"] is True
    assert result["payment_link_id"] == "plink_san_1"
    assert seen["data"]["customer"] == {}


async def test_case_nudge_skips_notify_for_dropped_contacts(
    monkeypatch: Any,
) -> None:
    """If both contacts are junk the link still mints; notify is skipped
    instead of firing at a link that carries no customer."""
    executor = RetryExecutor()
    notified: list[tuple[str, str]] = []

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        return {"id": "plink_san_2", "short_url": "https://rzp.io/i/san2"}

    def fake_notify(link_id: str, channel: str) -> None:
        notified.append((link_id, channel))

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)
    monkeypatch.setattr(executor._client.payment_link, "notifyBy", fake_notify)

    result = await executor.execute_case_action(
        case=_case(),
        action_type="nudge_customer",
        target_rail=None,
        idempotency_key="chase_invoice_overdue_inv_san_1_1",
        nudge_message="Your invoice is past due.",
        customer_email="bad",
        customer_contact="also bad",
    )

    assert result["success"] is True
    assert notified == []
    assert result["channels"] == []


async def test_case_nudge_still_notifies_valid_contacts(
    monkeypatch: Any,
) -> None:
    executor = RetryExecutor()
    notified: list[tuple[str, str]] = []

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        return {"id": "plink_san_3", "short_url": "https://rzp.io/i/san3"}

    def fake_notify(link_id: str, channel: str) -> None:
        notified.append((link_id, channel))

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)
    monkeypatch.setattr(executor._client.payment_link, "notifyBy", fake_notify)

    result = await executor.execute_case_action(
        case=_case(),
        action_type="nudge_customer",
        target_rail=None,
        idempotency_key="chase_invoice_overdue_inv_san_1_2",
        nudge_message="Your invoice is past due.",
        customer_email="accounts@acme.in",
        customer_contact="+919876543210",
    )

    assert result["success"] is True
    assert notified == [("plink_san_3", "sms"), ("plink_san_3", "email")]
    assert result["channels"] == ["sms", "email"]


# ── A case "retry" that nobody is told about is a dead chase ─────────────


async def test_case_retry_delivers_the_link_by_default(monkeypatch: Any) -> None:
    """Risk types have no instrument to re-present silently — a retry MINTS a
    link, and a link the customer never hears about recovers nothing while
    reporting success. Every chase action must therefore deliver it."""
    executor = RetryExecutor()
    notified: list[tuple[str, str]] = []

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        return {"id": "plink_deliver_1", "short_url": "https://rzp.io/i/d1"}

    def fake_notify(link_id: str, channel: str) -> None:
        notified.append((link_id, channel))

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)
    monkeypatch.setattr(executor._client.payment_link, "notifyBy", fake_notify)

    result = await executor.execute_case_action(
        case=_case(),
        action_type="retry_now",
        target_rail=None,
        idempotency_key="chase_invoice_overdue_inv_san_1_3",
        nudge_message="Your invoice is past due.",
        customer_email="accounts@acme.in",
        customer_contact="+919876543210",
    )

    assert result["success"] is True
    assert notified, "a chase retry that never notifies is a dead link"
    assert result["nudge_sent"] is True


async def test_case_retry_keeps_the_target_rail_when_delivering(
    monkeypatch: Any,
) -> None:
    """A switch-to-UPI must stay a UPI-only link even though delivery now
    routes every action through the nudge path."""
    executor = RetryExecutor()
    seen: dict[str, Any] = {}

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        seen["data"] = data
        return {"id": "plink_deliver_2", "short_url": "https://rzp.io/i/d2"}

    def fake_notify(link_id: str, channel: str) -> None:
        return None

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)
    monkeypatch.setattr(executor._client.payment_link, "notifyBy", fake_notify)

    await executor.execute_case_action(
        case=_case(),
        action_type="switch_rail",
        target_rail="upi",
        idempotency_key="chase_invoice_overdue_inv_san_1_4",
        nudge_message="Your invoice is past due.",
        customer_email="accounts@acme.in",
    )

    assert seen["data"].get("upi_link") is True


async def test_self_serve_retry_does_not_notify(monkeypatch: Any) -> None:
    """The customer clicked pay themselves and is redirected to the link —
    notifying them about their own click would be a nudge they never asked
    for."""
    executor = RetryExecutor()
    notified: list[tuple[str, str]] = []

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        return {"id": "plink_self_1", "short_url": "https://rzp.io/i/s1"}

    def fake_notify(link_id: str, channel: str) -> None:
        notified.append((link_id, channel))

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)
    monkeypatch.setattr(executor._client.payment_link, "notifyBy", fake_notify)

    result = await executor.execute_case_action(
        case=_case(),
        action_type="retry_now",
        target_rail=None,
        idempotency_key="selfserve_inv_san_1_0",
        customer_email="accounts@acme.in",
        customer_contact="+919876543210",
        notify_customer=False,
    )

    assert result["success"] is True
    assert notified == []


# ── UPI-only links must not be able to kill a chase ──────────────────────


async def test_upi_only_link_falls_back_when_refused(monkeypatch: Any) -> None:
    """Razorpay refuses upi_link in test mode (and on accounts without the
    feature). The 400 is about the FLAG, not the payment — the chase must
    carry on with a generic link and record the downgrade."""
    import razorpay as _razorpay

    executor = RetryExecutor()
    seen: list[dict[str, Any]] = []

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        seen.append(data)
        if data.get("upi_link"):
            raise _razorpay.errors.BadRequestError(
                "UPI Payment Links is not supported in Test Mode."
            )
        return {"id": "plink_fb_1", "short_url": "https://rzp.io/i/fb"}

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)

    result = await executor.execute_case_action(
        case=_case(),
        action_type="switch_rail",
        target_rail="upi",
        idempotency_key="chase_invoice_overdue_inv_san_1_5",
        nudge_message="Your invoice is past due.",
        customer_email="accounts@acme.in",
        notify_customer=False,
    )

    assert result["success"] is True
    assert result["rail_fallback"] is True
    assert len(seen) == 2
    assert seen[0].get("upi_link") is True
    assert "upi_link" not in seen[1]


async def test_non_upi_bad_request_still_fails(monkeypatch: Any) -> None:
    """The fallback is for the UPI flag only — any other 400 must surface,
    or a genuinely broken request would silently mint the wrong link."""
    import razorpay as _razorpay

    executor = RetryExecutor()

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        raise _razorpay.errors.BadRequestError("amount must be at least 100")

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)

    result = await executor.execute_case_action(
        case=_case(),
        action_type="retry_now",
        target_rail=None,
        idempotency_key="chase_invoice_overdue_inv_san_1_6",
        notify_customer=False,
    )

    assert result["success"] is False
    assert "amount must be at least 100" in str(result["error"])


def test_timeout_session_injects_a_default_timeout(monkeypatch: Any) -> None:
    """Every request gets a timeout even though no call site passes one."""
    captured: dict[str, Any] = {}

    def fake_request(self: Any, *args: Any, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(requests.Session, "request", fake_request)

    session = _TimeoutSession(7.5)
    # Exactly how razorpay's Client.request dispatches: getattr(session, verb)(...)
    session.post("https://example.invalid/v1/payment_links", data="{}", auth=("k", "s"))

    assert captured["timeout"] == 7.5


def test_explicit_timeout_is_not_overridden(monkeypatch: Any) -> None:
    """setdefault, not force — a caller that knows better still wins."""
    captured: dict[str, Any] = {}

    def fake_request(self: Any, *args: Any, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(requests.Session, "request", fake_request)

    _TimeoutSession(7.5).post("https://example.invalid/x", data="{}", timeout=1.0)

    assert captured["timeout"] == 1.0


def test_executor_wires_the_configured_timeout_into_the_sdk() -> None:
    """The SDK client must be built on the timeout-bearing session."""
    executor = RetryExecutor()

    # Read out of the assert: pytest renders every sub-expression of a failing
    # assert, and Settings' repr is the whole config including the Razorpay
    # key id. An unrelated failure should not paste that into a CI log.
    expected = get_settings().razorpay_timeout_seconds

    assert isinstance(executor._client.session, _TimeoutSession)
    assert executor._client.session._timeout == expected


async def test_payment_link_create_runs_off_the_event_loop(monkeypatch: Any) -> None:
    """
    The blocking SDK call must not execute on the loop thread.

    Asserting on the thread identity rather than on elapsed time keeps this
    deterministic: a sleep-based test would pass on a fast machine even if the
    call were blocking.
    """
    executor = RetryExecutor()
    loop_thread = threading.current_thread()
    seen: dict[str, Any] = {}

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        seen["thread"] = threading.current_thread()
        return {"id": "plink_test_1", "short_url": "https://rzp.io/i/test"}

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)

    result = await executor._create_payment_link(_failure(), None, "retry_pay_test_exec_001_0")

    assert result["success"] is True
    assert result["payment_link_id"] == "plink_test_1"
    assert seen["thread"] is not loop_thread


async def test_nudge_notifications_run_off_the_event_loop(monkeypatch: Any) -> None:
    """notifyBy is a second blocking SDK call — it must be dispatched too."""
    executor = RetryExecutor()
    loop_thread = threading.current_thread()
    notify_threads: list[threading.Thread] = []

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        return {"id": "plink_test_2", "short_url": "https://rzp.io/i/test2"}

    def fake_notify(link_id: str, medium: str) -> dict[str, Any]:
        notify_threads.append(threading.current_thread())
        return {"success": True}

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)
    monkeypatch.setattr(executor._client.payment_link, "notifyBy", fake_notify)

    result = await executor._send_nudge(_failure(), "retry_pay_test_exec_001_1", "try again")

    # One for sms (contact set), one for email (email set).
    assert len(notify_threads) == 2
    assert all(t is not loop_thread for t in notify_threads)
    assert result["nudge_sent"] is True


async def test_the_timeout_survives_all_the_way_to_the_transport(monkeypatch: Any) -> None:
    """
    End to end through the real SDK, mocking only the last hop before the socket.

    The tests above mock either Session.request — the method _TimeoutSession
    overrides — or payment_link.create, which sits above the SDK's HTTP layer.
    Neither would notice if razorpay stopped dispatching through the session, or
    if requests stopped forwarding the timeout into send(). This one runs the
    genuine Client.request -> session.post -> Session.request -> Session.send
    chain and reads the timeout off the bottom of it.

    It covers both Fix A defects at once: a lost timeout, and a blocking call
    back on the loop thread.
    """
    executor = RetryExecutor()
    loop_thread = threading.current_thread()
    expected_timeout = get_settings().razorpay_timeout_seconds
    seen: dict[str, Any] = {}

    def fake_send(self: Any, request: Any, **kwargs: Any) -> requests.Response:
        seen["timeout"] = kwargs.get("timeout")
        seen["thread"] = threading.current_thread()
        seen["url"] = request.url
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"id": "plink_transport", "short_url": "https://rzp.io/i/t"}'
        response.request = request
        return response

    monkeypatch.setattr(requests.Session, "send", fake_send)

    result = await executor._create_payment_link(_failure(), None, "retry_pay_test_exec_001_2")

    assert seen["timeout"] == expected_timeout
    assert seen["thread"] is not loop_thread
    assert seen["url"].endswith("/payment_links")
    assert result["success"] is True
    assert result["payment_link_id"] == "plink_transport"


# ── The link must be for what is STILL owed ──────────────────────────────


async def test_case_link_is_for_the_outstanding_not_the_original(
    monkeypatch: Any,
) -> None:
    """
    A part-paid case must be chased for the BALANCE.

    `amount_at_risk` is what was owed when the case opened and never shrinks;
    the case stays open until the balance clears. Minting against it asked a
    customer who had already paid ₹600 of ₹1,000 for the full ₹1,000 a second
    time — normal on a B2B invoice, where part payments are routine.
    """
    executor = RetryExecutor()
    seen: dict[str, Any] = {}

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        seen["data"] = data
        return {"id": "plink_out_1", "short_url": "https://rzp.io/i/out"}

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)

    case = _case()                 # amount_at_risk = 50000 paise
    case.amount_recovered = 30000  # ₹300 of ₹500 already paid

    result = await executor.execute_case_action(
        case=case,
        action_type="retry_now",
        target_rail=None,
        idempotency_key="chase_invoice_overdue_inv_san_1_1",
        notify_customer=False,
    )

    assert result["success"] is True
    assert seen["data"]["amount"] == 20000, (
        "the link re-charged money the customer had already paid"
    )


async def test_case_link_refuses_when_nothing_is_outstanding(
    monkeypatch: Any,
) -> None:
    """A fully-paid case mints nothing. The stopping rules should close it
    first; this refuses honestly rather than asking Razorpay for ₹0."""
    executor = RetryExecutor()
    called = False

    def fake_create(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"id": "plink_never", "short_url": "https://rzp.io/i/never"}

    monkeypatch.setattr(executor._client.payment_link, "create", fake_create)

    case = _case()
    case.amount_recovered = case.amount_at_risk

    result = await executor.execute_case_action(
        case=case,
        action_type="retry_now",
        target_rail=None,
        idempotency_key="chase_invoice_overdue_inv_san_1_2",
        notify_customer=False,
    )

    assert result["success"] is False
    assert called is False, "Razorpay was asked for a zero-amount link"
