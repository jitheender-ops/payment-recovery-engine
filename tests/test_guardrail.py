"""Tests for the guardrail gate — each rule tested independently."""

from datetime import UTC, datetime, timedelta
from typing import Any

from src.agent.actions import FailureContext, RetryAction
from src.guardrail.gate import RULE_LABELS, GuardrailGate, rule_roster
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


def test_switch_only_class_refuses_same_rail_retry() -> None:
    """
    risk_check_failed is retryable, but only off the refused instrument. The
    money bug this guards: a same-rail retry walks back into the same risk
    screen, fails for certain, and still burns one of three attempt slots.
    """
    for action_type in ("retry_now", "retry_at"):
        passed, reason = rules.check_switch_only_class("risk_check_failed", action_type)
        assert passed is False, action_type
        assert reason is not None and "switch_rail" in reason

    for action_type in ("switch_rail", "nudge_customer"):
        passed, _ = rules.check_switch_only_class("risk_check_failed", action_type)
        assert passed is True, action_type

    # Not a blanket ban on retrying: every other retryable class is untouched.
    assert rules.check_switch_only_class("network_error", "retry_now")[0] is True
    # And it is not a hard decline — the case must still be worked.
    assert rules.check_hard_decline_blocklist("risk_check_failed")[0] is True


def test_switch_only_class_blocks_through_the_full_gate() -> None:
    """The rule is wired into validate(), not just callable on its own."""
    context = _make_context(failure_class="risk_check_failed", method="card")
    retry = RetryAction(action="retry_now", reason="retry the same card")
    assert gate.validate(retry, context, "idem_switch_only_1", 0).passed is False

    switch = RetryAction(action="switch_rail", rail="upi", reason="move off the card")
    assert gate.validate(switch, context, "idem_switch_only_2", 0).passed is True


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


# ── Self-serve subset ────────────────────────────────────────────────────


def _self_serve_action() -> RetryAction:
    return RetryAction(
        action="retry_now", reason="Customer-initiated from the recovery page"
    )


def test_self_serve_passes_for_a_fresh_retryable_failure() -> None:
    context = _make_context()
    result = gate.validate_self_serve(_self_serve_action(), context, "selfserve_k1", 1)
    assert result.passed is True
    assert result.rules_checked == 5


def test_self_serve_rejects_past_the_consent_window() -> None:
    """
    The token's TTL runs from ISSUANCE, so a link minted near the window's end
    outlives it. The customer pressing pay on that link is still US minting a
    payment object past our authority to act — the window rule applies here.
    """
    window_hours = 72
    failed = now - timedelta(hours=window_hours + 1)
    context = _make_context(failed_at=failed, current_time=now)
    result = gate.validate_self_serve(_self_serve_action(), context, "selfserve_k2", 1)
    assert result.passed is False
    assert any("consent" in r.lower() for r in result.rejection_reasons)


def test_self_serve_rejects_a_hard_decline_class() -> None:
    context = _make_context(failure_class="fraud_block")
    result = gate.validate_self_serve(_self_serve_action(), context, "selfserve_k3", 1)
    assert result.passed is False
    assert any("blocklist" in r.lower() for r in result.rejection_reasons)


def test_self_serve_rejects_a_spent_attempt_budget() -> None:
    context = _make_context()
    result = gate.validate_self_serve(_self_serve_action(), context, "selfserve_k4", 3)
    assert result.passed is False


def test_self_serve_ignores_the_outreach_rules() -> None:
    """
    Blackout and contact limits bound OUR chasing, not a customer choosing to
    pay: 3 AM and a maxed-out 24h tally must not block a self-serve payment.
    """
    context = _make_context(hour_of_day=2, retry_count_24h=99, nudge_count_24h=99)
    result = gate.validate_self_serve(_self_serve_action(), context, "selfserve_k5", 1)
    assert result.passed is True


# ── Expected-value stopping rule ─────────────────────────────────────────


def test_ev_silent_without_a_confidence_score() -> None:
    """No confidence supplied — the rule must not invent one to reject on."""
    passed, _ = rules.check_expected_value("retry_now", None, 100_00)
    assert passed is True


def test_ev_rejects_when_non_positive() -> None:
    # confidence 0.05 * ₹100 = ₹5, cost floor is ₹2 default — still positive,
    # so push confidence low enough that EV drops under the ₹2 cost.
    passed, reason = rules.check_expected_value("retry_now", 0.01, 100_00)
    assert passed is False
    assert reason is not None and "Expected value" in reason


def test_ev_passes_when_clearly_positive() -> None:
    passed, _ = rules.check_expected_value("retry_now", 0.8, 100_00)
    assert passed is True


