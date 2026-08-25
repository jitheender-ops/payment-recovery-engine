"""Tests for the guardrail gate — each rule tested independently."""

from datetime import UTC, datetime, timedelta
from typing import Any

from src.agent.actions import FailureContext, RetryAction
from src.guardrail.gate import GuardrailGate
from src.guardrail.rules import GuardrailRules

rules = GuardrailRules()
gate = GuardrailGate()

now = datetime.now(UTC)


def _make_context(**overrides: Any) -> FailureContext:
    defaults: dict[str, Any] = dict(
        payment_id="pay_test", failure_class="network_error",
        error_code="GATEWAY_ERROR", amount=50000, method="card",
        failed_at=now, current_time=now, hour_of_day=14, day_of_week=2,
        is_retryable=True, retry_count_24h=0, nudge_count_24h=0,
    )
    defaults.update(overrides)
    return FailureContext(**defaults)


def test_hard_decline_blocked() -> None:
    passed, reason = rules.check_hard_decline_blocklist("hard_decline")
    assert passed is False
    assert reason is not None, "a blocked action must carry an audit reason"
    assert "blocklist" in reason.lower()


def test_fraud_block_blocked() -> None:
    passed, _ = rules.check_hard_decline_blocklist("fraud_block")
    assert passed is False


def test_retryable_class_allowed() -> None:
    passed, _ = rules.check_hard_decline_blocklist("bank_downtime")
    assert passed is True


def test_max_retries_per_payment_exceeded() -> None:
    passed, _ = rules.check_max_retries_per_payment("pay_1", 3)
    assert passed is False


def test_max_retries_per_payment_within_limit() -> None:
    passed, _ = rules.check_max_retries_per_payment("pay_1", 1)
    assert passed is True


def test_max_retries_per_customer_exceeded() -> None:
    passed, _ = rules.check_max_retries_per_customer(5)
    assert passed is False


def test_amount_ceiling_exceeded() -> None:
    passed, _ = rules.check_amount_ceiling(6_000_000)  # ₹60,000
    assert passed is False


def test_amount_ceiling_within_limit() -> None:
    passed, _ = rules.check_amount_ceiling(1_000_000)  # ₹10,000
    assert passed is True


def test_consent_window_expired() -> None:
    old = now - timedelta(hours=80)
    passed, _ = rules.check_consent_window(old, now)
    assert passed is False


def test_consent_window_within_limit() -> None:
    recent = now - timedelta(hours=24)
    passed, _ = rules.check_consent_window(recent, now)
    assert passed is True


def test_nudge_rate_limit_exceeded() -> None:
    passed, _ = rules.check_customer_nudge_rate_limit(2)
    assert passed is False


def test_time_blackout_rejected() -> None:
    passed, _ = rules.check_time_of_day_blackout(2)  # 2 AM
    assert passed is False


def test_time_blackout_allowed() -> None:
    passed, _ = rules.check_time_of_day_blackout(10)  # 10 AM
    assert passed is True


def test_idempotency_key_required() -> None:
    passed, _ = rules.check_idempotency_key(None)
    assert passed is False

    passed, _ = rules.check_idempotency_key("")
    assert passed is False


def test_abandon_always_passes() -> None:
    action = RetryAction(action="abandon", reason="Test abandon")
    context = _make_context(failure_class="hard_decline")
    result = gate.validate(action, context, "key_1", 10)
    assert result.passed is True


def test_all_rules_checked_not_short_circuit() -> None:
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


# ── Fail-closed on the unexpected ────────────────────────────────────────


async def test_schema_validation_fails_closed_on_unexpected_errors(
    sample_retry_action, monkeypatch
) -> None:
    """
    If validation itself explodes (a pydantic bug, an OOM mid-import), the
    answer must be REJECT — an unvalidated action reaching execution is the
    one failure this layer exists to prevent.
    """
    from src.guardrail.schemas import validate_action_schema

    monkeypatch.setattr(
        "src.guardrail.schemas.RetryAction",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    is_valid, action, err = validate_action_schema(sample_retry_action.model_dump())
    assert is_valid is False
    assert action is None
    assert err is not None


def test_valid_non_switch_actions_pass_the_semantic_checks() -> None:
    """
    The switch_rail-requires-rail check must not leak into other actions:
    'and', not 'or' — otherwise every rail-less retry_now gets vetoed here
    and nothing downstream ever sees why.
    """
    from src.agent.actions import RetryAction
    from src.guardrail.schemas import validate_action_schema

    ok, parsed, err = validate_action_schema(
        RetryAction(action="retry_now", reason="transient").model_dump()
    )
    assert ok is True and err is None and parsed.action == "retry_now"

    ok, _, _ = validate_action_schema(
        RetryAction(action="nudge_customer", reason="needs customer").model_dump()
    )
    assert ok is True


def test_switch_rail_without_rail_is_rejected_but_others_are_not() -> None:
    from src.guardrail.schemas import validate_action_schema

    bad = validate_action_schema(
        {"action": "switch_rail", "reason": "no rail given"}
    )
    assert bad[0] is False
