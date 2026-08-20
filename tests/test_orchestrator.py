"""Integration tests for the orchestrator (mocked externals)."""

from src.classifier.mapper import ClassifierMapper
from src.classifier.taxonomy import FailureClass


def test_classifier_pipeline():
    """Classifier correctly maps a known error code."""
    mapper = ClassifierMapper()
    fc, retryable = mapper.classify(
        "BAD_REQUEST_ERROR",
        error_description="Insufficient funds",
        error_source="customer",
        error_step="payment_authorization",
        error_reason="insufficient_funds",
    )
    assert fc == FailureClass.INSUFFICIENT_FUNDS
    assert retryable is True


def test_hard_decline_skips_agent():
    """Hard declines should be classified as non-retryable."""
    mapper = ClassifierMapper()
    fc, retryable = mapper.classify(
        "BAD_REQUEST_ERROR", error_reason="card_stolen"
    )
    assert fc == FailureClass.HARD_DECLINE
    assert retryable is False
    assert fc.is_hard_decline is True


def test_full_classify_to_guardrail():
    """End-to-end: classify → build action → validate guardrail."""
    from datetime import datetime, timezone
    from src.agent.actions import FailureContext, RetryAction
    from src.agent.xgboost_baseline import XGBoostBaseline
    from src.guardrail.gate import GuardrailGate

    mapper = ClassifierMapper()
    fc, retryable = mapper.classify("GATEWAY_ERROR", error_reason="bank_technical_error")
    assert fc == FailureClass.BANK_DOWNTIME

    now = datetime.now(timezone.utc)
    ctx = FailureContext(
        payment_id="pay_integ_001", failure_class=fc.value,
        error_code="GATEWAY_ERROR", amount=50000, method="netbanking",
        bank="PNB", failed_at=now, current_time=now,
        hour_of_day=14, day_of_week=2, is_retryable=retryable,
        retry_count_24h=0, nudge_count_24h=0, previous_retry_outcomes=[],
    )

    agent = XGBoostBaseline()
    action = agent.predict(ctx)
    assert isinstance(action, RetryAction)

    gate = GuardrailGate()
    result = gate.validate(action, ctx, "idem_key_001", 0)
    assert result.passed is True


def test_eval_harness_runs():
    """Smoke test: eval harness can generate scenarios and run policies."""
    from eval.scenario_generator import ScenarioGenerator
    from eval.simulator import BankResponseSimulator
    from eval.policies.no_retry import NoRetryPolicy
    from eval.runner import EvalRunner

    gen = ScenarioGenerator(seed=42)
    scenarios = gen.generate(100)
    assert len(scenarios) == 100

    sim = BankResponseSimulator(seed=42)
    runner = EvalRunner(n_scenarios=100, n_seeds=1, skip_llm=True)
    results = runner.run_policy("No Retry", NoRetryPolicy(), scenarios, sim)
    assert len(results) == 100
    assert results["recovered"].sum() == 0  # no-retry never recovers
