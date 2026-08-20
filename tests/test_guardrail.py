"""Tests for the guardrail gate — each rule tested independently."""

from datetime import datetime, timedelta, timezone

import pytest

from src.agent.actions import FailureContext, RetryAction
from src.guardrail.gate import GuardrailGate
from src.guardrail.rules import GuardrailRules

rules = GuardrailRules()
gate = GuardrailGate()

now = datetime.now(timezone.utc)


def _make_context(**overrides) -> FailureContext:
    defaults = dict(
        payment_id="pay_test", failure_class="network_error",
        error_code="GATEWAY_ERROR", amount=50000, method="card",
        failed_at=now, current_time=now, hour_of_day=14, day_of_week=2,
        is_retryable=True, retry_count_24h=0, nudge_count_24h=0,
    )
    defaults.update(overrides)
    return FailureContext(**defaults)


def test_hard_decline_blocked():
    passed, reason = rules.check_hard_decline_blocklist("hard_decline")
    assert passed is False
    assert "blocklist" in reason.lower()


def test_fraud_block_blocked():
    passed, _ = rules.check_hard_decline_blocklist("fraud_block")
    assert passed is False


def test_retryable_class_allowed():
    passed, _ = rules.check_hard_decline_blocklist("bank_downtime")
    assert passed is True


def test_max_retries_per_payment_exceeded():
    passed, _ = rules.check_max_retries_per_payment("pay_1", 3)
    assert passed is False


def test_max_retries_per_payment_within_limit():
    passed, _ = rules.check_max_retries_per_payment("pay_1", 1)
    assert passed is True


def test_max_retries_per_customer_exceeded():
    passed, _ = rules.check_max_retries_per_customer(5)
    assert passed is False


def test_amount_ceiling_exceeded():
    passed, _ = rules.check_amount_ceiling(6_000_000)  # ₹60,000
    assert passed is False


def test_amount_ceiling_within_limit():
    passed, _ = rules.check_amount_ceiling(1_000_000)  # ₹10,000
    assert passed is True


def test_consent_window_expired():
    old = now - timedelta(hours=80)
    passed, _ = rules.check_consent_window(old, now)
    assert passed is False


def test_consent_window_within_limit():
    recent = now - timedelta(hours=24)
    passed, _ = rules.check_consent_window(recent, now)
    assert passed is True


def test_nudge_rate_limit_exceeded():
    passed, _ = rules.check_customer_nudge_rate_limit(2)
    assert passed is False


def test_time_blackout_rejected():
    passed, _ = rules.check_time_of_day_blackout(2)  # 2 AM
    assert passed is False


def test_time_blackout_allowed():
    passed, _ = rules.check_time_of_day_blackout(10)  # 10 AM
    assert passed is True


def test_idempotency_key_required():
    passed, _ = rules.check_idempotency_key(None)
    assert passed is False

    passed, _ = rules.check_idempotency_key("")
    assert passed is False


def test_abandon_always_passes():
    action = RetryAction(action="abandon", reason="Test abandon")
    context = _make_context(failure_class="hard_decline")
    result = gate.validate(action, context, "key_1", 10)
    assert result.passed is True


def test_all_rules_checked_not_short_circuit():
    """Multiple violations should all be reported."""
    action = RetryAction(action="retry_now", reason="Test retry action")
    context = _make_context(
        failure_class="hard_decline",
        amount=6_000_000,
        hour_of_day=2,
        retry_count_24h=10,
    )
    result = gate.validate(action, context, "key_1", 5)
    assert result.passed is False
    assert result.rules_failed >= 3  # blocklist + amount + retries + blackout
