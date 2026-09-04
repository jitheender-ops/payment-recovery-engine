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


def test_a_swapped_model_file_is_refused_when_a_pin_is_set(tmp_path: Any, monkeypatch: Any) -> None:
    """
    joblib.load is pickle: it executes whatever the file says. XGBOOST_MODEL_SHA256
    pins the trusted bytes; a model that arrives from outside the build (a
    registry, a bucket, a re-upload) with different bytes must be refused, not
    executed.
    """
    import hashlib

    path = tmp_path / "model.joblib"
    path.write_bytes(b"not a real model, bytes are bytes")
    # Any load that gets past the pin would raise on these bytes anyway; the
    # point is that it never gets there.
    monkeypatch.setattr(
        "src.agent.xgboost_baseline.get_settings",
        lambda: type("S", (), {"xgboost_model_sha256": "0" * 64})(),
    )

    assert XGBoostBaseline(model_path=str(path)).is_trained is False, (
        "a pickle whose digest does not match the pin was executed"
    )

    good = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "src.agent.xgboost_baseline.get_settings",
        lambda: type("S", (), {"xgboost_model_sha256": good})(),
    )
    # Matching pin, unpickleable bytes: still rules, but it got PAST the pin.
    assert XGBoostBaseline(model_path=str(path)).is_trained is False


def test_every_training_run_writes_a_matching_model_card(tmp_path: Any) -> None:
    """The joblib is an opaque binary with no provenance; the card IS the
    provenance. The pairing contract: same directory, same stem, a SHA-256
    that matches the joblib's actual bytes, and the feature width the loader
    refuses stale models against."""
    import hashlib
    import json
    import subprocess
    import sys

    out = tmp_path / "m.joblib"
    card_path = out.with_suffix(".card.json")

    # Run the real CLI exactly as the Dockerfile does — the card is the
    # script's contract, not the trainer helper's.
    res = subprocess.run(
        [sys.executable, "scripts/train_xgboost.py", "--n-samples", "5000",
         "--output", str(out)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr[-400:]
    assert out.is_file() and card_path.is_file(), "training wrote no model card"

    card = json.loads(card_path.read_text())
    assert card["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest(), (
        "the card's digest does not match the joblib beside it"
    )
    from src.agent.xgboost_baseline import FAILURE_CLASSES, METHODS
    assert card["feature_width"] == len(FAILURE_CLASSES) + len(METHODS) + 6
    assert card["action_labels"] == ["retry_now", "retry_at", "switch_rail",
                                     "nudge_customer", "abandon"]
    assert card["pin_hint"].startswith("XGBOOST_MODEL_SHA256=")
    assert sum(card["label_counts"].values()) == card["samples"]
