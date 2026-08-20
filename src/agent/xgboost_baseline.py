"""
XGBoost baseline classifier for payment retry decisions.

Trained on simulated failure data, maps FailureContext features to one of
the fixed actions. Used as a comparison point in the eval harness and as
a fallback when the LLM is unavailable.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

import numpy as np

from src.agent.actions import FailureContext, RetryAction
from src.classifier.taxonomy import FailureClass

logger = logging.getLogger(__name__)

# Action labels (must match the order used during training)
ACTION_LABELS = ["retry_now", "retry_at", "switch_rail", "nudge_customer", "abandon"]

# Feature indices for FailureClass one-hot
FAILURE_CLASSES = [fc.value for fc in FailureClass]
METHODS = ["upi", "card", "netbanking", "wallet"]


def extract_features(context: FailureContext) -> np.ndarray:
    """
    Convert a FailureContext into a feature vector for XGBoost.

    Features:
        - failure_class one-hot (14 dims)
        - method one-hot (4 dims)
        - hour_of_day (1 dim, normalized 0-1)
        - day_of_week (1 dim, normalized 0-1)
        - amount_bucket (1 dim, log-scaled)
        - retry_count_24h (1 dim)
        - is_retryable (1 dim)
        - bank_hash (1 dim, hashed to 0-1)
    Total: 23 dims
    """
    features = []

    # Failure class one-hot
    fc_onehot = [1.0 if fc == context.failure_class else 0.0 for fc in FAILURE_CLASSES]
    features.extend(fc_onehot)

    # Method one-hot
    method_onehot = [1.0 if m == context.method else 0.0 for m in METHODS]
    features.extend(method_onehot)

    # Temporal
    features.append(context.hour_of_day / 23.0)
    features.append(context.day_of_week / 6.0)

    # Amount (log-scaled, capped)
    amount_log = np.log1p(min(context.amount, 10_000_000)) / np.log1p(10_000_000)
    features.append(float(amount_log))

    # Customer history
    features.append(min(context.retry_count_24h, 10) / 10.0)

    # Retryable flag
    features.append(1.0 if context.is_retryable else 0.0)

    # Bank hash (deterministic hash to [0, 1])
    bank_str = context.bank or "unknown"
    bank_hash = int(hashlib.md5(bank_str.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    features.append(bank_hash)

    return np.array(features, dtype=np.float32)


class XGBoostBaseline:
    """
    XGBoost multi-class classifier for retry decisions.

    Falls back to rule-based heuristics if no trained model is available.
    """

    def __init__(self, model_path: Optional[str] = None) -> None:
        self._model = None
        if model_path and Path(model_path).exists():
            try:
                import joblib
                self._model = joblib.load(model_path)
                logger.info("XGBoost model loaded from %s", model_path)
            except Exception:
                logger.exception("Failed to load XGBoost model from %s", model_path)

        if self._model is None:
            logger.info("XGBoost: no trained model — using rule-based fallback")

    def predict(self, context: FailureContext) -> RetryAction:
        """
        Predict the optimal recovery action.

        Args:
            context: Full failure context.

        Returns:
            RetryAction from the fixed action space.
        """
        if self._model is not None:
            return self._predict_ml(context)
        return self._predict_heuristic(context)

    def _predict_ml(self, context: FailureContext) -> RetryAction:
        """Use the trained XGBoost model."""
        features = extract_features(context).reshape(1, -1)
        pred_idx = int(self._model.predict(features)[0])
        action_label = ACTION_LABELS[pred_idx]

        # Confidence from probability
        proba = self._model.predict_proba(features)[0]
        confidence = float(proba[pred_idx])

        # Determine rail for switch_rail
        rail = None
        if action_label == "switch_rail":
            rail = "upi" if context.method != "upi" else "card"

        # Determine retry_at for retry_at
        retry_at = None
        if action_label == "retry_at":
            from datetime import timedelta
            retry_at = context.current_time + timedelta(minutes=30)

        return RetryAction(
            action=action_label,
            rail=rail,
            retry_at=retry_at,
            reason=f"XGBoost prediction (confidence={confidence:.2f})",
            confidence=confidence,
        )

    @staticmethod
    def _predict_heuristic(context: FailureContext) -> RetryAction:
        """Rule-based fallback when no trained model is available."""
        from datetime import timedelta

        try:
            fc = FailureClass(context.failure_class)
        except ValueError:
            fc = FailureClass.UNKNOWN

        # Hard declines — always abandon
        if fc.is_hard_decline:
            return RetryAction(
                action="abandon",
                reason=f"Rule-based: {fc.value} is a hard decline",
                confidence=0.95,
            )

        # Network errors — immediate retry
        if fc == FailureClass.NETWORK_ERROR:
            return RetryAction(
                action="retry_now",
                reason="Rule-based: transient network error, immediate retry",
                confidence=0.8,
            )

        # Payment timeout — immediate retry
        if fc == FailureClass.PAYMENT_TIMEOUT:
            return RetryAction(
                action="retry_now",
                reason="Rule-based: payment timed out, immediate retry",
                confidence=0.75,
            )

        # Bank downtime — retry after delay
        if fc == FailureClass.BANK_DOWNTIME:
            return RetryAction(
                action="retry_at",
                retry_at=context.current_time + timedelta(minutes=30),
                reason="Rule-based: bank downtime, retry in 30 minutes",
                confidence=0.7,
            )

        # 3DS drop-off — switch to UPI
        if fc == FailureClass.THREEDS_DROPOFF:
            return RetryAction(
                action="switch_rail",
                rail="upi" if context.method != "upi" else "card",
                reason="Rule-based: 3DS drop-off, switch to simpler auth flow",
                confidence=0.7,
            )

        # Issuer decline — switch rail
        if fc == FailureClass.ISSUER_DECLINE:
            return RetryAction(
                action="switch_rail",
                rail="upi" if context.method != "upi" else "netbanking",
                reason="Rule-based: issuer decline, try different rail",
                confidence=0.5,
            )

        # Insufficient funds — nudge customer
        if fc == FailureClass.INSUFFICIENT_FUNDS:
            return RetryAction(
                action="nudge_customer",
                reason="Rule-based: insufficient funds, customer needs to act",
                confidence=0.7,
            )

        # UPI timeout — nudge customer
        if fc == FailureClass.UPI_COLLECT_TIMEOUT:
            return RetryAction(
                action="nudge_customer",
                reason="Rule-based: UPI collect timeout, remind customer to approve",
                confidence=0.65,
            )

        # Card limit exceeded — nudge customer
        if fc == FailureClass.CARD_LIMIT_EXCEEDED:
            return RetryAction(
                action="nudge_customer",
                reason="Rule-based: card limit exceeded, suggest alternate method",
                confidence=0.65,
            )

        # Default — conservative retry after delay
        if fc.is_retryable:
            return RetryAction(
                action="retry_at",
                retry_at=context.current_time + timedelta(minutes=15),
                reason=f"Rule-based: {fc.value}, conservative retry in 15 min",
                confidence=0.4,
            )

        return RetryAction(
            action="abandon",
            reason=f"Rule-based: {fc.value}, non-retryable, abandon",
            confidence=0.5,
        )

    def train(self, X: np.ndarray, y: np.ndarray, save_path: str) -> None:
        """Train the XGBoost model and save to disk."""
        import xgboost as xgb
        import joblib

        model = xgb.XGBClassifier(
            n_estimators=200,
            max_depth=6,
            learning_rate=0.1,
            objective="multi:softprob",
            num_class=len(ACTION_LABELS),
            eval_metric="mlogloss",
            use_label_encoder=False,
            random_state=42,
        )
        model.fit(X, y)

        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, save_path)
        self._model = model
        logger.info("XGBoost model trained and saved to %s", save_path)
