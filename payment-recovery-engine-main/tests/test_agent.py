"""Tests for the policy agent (LLM mocked)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.agent.actions import FailureContext, RetryAction
from src.agent.xgboost_baseline import XGBoostBaseline

now = datetime.now(UTC)


def _make_context(**overrides: Any) -> FailureContext:
    defaults: dict[str, Any] = dict(
        payment_id="pay_test", failure_class="network_error",
        error_code="GATEWAY_ERROR", amount=50000, method="card",
        bank="HDFC", failed_at=now, current_time=now,
        hour_of_day=14, day_of_week=2, is_retryable=True,
        retry_count_24h=0, nudge_count_24h=0, previous_retry_outcomes=[],
    )
    defaults.update(overrides)
    return FailureContext(**defaults)


def test_xgboost_heuristic_hard_decline() -> None:
    baseline = XGBoostBaseline(model_path="")
    ctx = _make_context(failure_class="hard_decline", is_retryable=False)
    action = baseline.predict(ctx)
    assert action.action == "abandon"


def test_xgboost_heuristic_network_error() -> None:
    baseline = XGBoostBaseline(model_path="")
    ctx = _make_context(failure_class="network_error")
    action = baseline.predict(ctx)
    assert action.action == "retry_now"


def test_xgboost_heuristic_3ds_dropoff() -> None:
    baseline = XGBoostBaseline(model_path="")
    ctx = _make_context(failure_class="3ds_dropoff")
    action = baseline.predict(ctx)
    assert action.action == "switch_rail"
    assert action.rail == "upi"


def test_xgboost_heuristic_insufficient_funds() -> None:
    baseline = XGBoostBaseline(model_path="")
    ctx = _make_context(failure_class="insufficient_funds")
    action = baseline.predict(ctx)
    assert action.action == "nudge_customer"


def test_action_schema_validation() -> None:
    baseline = XGBoostBaseline(model_path="")
    for fc in ["network_error", "hard_decline", "bank_downtime", "3ds_dropoff",
               "insufficient_funds", "upi_collect_timeout", "issuer_decline", "unknown"]:
        ctx = _make_context(failure_class=fc)
        action = baseline.predict(ctx)
        assert isinstance(action, RetryAction)
        assert action.action in [
            "retry_now",
            "retry_at",
            "switch_rail",
            "nudge_customer",
            "abandon",
        ]
        assert len(action.reason) >= 5


def test_xgboost_bank_downtime_has_delay() -> None:
    baseline = XGBoostBaseline(model_path="")
    ctx = _make_context(failure_class="bank_downtime")
    action = baseline.predict(ctx)
    assert action.action == "retry_at"
    assert action.retry_at is not None
    assert action.retry_at > now


# ── The model actually gets used ─────────────────────────────────────────
# Regression guard for the bug that made "the XGBoost baseline" a fiction: every
# production call site constructed XGBoostBaseline() with no argument, and the
# constructor only loaded a model when handed an explicit path. A trained model
# could sit on disk forever and never influence a single decision.


def test_no_argument_means_load_the_configured_model(tmp_path: Any) -> None:
    from src.config import get_settings

    baseline = XGBoostBaseline()
    expected = Path(get_settings().xgboost_model_path).exists()
    assert baseline.is_trained is expected, (
        "XGBoostBaseline() ignored the configured model path — this is exactly "
        "the defect that made the README's XGBoost row a rule heuristic"
    )


def test_empty_path_forces_the_rule_heuristic() -> None:
    """"" is how a caller opts out; None means 'use settings'."""
    assert XGBoostBaseline(model_path="").is_trained is False


def test_a_missing_model_degrades_instead_of_crashing(tmp_path: Any) -> None:
    baseline = XGBoostBaseline(model_path=str(tmp_path / "not_here.joblib"))
    assert baseline.is_trained is False
    action = baseline.predict(_make_context(failure_class="network_error"))
    assert action.action in ("retry_now", "retry_at", "switch_rail", "nudge_customer", "abandon")
