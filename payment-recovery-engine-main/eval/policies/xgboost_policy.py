"""XGBoost policy for the eval harness, with a rule heuristic as fallback.

The model used to be loaded into `self._model` and then never referenced by
`decide()` — every call took the rule branch, so the README row labelled
"XGBoost/Rules" was 100% rules whether a trained model existed or not. It is
consulted now, and `used_model()` reports which path actually ran so a result
table can never again claim a model that was not asked.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# The delay retry_at asks for when the model picks it. Matches the horizon the
# training labels were computed over (scripts/train_xgboost.py RETRY_AT_HOURS).
MODEL_RETRY_AT_MINUTES = 4 * 60

NON_RETRYABLE = {
    "hard_decline",
    "fraud_block",
    "customer_cancelled",
    "invalid_card",
    "expired_instrument",
}


class XGBoostPolicy:
    """Uses trained XGBoost model or rule-based heuristic fallback."""

    def __init__(self, model_path: str | None = None) -> None:
        self._model = None
        self._model_decisions = 0
        self._rule_decisions = 0
        # None means "use the configured path". Pass "" to force the rules,
        # which is what the training script does so it cannot load a previous
        # model over the one it is building.
        if model_path is None:
            from src.config import get_settings
            model_path = get_settings().xgboost_model_path
        if model_path:
            try:
                from pathlib import Path

                import joblib
                if Path(model_path).exists():
                    self._model = joblib.load(model_path)
                    logger.info("XGBoost policy loaded model from %s", model_path)
                else:
                    logger.warning(
                        "XGBoost policy: no model at %s — running rules only. "
                        "Train one with scripts/train_xgboost.py.", model_path
                    )
            except Exception:
                logger.exception("XGBoost policy: model at %s failed to load", model_path)

    def used_model(self) -> bool:
        """True if any decision this run came from the model rather than rules."""
        return self._model_decisions > 0

    @property
    def decision_mix(self) -> dict[str, int]:
        """How many decisions came from each path — for honest result labelling."""
        return {"model": self._model_decisions, "rules": self._rule_decisions}

    def decide(self, scenario: pd.Series, attempt: int = 0) -> dict[str, Any]:
        if attempt >= 3:
            return {"action": "abandon", "rail": None, "delay_minutes": 0, "reason": "Max attempts"}

        fc = scenario.get("failure_class", "unknown")
        method = scenario.get("method", "upi")

        # Hard declines short-circuit ahead of the model, exactly as they do in
        # production (src/orchestrator.py). A model is not asked whether to
        # retry a stolen card.
        if fc not in NON_RETRYABLE and self._model is not None:
            decision = self._decide_with_model(scenario, fc, method)
            if decision is not None:
                self._model_decisions += 1
                return decision

        self._rule_decisions += 1

        # Hard declines — never retry
        if fc in NON_RETRYABLE:
            return {"action": "abandon", "rail": None, "delay_minutes": 0,
                    "reason": f"{fc} is non-retryable"}

        # Network error — immediate retry
        if fc == "network_error":
            return {"action": "retry_now", "rail": method, "delay_minutes": 0,
                    "reason": "Transient network error"}

        # Payment timeout — immediate retry
        if fc == "payment_timeout":
            return {"action": "retry_now", "rail": method, "delay_minutes": 0,
                    "reason": "Payment timed out"}

        # Bank downtime — retry after 30min
        if fc == "bank_downtime":
            return {"action": "retry_at", "rail": method, "delay_minutes": 30,
                    "reason": "Bank downtime, retry after 30min"}

        # 3DS dropoff — switch to UPI
        if fc == "3ds_dropoff":
            alt = "upi" if method != "upi" else "card"
            return {"action": "switch_rail", "rail": alt, "delay_minutes": 0,
                    "reason": "3DS dropoff, switch to simpler auth"}

        # Issuer decline — switch rail
        if fc == "issuer_decline":
            alt = "upi" if method != "upi" else "netbanking"
            return {"action": "switch_rail", "rail": alt, "delay_minutes": 0,
                    "reason": "Issuer decline, try different rail"}

        # Insufficient funds — nudge customer
        if fc == "insufficient_funds":
            return {"action": "nudge_customer", "rail": None, "delay_minutes": 60,
                    "reason": "Insufficient funds, nudge customer"}

        # UPI timeout — nudge
        if fc == "upi_collect_timeout":
            return {"action": "nudge_customer", "rail": None, "delay_minutes": 15,
                    "reason": "UPI timeout, nudge to approve"}

        # Card limit — nudge
        if fc == "card_limit_exceeded":
            alt = "upi" if method != "upi" else "netbanking"
            return {"action": "switch_rail", "rail": alt, "delay_minutes": 0,
                    "reason": "Card limit exceeded, switch rail"}

        # Default — retry after 15min
        return {"action": "retry_at", "rail": method, "delay_minutes": 15,
                "reason": f"Default retry for {fc}"}

    def _decide_with_model(
        self, scenario: pd.Series, fc: str, method: str
    ) -> dict[str, Any] | None:
        """Ask the trained model. None on any error, so a bad model degrades to rules."""
        model = self._model
        if model is None:  # pragma: no cover — caller already checked
            return None
        try:
            from src.agent.actions import FailureContext
            from src.agent.xgboost_baseline import ACTION_LABELS, extract_features
            from src.executor.rail_selector import select_alternative_rail

            now = datetime.now(UTC)
            ctx = FailureContext(
                payment_id=str(scenario.get("payment_id", "sim")),
                failure_class=fc,
                error_code="SIM",
                amount=int(scenario.get("amount", 0)),
                method=method,
                bank=scenario.get("bank"),
                customer_id=scenario.get("customer_id"),
                failed_at=now,
                current_time=now,
                hour_of_day=int(scenario.get("hour_of_day", 12)),
                day_of_week=int(scenario.get("day_of_week", 0)),
                is_retryable=bool(scenario.get("is_retryable", True)),
            )
            import numpy as np
            action = ACTION_LABELS[
                int(model.predict(np.array([extract_features(ctx)]))[0])
            ]
        except Exception:
            logger.exception("XGBoost policy: model prediction failed — using rules")
            return None

        if action == "switch_rail":
            rail = select_alternative_rail(method, fc)
            if rail is None:  # nowhere to switch to; let the rules decide
                return None
            return {"action": "switch_rail", "rail": rail, "delay_minutes": 0,
                    "reason": f"Model: switch from {method} on {fc}"}
        if action == "retry_at":
            return {"action": "retry_at", "rail": method,
                    "delay_minutes": MODEL_RETRY_AT_MINUTES,
                    "reason": f"Model: wait then retry on {fc}"}
        if action == "nudge_customer":
            return {"action": "nudge_customer", "rail": None, "delay_minutes": 60,
                    "reason": f"Model: nudge on {fc}"}
        if action == "abandon":
            return {"action": "abandon", "rail": None, "delay_minutes": 0,
                    "reason": f"Model: not worth chasing on {fc}"}
        return {"action": "retry_now", "rail": method, "delay_minutes": 0,
                "reason": f"Model: retry now on {fc}"}
