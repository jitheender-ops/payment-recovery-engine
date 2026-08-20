"""Tests for the policy agent (LLM mocked)."""

from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

import pytest

from src.agent.actions import FailureContext, RetryAction
from src.agent.xgboost_baseline import XGBoostBaseline


now = datetime.now(timezone.utc)


def _make_context(**overrides) -> FailureContext:
    defaults = dict(
        payment_id="pay_test", failure_class="network_error",
        error_code="GATEWAY_ERROR", amount=50000, method="card",
        bank="HDFC", failed_at=now, current_time=now,
        hour_of_day=14, day_of_week=2, is_retryable=True,
        retry_count_24h=0, nudge_count_24h=0, previous_retry_outcomes=[],
    )
    defaults.update(overrides)
    return FailureContext(**defaults)


def test_xgboost_heuristic_hard_decline():
    baseline = XGBoostBaseline()
    ctx = _make_context(failure_class="hard_decline", is_retryable=False)
    action = baseline.predict(ctx)
    assert action.action == "abandon"


def test_xgboost_heuristic_network_error():
    baseline = XGBoostBaseline()
    ctx = _make_context(failure_class="network_error")
    action = baseline.predict(ctx)
    assert action.action == "retry_now"


def test_xgboost_heuristic_3ds_dropoff():
    baseline = XGBoostBaseline()
    ctx = _make_context(failure_class="3ds_dropoff")
    action = baseline.predict(ctx)
    assert action.action == "switch_rail"
    assert action.rail == "upi"


def test_xgboost_heuristic_insufficient_funds():
    baseline = XGBoostBaseline()
    ctx = _make_context(failure_class="insufficient_funds")
    action = baseline.predict(ctx)
    assert action.action == "nudge_customer"


def test_action_schema_validation():
    baseline = XGBoostBaseline()
    for fc in ["network_error", "hard_decline", "bank_downtime", "3ds_dropoff",
               "insufficient_funds", "upi_collect_timeout", "issuer_decline", "unknown"]:
        ctx = _make_context(failure_class=fc)
        action = baseline.predict(ctx)
        assert isinstance(action, RetryAction)
        assert action.action in ["retry_now", "retry_at", "switch_rail", "nudge_customer", "abandon"]
        assert len(action.reason) >= 5


def test_xgboost_bank_downtime_has_delay():
    baseline = XGBoostBaseline()
    ctx = _make_context(failure_class="bank_downtime")
    action = baseline.predict(ctx)
    assert action.action == "retry_at"
    assert action.retry_at is not None
    assert action.retry_at > now
