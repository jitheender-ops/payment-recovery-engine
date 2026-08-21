"""LLM policy wrapper for eval harness. Optional — requires API keys."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from eval.policies.xgboost_policy import XGBoostPolicy

if TYPE_CHECKING:
    # Type-only: the runtime import stays inside __init__ so that a missing
    # anthropic/openai package degrades to the fallback instead of making this
    # module unimportable.
    from src.agent.policy_agent import PolicyAgent

# What every policy's decide() returns: action / rail / delay_minutes / reason.
# Named because it appears in five signatures in this file.
Decision = dict[str, Any]

logger = logging.getLogger(__name__)


class LLMPolicy:
    """Wraps the PolicyAgent for eval. Falls back to XGBoost on failure."""

    def __init__(self) -> None:
        self._agent: PolicyAgent | None = None
        self._fallback = XGBoostPolicy()
        # Counted so a silently-degraded run cannot be reported as an LLM result.
        # A row labelled "LLM Agent" that was actually served by the fallback is
        # worse than no row at all — the runner refuses to publish one.
        self.call_count = 0
        self.fallback_count = 0
        # (scenario_id, attempt) -> decision, filled by prefetch().
        self._cache: dict[tuple[int, int], Decision] = {}
        # Tripped by an unrecoverable provider error (bad key, no credits,
        # model not found). Without it a dead provider still gets one doomed
        # request per scenario — a real run burned 7,856 calls against an
        # HTTP 402 before finishing and reporting a meaningless row.
        self._abort_reason: str | None = None
        try:
            from src.agent.policy_agent import PolicyAgent
            self._agent = PolicyAgent()
        except Exception:
            logger.exception("PolicyAgent could not be constructed — every decision will fall back")

    @property
    def total_calls(self) -> int:
        """
        Decisions the LLM was actually asked for.

        PolicyAgent.call_count is the right denominator: it increments once per
        real decide() invocation, including prefetch. LLMPolicy.call_count only
        counts cache reads by the eval loop, which is a smaller and different
        population — dividing by it produced fallback rates above 100%.
        """
        return getattr(self._agent, "call_count", 0) or self.call_count

    @property
    def total_fallbacks(self) -> int:
        """
        Fallbacks at BOTH levels.

        PolicyAgent.decide() catches its own LLM errors and returns a heuristic
        action, so those never raise up to this class. Counting only our own
        except-branch reported 0% while every single decision was a fallback.
        """
        return self.fallback_count + getattr(self._agent, "fallback_count", 0)

    def prefetch(
        self, scenarios: pd.DataFrame, max_attempt: int = 3, concurrency: int = 10
    ) -> None:
        """
        Precompute every (scenario, attempt) decision concurrently.

        The eval loop is sequential, and one LLM call takes ~4.7s wall-clock —
        60,000 of those in series is 79 hours. Decisions depend only on
        (scenario, attempt), never on prior outcomes, so they can all be issued
        at once: measured ~14x faster at concurrency 10.

        Attempts that the loop never reaches are computed and discarded. That
        wastes maybe a third of the calls, which at these token prices is worth
        far less than the complexity of a wave-by-wave scheduler.
        """
        if self._agent is None:
            return

        # Bound once here rather than read off self inside the closure: the
        # None-check above is what makes the attribute accesses below safe, and
        # a local keeps that guarantee visible to the reader and the type
        # checker instead of relying on self._agent never being reassigned.
        agent = self._agent

        async def run() -> list[tuple[tuple[int, int], Decision]]:
            sem = asyncio.Semaphore(concurrency)

            async def one(
                sid: int, row: pd.Series, attempt: int
            ) -> tuple[tuple[int, int], Decision]:
                async with sem:
                    if self._abort_reason is not None:
                        self.fallback_count += 1
                        return (sid, attempt), self._fallback.decide(row, attempt)
                    try:
                        result = (sid, attempt), await self._decide_async(agent, row, attempt)
                        # decide() catches its own errors, so a fatal provider
                        # failure arrives as a flag rather than an exception.
                        if agent.last_error_status is not None:
                            self._abort_reason = getattr(
                                agent, "last_error_detail",
                                f"HTTP {agent.last_error_status}",
                            )
                            logger.error("Aborting LLM prefetch — %s", self._abort_reason)
                        return result
                    except Exception as exc:
                        status = getattr(exc, "status_code", None)
                        if status in (401, 402, 403, 404):
                            # Retrying cannot fix a key, a balance, or a model id.
                            self._abort_reason = f"HTTP {status}: {str(exc)[:160]}"
                            logger.error("Aborting LLM prefetch — %s", self._abort_reason)
                        else:
                            logger.exception("prefetch failed scenario=%s attempt=%s", sid, attempt)
                        self.fallback_count += 1
                        return (sid, attempt), self._fallback.decide(row, attempt)

            return await asyncio.gather(*[
                one(int(row["scenario_id"]), row, a)
                for _, row in scenarios.iterrows()
                for a in range(max_attempt)
            ])

        for key, decision in asyncio.run(run()):
            self._cache[key] = decision

    def decide(self, scenario: pd.Series, attempt: int = 0) -> Decision:
        """Synchronous wrapper around async agent."""
        if attempt >= 3:
            return {"action": "abandon", "rail": None, "delay_minutes": 0, "reason": "Max attempts"}

        self.call_count += 1

        cached = self._cache.get((int(scenario["scenario_id"]), attempt))
        if cached is not None:
            return cached

        if self._agent is None:
            self.fallback_count += 1
            return self._fallback.decide(scenario, attempt)

        try:
            # asyncio.get_event_loop() no longer creates a loop when none is
            # running — it was deprecated in 3.10 and raises in 3.12+, so the old
            # get_event_loop().run_until_complete() path would have taken the
            # blanket `except Exception` below and silently returned the XGBoost
            # fallback for every single scenario. The eval would have completed,
            # reported an "LLM Agent" row, and never called the LLM once.
            return asyncio.run(self._decide_async(self._agent, scenario, attempt))
        except Exception:
            logger.exception("LLM policy call failed — using XGBoost fallback")
            self.fallback_count += 1
            return self._fallback.decide(scenario, attempt)

    async def _decide_async(
        self, agent: PolicyAgent, scenario: pd.Series, attempt: int
    ) -> Decision:
        """Agent is passed in, not read from self, so this cannot run without one."""
        from src.agent.actions import FailureContext

        now = datetime.now(UTC)
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

        action = await agent.decide(context)
        delay = 0
        if action.action == "retry_at" and action.retry_at:
            delay = max(0, int((action.retry_at - now).total_seconds() / 60))

        return {
            "action": action.action,
            "rail": action.rail or scenario.get("method", "upi"),
            "delay_minutes": delay,
            "reason": action.reason,
        }