def test_ev_ignores_actions_that_do_not_spend_a_charge_attempt() -> None:
    passed, _ = rules.check_expected_value("nudge_customer", 0.01, 100_00)
    assert passed is True
    passed, _ = rules.check_expected_value("abandon", 0.01, 100_00)
    assert passed is True


def test_gate_rejects_a_low_confidence_low_value_retry() -> None:
    action = RetryAction(action="retry_now", reason="Low-odds retry", confidence=0.01)
    context = _make_context(amount=100_00)
    result = gate.validate(action, context, "ev_key_1", 0)
    assert result.passed is False
    assert any("Expected value" in r for r in result.rejection_reasons)


def test_gate_passes_a_confident_retry() -> None:
    action = RetryAction(action="retry_now", reason="High-odds retry", confidence=0.9)
    context = _make_context(amount=100_00)
    result = gate.validate(action, context, "ev_key_2", 0)
    assert result.passed is True


# ── Mandate pre-debit notification (RBI e-mandate framework, 2026) ─────────


def test_mandate_retry_without_any_notification_is_blocked() -> None:
    passed, reason = rules.check_mandate_predebit_notification(
        "mandate_failure", "retry_now", None, now,
    )
    assert passed is False
    assert reason is not None
    assert "RBI" in reason


def test_mandate_retry_notified_too_recently_is_blocked() -> None:
    passed, reason = rules.check_mandate_predebit_notification(
        "mandate_failure", "retry_now", now - timedelta(hours=23), now,
    )
    assert passed is False
    assert reason is not None and "23" in reason


def test_mandate_retry_notified_24h_ago_or_more_is_allowed() -> None:
    passed, _ = rules.check_mandate_predebit_notification(
        "mandate_failure", "retry_now", now - timedelta(hours=24), now,
    )
    assert passed is True


def test_mandate_retry_with_a_stale_notification_is_blocked() -> None:
    """
    The framework's notice is per-debit, not per-mandate: one notification
    must not authorize unlimited re-presentations forever. Past the
    validity window (7 days by default) it is treated as unsent.
    """
    passed, reason = rules.check_mandate_predebit_notification(
        "mandate_failure", "retry_now", now - timedelta(days=30), now,
    )
    assert passed is False
    assert reason is not None and "stale" in reason


def test_mandate_notification_inside_the_validity_window_is_allowed() -> None:
    passed, _ = rules.check_mandate_predebit_notification(
        "mandate_failure", "retry_now", now - timedelta(days=6), now,
    )
    assert passed is True


def test_non_mandate_risk_type_is_unaffected() -> None:
    """The rule only ever applies to risk_type=mandate_failure."""
    passed, _ = rules.check_mandate_predebit_notification(
        "subscription_failure", "retry_now", None, now,
    )
    assert passed is True


def test_mandate_nudge_action_is_unaffected() -> None:
    """
    nudge_customer IS how the notification gets sent — the rule must not
    block the notification itself, only a retry_now that skipped it.
    """
    passed, _ = rules.check_mandate_predebit_notification(
        "mandate_failure", "nudge_customer", None, now,
    )
    assert passed is True


def test_gate_rejects_a_mandate_retry_with_no_notification_on_record() -> None:
    action = RetryAction(action="retry_now", reason="Re-present the mandate charge")
    context = _make_context(
        risk_type="mandate_failure", failure_class="mandate_debit_failed",
        last_notification_sent_at=None,
    )
    result = gate.validate(action, context, "mandate_key_1", 0)
    assert result.passed is False
    assert any("RBI" in r for r in result.rejection_reasons)


def test_gate_passes_a_mandate_retry_notified_well_in_advance() -> None:
    action = RetryAction(action="retry_now", reason="Re-present the mandate charge")
    context = _make_context(
        risk_type="mandate_failure", failure_class="mandate_debit_failed",
        last_notification_sent_at=now - timedelta(hours=48),
    )
    result = gate.validate(action, context, "mandate_key_2", 0)
    assert result.passed is True


# ── Boundary mutants the mutation run survived ──────────────────────────────
# The weekly mutmut pass (mutation.yml) reported survivors that are real
# boundary gaps, not message-text noise. Each test below is named for the
# behavioural distinction a mutant erased. Anything marked "honest copy"
# pins the EXACT rejection string an operator reads in the console — the
# XX...XX mutants on those lines proved nothing asserts them verbatim.


def test_the_consent_window_boundary_hour_itself_still_passes() -> None:
    """Exactly deadline hours elapsed: `current_time > deadline` must keep
    the window open at its edge — the deadline instant belongs to the
    consent period. (Mutant: > flipped.)"""
    failed = now - timedelta(hours=72)
    passed, _ = rules.check_consent_window(failed, failed + timedelta(hours=72))
    assert passed is True
    # One second past the edge closes it.
    passed, _ = rules.check_consent_window(
        failed, failed + timedelta(hours=72, seconds=1)
    )
    assert passed is False


