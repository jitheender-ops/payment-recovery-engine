"""
The injected requests Session must not weaken the SDK's TLS verification.

An earlier review flagged that `_TimeoutSession` might bypass razorpay-python's
bundled ca-bundle.crt. It does not, and this pins the reason: the SDK passes
`verify=self.cert_path` as a per-CALL keyword (razorpay/client.py), not as a
session attribute, and `_TimeoutSession.request` forwards **kwargs untouched. A
future refactor that starts stripping or overriding kwargs would silently move
the client onto certifi's trust store instead, which is the kind of change
nobody notices until a pinning requirement matters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import razorpay

from src.executor.retry_executor import _TimeoutSession


def _captured(session: _TimeoutSession, monkeypatch: Any) -> dict[str, Any]:
    """Intercept at requests.Session.request — the floor every verb funnels into."""
    seen: dict[str, Any] = {}

    def fake_request(self: Any, *args: Any, **kwargs: Any) -> Any:
        seen["args"] = args
        seen["kwargs"] = kwargs
        raise RuntimeError("stop here — the call itself is not under test")

    monkeypatch.setattr("requests.Session.request", fake_request)
    return seen


def test_the_timeout_default_is_applied(monkeypatch: Any) -> None:
    session = _TimeoutSession(7.5)
    seen = _captured(session, monkeypatch)
    try:
        session.get("https://api.razorpay.com/v1/ping")
    except RuntimeError:
        pass
    assert seen["kwargs"]["timeout"] == 7.5


def test_an_explicit_timeout_still_wins(monkeypatch: Any) -> None:
    """setdefault, not assignment — a caller that knows better keeps its value."""
    session = _TimeoutSession(7.5)
    seen = _captured(session, monkeypatch)
    try:
        session.get("https://api.razorpay.com/v1/ping", timeout=1.0)
    except RuntimeError:
        pass
    assert seen["kwargs"]["timeout"] == 1.0


def test_the_sdk_ca_bundle_survives_the_injected_session(monkeypatch: Any) -> None:
    """The actual claim: verify= reaches requests with the SDK's own bundle."""
    session = _TimeoutSession(10.0)
    client = razorpay.Client(session=session, auth=("key", "secret"))
    seen = _captured(session, monkeypatch)
    try:
        client.payment.fetch("pay_nonexistent")
    except Exception:
        pass

    verify = seen["kwargs"].get("verify")
    assert verify, "the injected session dropped TLS verification entirely"
    assert verify == client.cert_path
    assert Path(verify).name == "ca-bundle.crt"
    assert Path(verify).exists(), "the SDK's bundled CA file is missing"
    # And the timeout is still there — neither feature cancels the other.
    assert seen["kwargs"]["timeout"] == 10.0
