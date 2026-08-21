"""No-retry baseline policy — always abandons."""

from __future__ import annotations

from typing import Any

import pandas as pd


class NoRetryPolicy:
    """Baseline: never retry any failed payment."""

    def decide(self, scenario: pd.Series, attempt: int = 0) -> dict[str, Any]:
        return {
            "action": "abandon",
            "rail": None,
            "delay_minutes": 0,
            "reason": "No retry policy — baseline floor",
        }