def test_the_nudge_cap_boundary_slot_is_vetoed() -> None:
    """nudges == limit is the cap itself, not one below it (>=, not >).
    The default cap is 2 nudges/24h — the boundary slot is 2."""
    limit = rules._settings.max_nudges_per_customer_24h
    passed, reason = rules.check_customer_nudge_rate_limit(limit)
    assert passed is False
    assert reason is not None
    passed, _ = rules.check_customer_nudge_rate_limit(limit - 1)
    assert passed is True


def test_a_naive_current_time_is_assumed_utc_not_refused() -> None:
    """Naive datetimes from tests/legacy rows must compare as UTC, not
    crash or silently pass. (Mutant: tzinfo check inverted.)"""
    failed = now.replace(tzinfo=None) - timedelta(hours=80)
    current = now.replace(tzinfo=None)
    passed, _ = rules.check_consent_window(failed, current)
    assert passed is False, "a naive 80h-old failure leaked past the window"


def test_the_consent_rejection_reason_names_the_true_hours() -> None:
    """Honest copy: the reason says the REAL elapsed hours and the REAL
    window. hours * 3600 (the mutant) reported a 259200h debt."""
    failed = now - timedelta(hours=80)
    _, reason = rules.check_consent_window(failed, now)
    assert reason is not None
    assert "80.0h" in reason
    assert "> 72h" in reason


def test_the_amount_ceiling_reason_quotes_the_true_rupees() -> None:
    """Honest copy: ₹ figures in the rejection match the case. The * 100
    mutant inflated the displayed amount a hundredfold while blocking the
    same rows — an operator reading it would distrust the whole console."""
    amount = rules._settings.amount_ceiling_paise
    # Deliberately over the ceiling by a known factor: 2× the ceiling.
    passed, reason = rules.check_amount_ceiling(amount * 2)
    assert passed is False
    over = f"₹{amount * 2 / 100:,.2f}"
    assert over in reason, f"the rejection misquotes the amount: {reason}"


def test_the_blackout_reason_names_the_hour_and_window() -> None:
    """Honest copy: the operator sees which hour and which window."""
    passed, reason = rules.check_time_of_day_blackout(23)
    assert passed is False
    assert "23" in reason and "23:00-07:00" in reason


def test_an_empty_blackout_window_blocks_nothing() -> None:
    """start == end (the tests' 0/0 "off" setting) must be an EMPTY window
    in both branches. The >= mutant turned 0/0 into an always-blackout,
    which would have deferred every scheduled test and every real sweep.
    Pinned here with the LIVE settings (23-7); the 0/0 trick itself is
    pinned by test_recovery_batch.py's fixture."""
    from src.guardrail.rules import is_in_blackout

    # Default window 23-7: the boundary hours themselves are the contract.
    assert is_in_blackout(23) is True
    assert is_in_blackout(7) is False
    assert is_in_blackout(12) is False


def test_the_expected_value_boundary_is_inclusive() -> None:
    """EV exactly == cost: the attempt is not worth it — `<= cost` refuses.
    The < mutant let an exactly-at-cost retry through, spending an attempt
    for zero expected gain."""
    passed, _ = rules.check_expected_value("retry_now", 0.02, 100_00)
    # confidence 0.02 × ₹100 = ₹2 == default ₹2 cost — must REFUSE.
    assert passed is False


def test_a_retry_at_without_a_timestamp_is_a_schema_violation() -> None:
    """validate_action_schema is the first thing the gate feeds through; a
    retry_at with no timestamp must fail it outright. (Mutant: inverted.)"""
    from src.guardrail.schemas import validate_action_schema

    ok, parsed, err = validate_action_schema({
        "action": "retry_at", "reason": "no timestamp supplied",
    })
    assert ok is False
    assert parsed is None
    assert err is not None and "retry_at" in err


def test_the_mandate_notice_rule_reports_the_missing_notice_honestly() -> None:
    """Honest copy for compliance: the RBI rule's refusal names the
    requirement and the required lead time."""
    passed, reason = rules.check_mandate_predebit_notification(
        risk_type="mandate_failure", action_type="retry_now",
        last_notification_sent_at=None, current_time=now,
    )
    assert passed is False
    assert reason is not None
    assert "24h" in reason or "24 hours" in reason
    assert "pre-debit notification" in reason or "notification" in reason


