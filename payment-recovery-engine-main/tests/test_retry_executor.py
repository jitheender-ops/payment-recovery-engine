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
from src.executor.retry_executor import RetryExecutor, _TimeoutSession
from src.models import PaymentFailure


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
