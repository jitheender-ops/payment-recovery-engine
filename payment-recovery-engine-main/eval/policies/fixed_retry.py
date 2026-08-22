"""Fixed 3-retry baseline policy — dumb fixed schedule."""

from __future__ import annotations

from typing import Any

import pandas as pd


class FixedRetryPolicy:
    """Baseline: retry up to N times on same rail with fixed delay.
    Does NOT check retryability — that's the point (it's a dumb baseline)."""

    def __init__(self, max_retries: int = 3, delay_minutes: int = 15) -> None:
        self.max_retries = max_retries
        self.delay_minutes = delay_minutes

    def decide(self, scenario: pd.Series, attempt: int = 0) -> dict[str, Any]:
        if attempt >= self.max_retries:
            return {
                "action": "abandon",
                "rail": None,
                "delay_minutes": 0,
                "reason": f"Fixed retry exhausted ({self.max_retries} attempts)",
            }
        return {
            "action": "retry_at",
            "rail": scenario.get("method", "upi"),
            "delay_minutes": self.delay_minutes,
            "reason": f"Fixed retry #{attempt + 1} after {self.delay_minutes}min",
        }