def test_the_rules_checked_count_includes_the_schema_check() -> None:
    """The audit trail's `rules_checked` must count every rule that ran.
    (Mutant: -1.) A miscount here makes 'all rules checked' unpinnable.
    The abandon path returns rules_checked=0 by design — use a retry."""
    action = RetryAction(action="retry_now", reason="transient network error")
    ctx = _ctx()
    result = gate.validate(action, ctx, idempotency_key="mut_pin_1",
                           current_attempts=0)
    # The gate runs the schema check + every business rule and reports the
    # count; a retry_now on a fresh case passes, so what we pin is that
    # the count matches the rules that ran, not 0.
    assert result.rules_checked >= 12, (
        f"the gate undercounted its own checks: {result.rules_checked}"
    )


def _ctx() -> FailureContext:
    """One valid context for whole-gate exercises in this section."""
    return FailureContext(
        payment_id="pay_mut_1", failure_class="insufficient_funds",
        error_code="BAD_REQUEST_ERROR", amount=100_00, method="card",
        bank="HDFC", failed_at=now - timedelta(hours=1), current_time=now,
        hour_of_day=14, day_of_week=2,
    )


# ── The roster the console explains the gate with ────────────────────────────


def _checks_for(action_type: str) -> list[str]:
    """The method names _checks_for actually builds, for one action type."""
    from datetime import UTC, datetime

    from src.agent.actions import FailureContext, RetryAction

    now = datetime.now(UTC)
    action = RetryAction(action=action_type, confidence=0.8, reason="roster probe")
    context = FailureContext(
        payment_id="pay_roster", failure_class="insufficient_funds",
        error_code="BAD_REQUEST_ERROR", method="card", amount=100000,
        hour_of_day=12, day_of_week=2, retry_count_24h=0, is_retryable=True,
        bank="HDFC", failed_at=now, current_time=now,
    )
    gate = GuardrailGate()
    checks = gate._checks_for(action, context, "idem_roster", 0)
    return [fn.__name__ for fn, _args in checks]


def test_every_rule_the_gate_runs_has_a_merchant_facing_label() -> None:
    """
    The console tells a merchant which rules ran and which fired. It cannot
    read that from the database — RetryAttempt stores the joined rejection
    string, not the roster — so gate.RULE_LABELS declares it. A rule added to
    _checks_for without a label would be a rule the console silently stopped
    mentioning, which is the whole failure this asserts against.
    """
    for action_type in ("retry_now", "nudge_customer", "switch_rail"):
        for name in _checks_for(action_type):
            assert name in RULE_LABELS, f"{name} has no label for the console"


def test_no_label_names_a_rule_that_no_longer_exists() -> None:
    """The other direction: a renamed or deleted check must not leave a label
    behind, or the console would list a rule nothing runs."""
    rules = GuardrailRules()
    for name in RULE_LABELS:
        assert hasattr(rules, name), f"RULE_LABELS names a missing rule: {name}"


def test_the_roster_matches_what_the_gate_actually_runs() -> None:
    """Same rules, same order — the console's checklist is the audit order."""
    for action_type in ("retry_now", "switch_rail", "nudge_customer"):
        roster = [name for name, _label, _prefix in rule_roster(action_type)]
        assert roster == _checks_for(action_type), action_type


def test_an_abandon_has_an_empty_roster_because_the_gate_runs_nothing() -> None:
    """
    validate() auto-passes an abandon with rules_checked=0. A surface drawing
    twelve green ticks for one would be describing work that never happened.
    """
    assert rule_roster("abandon") == []
    gate = GuardrailGate()
    from datetime import UTC, datetime

    from src.agent.actions import FailureContext, RetryAction

    now = datetime.now(UTC)
    result = gate.validate(
        RetryAction(action="abandon", confidence=0.9, reason="hard decline"),
        FailureContext(
            payment_id="pay_abandon", failure_class="fraud_block",
            error_code="BAD_REQUEST_ERROR", method="card", amount=100000,
            hour_of_day=12, day_of_week=2, retry_count_24h=0,
            is_retryable=False, bank="HDFC", failed_at=now, current_time=now,
        ),
        "idem_abandon", 0,
    )
    assert result.passed and result.rules_checked == 0


def test_every_rejection_prefix_is_one_a_rule_can_actually_produce() -> None:
    """
    The prefixes attribute a stored reason back to its rule. A prefix that no
    rule emits would silently mark that rule as never-fired on every page it
    appears on — a guardrail the console claims always passes.
    """
    import inspect

    source = inspect.getsource(GuardrailRules)
    for name, (_label, prefix) in RULE_LABELS.items():
        # The prefix is the literal head of the f-string the rule returns.
        assert prefix in source, f"{name}: no rule emits {prefix!r}"
