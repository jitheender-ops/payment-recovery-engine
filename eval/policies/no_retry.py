"""No-retry baseline policy — always abandons."""

from __future__ import annotations
import pandas as pd


class NoRetryPolicy:
    """Baseline: never retry any failed payment."""

    def decide(self, scenario: pd.Series, attempt: int = 0) -> dict:
        return {
            "action": "abandon",
            "rail": None,
            "delay_minutes": 0,
            "reason": "No retry policy — baseline floor",
        }
