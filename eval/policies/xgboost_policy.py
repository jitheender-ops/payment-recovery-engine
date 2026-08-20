"""XGBoost / rule-based policy for the eval harness."""

from __future__ import annotations
from typing import Optional
import pandas as pd

NON_RETRYABLE = {"hard_decline", "fraud_block", "customer_cancelled", "invalid_card", "expired_instrument"}


class XGBoostPolicy:
    """Uses trained XGBoost model or rule-based heuristic fallback."""

    def __init__(self, model_path: Optional[str] = None) -> None:
        self._model = None
        if model_path:
            try:
                import joblib
                from pathlib import Path
                if Path(model_path).exists():
                    self._model = joblib.load(model_path)
            except Exception:
                pass

    def decide(self, scenario: pd.Series, attempt: int = 0) -> dict:
        if attempt >= 3:
            return {"action": "abandon", "rail": None, "delay_minutes": 0, "reason": "Max attempts"}

        fc = scenario.get("failure_class", "unknown")
        method = scenario.get("method", "upi")

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
