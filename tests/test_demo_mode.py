"""
Demo mode: the fake gateway, and the guards that keep it where it belongs.

Most of the weight here is on the two safety properties, not on the fake
itself. A fake gateway that always succeeds is indistinguishable from a
healthy business in every downstream signal — recovered cases, attributed
money, a moving dashboard — so the only thing standing between it and a
false report of a working product is that it cannot run outside
development, and cannot widen the pay-path redirect allowlist.

The second is the one to watch. /pay ends in a redirect to wherever the
customer hands over money, and demo mode adds an exception to the allowlist
that guards it. These tests pin that the exception is exactly as narrow as
it claims: right mode, right origin, right path prefix, and nothing else.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.config import Settings, get_settings

# ── The guard that keeps a fake gateway out of a real deployment ─────────


@pytest.mark.parametrize("env", ["staging", "production"])
def test_demo_mode_is_refused_outside_development(env: str) -> None:
    with pytest.raises(ValueError, match="DEMO_MODE"):
        Settings(_env_file=None, app_env=env, demo_mode=True)  # type: ignore[call-arg]


def test_demo_mode_is_allowed_in_development() -> None:
    settings = Settings(_env_file=None, app_env="development", demo_mode=True)  # type: ignore[call-arg]
    assert settings.demo_mode is True


def test_demo_mode_is_off_by_default() -> None:
    """Nothing about a normal boot may depend on remembering to unset it."""
    assert Settings(_env_file=None).demo_mode is False  # type: ignore[call-arg]


# ── The pay-path redirect allowlist ──────────────────────────────────────
#
# /recover/{token}/pay 303s to the payment object. A poisoned short_url that
# could steer that redirect is a phishing page writing itself, on the one
# page whose whole job is asking someone for money.


@pytest.fixture
def _demo_settings(monkeypatch: Any) -> Any:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_demo_checkout_is_a_valid_redirect_target_in_demo_mode(
    _demo_settings: Any,
) -> None:
    from src.customer.routes import _is_demo_redirect_target

    assert _is_demo_redirect_target("http://127.0.0.1:8000/demo/checkout/plink_demo_1")


@pytest.mark.parametrize(
    "url",
    [
        # A different host on the same path — the whole attack.
        "http://evil.example/demo/checkout/plink_demo_1",
        # The right host, a path that is not the stub checkout.
        "http://127.0.0.1:8000/recover/anything",
        "http://127.0.0.1:8000/",
        # The right host and path, the wrong PORT — a different service on
        # the same machine is still a different origin.
        "http://127.0.0.1:9999/demo/checkout/plink_demo_1",
        # Scheme mismatch against the configured base.
        "https://127.0.0.1:8000/demo/checkout/plink_demo_1",
        # Path-prefix confusion: the check must not match a lookalike host.
        "http://127.0.0.1:8000.evil.example/demo/checkout/x",
        "",
        "not a url at all",
    ],
)
def test_nothing_else_is_a_valid_demo_redirect_target(
    _demo_settings: Any, url: str
) -> None:
    from src.customer.routes import _is_demo_redirect_target

    assert not _is_demo_redirect_target(url)


def test_the_demo_exception_does_not_exist_when_demo_mode_is_off(
    monkeypatch: Any,
) -> None:
    """The load-bearing clause: with demo mode off this is dead code."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    get_settings.cache_clear()
    try:
        from src.customer.routes import _is_demo_redirect_target

        assert not _is_demo_redirect_target(
            "http://127.0.0.1:8000/demo/checkout/plink_demo_1"
        )
    finally:
        get_settings.cache_clear()


def test_razorpay_remains_the_only_real_redirect_target(_demo_settings: Any) -> None:
    """Demo mode must not loosen the production allowlist by one inch."""
    from src.customer.routes import _is_payment_redirect_target

    assert _is_payment_redirect_target("https://rzp.io/i/abc123")
    assert _is_payment_redirect_target("https://api.razorpay.com/v1/x")
    assert not _is_payment_redirect_target("https://razorpay.com.evil.in/x")
    assert not _is_payment_redirect_target("https://evilrazorpay.com/x")
    assert not _is_payment_redirect_target("http://rzp.io/i/abc123")  # not https


# ── The fake itself ──────────────────────────────────────────────────────


def test_the_fake_returns_the_documented_payment_link_shape(
    _demo_settings: Any,
) -> None:
    """
    id / short_url / status, as Razorpay's Payment Link object carries them.
    The executor reads exactly `id` and `short_url`, so a fake missing either
    would fail somewhere far from here.
    """
    from src.demo import FakeRazorpayClient

    link = FakeRazorpayClient().payment_link.create(
        {"amount": 249900, "currency": "INR", "notes": {"retry_idempotency_key": "k1"}}
    )
    assert link["id"].startswith("plink_")
    assert link["short_url"].startswith("http://127.0.0.1:8000/demo/checkout/")
    assert link["status"] == "created"
    assert link["notes"]["retry_idempotency_key"] == "k1"


def test_the_executor_uses_the_fake_in_demo_mode(_demo_settings: Any) -> None:
    from src.demo import FakeRazorpayClient
    from src.executor.retry_executor import RetryExecutor

    assert isinstance(RetryExecutor()._client, FakeRazorpayClient)


def test_a_capture_carries_the_breadcrumb_that_attributes_it(
    _demo_settings: Any,
) -> None:
    """
    The idempotency key in `notes` is the entire attribution mechanism: a
    paid link mints a NEW payment id, so this breadcrumb is the only thing
    joining the money back to the attempt that earned it.
    """
    from src.demo import captured_payload

    entity = captured_payload(50_000, idempotency_key="retry_abc")[
        "payload"]["payment"]["entity"]
    assert entity["status"] == "captured"
    assert entity["amount"] == 50_000
    assert entity["notes"]["retry_idempotency_key"] == "retry_abc"
    assert "order_id" not in entity


def test_a_self_paid_capture_carries_no_attribution_breadcrumb(
    _demo_settings: Any,
) -> None:
    """The control group: real revenue, explicitly NOT credited to us."""
    from src.demo import captured_payload

    entity = captured_payload(50_000, order_id="order_9")[
        "payload"]["payment"]["entity"]
    assert entity["order_id"] == "order_9"
    assert entity["notes"] == {}


def test_the_demo_signature_is_one_the_engine_actually_accepts() -> None:
    """
    The stub posts a genuinely signed webhook at the real endpoint. If these
    two ever disagreed the demo would look broken in a way that had nothing
    to do with the engine.
    """
    from src.demo import sign
    from src.ingestion.signature import verify_webhook_signature

    body = b'{"event":"payment.captured"}'
    assert verify_webhook_signature(body, sign(body, "s3cret"), "s3cret")
    assert not verify_webhook_signature(body, sign(body, "s3cret"), "different")
