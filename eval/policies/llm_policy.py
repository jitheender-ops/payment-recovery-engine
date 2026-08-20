"""LLM policy wrapper for eval harness. Optional — requires API keys."""

from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Optional
import pandas as pd

from eval.policies.xgboost_policy import XGBoostPolicy


class LLMPolicy:
    """Wraps the PolicyAgent for eval. Falls back to XGBoost on failure."""

    def __init__(self) -> None:
        self._agent = None
        self._fallback = XGBoostPolicy()
        try:
            from src.agent.policy_agent import PolicyAgent
            self._agent = PolicyAgent()
        except Exception:
            pass

    def decide(self, scenario: pd.Series, attempt: int = 0) -> dict:
        """Synchronous wrapper around async agent."""
        if attempt >= 3:
            return {"action": "abandon", "rail": None, "delay_minutes": 0, "reason": "Max attempts"}

        if self._agent is None:
            return self._fallback.decide(scenario, attempt)

        try:
            return asyncio.get_event_loop().run_until_complete(
                self._decide_async(scenario, attempt)
            )
        except RuntimeError:
            # No event loop — create one
            result = asyncio.run(self._decide_async(scenario, attempt))
            return result
        except Exception:
            return self._fallback.decide(scenario, attempt)

    async def _decide_async(self, scenario: pd.Series, attempt: int) -> dict:
        from src.agent.actions import FailureContext

        now = datetime.now(timezone.utc)
        context = FailureContext(
            payment_id=scenario.get("payment_id", "unknown"),
            order_id=scenario.get("order_id"),
            failure_class=scenario.get("failure_class", "unknown"),
            error_code="SIMULATED",
            amount=int(scenario.get("amount", 50000)),
            method=scenario.get("method", "upi"),
            bank=scenario.get("bank"),
            customer_id=scenario.get("customer_id"),
            retry_count_24h=attempt,
            nudge_count_24h=0,
            previous_retry_outcomes=[],
            failed_at=now,
            current_time=now,
            hour_of_day=int(scenario.get("hour_of_day", 12)),
            day_of_week=int(scenario.get("day_of_week", 2)),
            is_retryable=bool(scenario.get("is_retryable", True)),
        )

        action = await self._agent.decide(context)
        delay = 0
        if action.action == "retry_at" and action.retry_at:
            delay = max(0, int((action.retry_at - now).total_seconds() / 60))

        return {
            "action": action.action,
            "rail": action.rail or scenario.get("method", "upi"),
            "delay_minutes": delay,
            "reason": action.reason,
        }
