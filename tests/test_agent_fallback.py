"""
A degraded LLM must hand over to XGBoost, not to a conservative abandon.

The README promises "if the LLM fails, times out, or returns garbage, the system
falls back to XGBoost." That was not true, and the way it was untrue is worth
keeping a test on:

  PolicyAgent.decide() catches its own provider errors and returns a PRIVATE
  heuristic — abandon for everything that is not a network error or bank
  downtime — while incrementing fallback_count. Because it never raises, the
  orchestrator's `except` branch (the one that reaches for XGBoost) could not
  fire. A missing or wrong API key therefore turned the engine into "give up on
  most recoverable payments", and the audit trail recorded an agent decision.

Observed on a live run before the fix: 17 of 17 non-hard-decline payments were
abandoned. After it, the same traffic produced 10 switch_rail, 5 nudge_customer
and 2 retry_at.

These assert on the PERSISTED agent_type, because that column is what the
dashboard and every results table read.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agent.actions import FailureContext, RetryAction
from src.guardrail.gate import GuardrailResult
from src.models import RetryAttempt, WebhookEvent
from src.orchestrator import PaymentRecoveryOrchestrator


class _DegradedAgent:
    """A PolicyAgent whose provider is down.

    Mirrors the real contract exactly: decide() does NOT raise, it returns a
    conservative action AND increments fallback_count. That combination is
    precisely what made the bug invisible.
    """

    def __init__(self) -> None:
        self.fallback_count = 0

    async def decide(self, context: FailureContext) -> RetryAction:
        self.fallback_count += 1
        return RetryAction(
            action="abandon",
            reason="Fallback: conservative abandon (LLM error: HTTP 400)",
            confidence=0.3,
        )


class _HealthyAgent:
    """A provider that answers. fallback_count never moves."""

    def __init__(self) -> None:
        self.fallback_count = 0

    async def decide(self, context: FailureContext) -> RetryAction:
        return RetryAction(action="switch_rail", rail="upi", reason="a real LLM decision")


def _orchestrator(monkeypatch: Any, agent: Any) -> PaymentRecoveryOrchestrator:
    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(orch, "_get_agent", lambda: agent)
    # Pinned: the real gate reads the wall clock for the IST blackout, so an
    # unpinned test passes or fails depending on the hour CI runs.
    monkeypatch.setattr(
        orch._guardrail, "validate",
        lambda *a, **k: GuardrailResult(passed=True, rejection_reasons=[],
                                        rules_checked=1, rules_failed=0),
    )
    monkeypatch.setattr(
        orch._executor, "execute_retry",
        lambda **k: {"success": True, "payment_link_id": "plink_fallback_test"},
    )
    return orch


async def _run(
    orch: PaymentRecoveryOrchestrator,
    sm: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
    event_id: str,
) -> RetryAttempt:
    async with sm() as session:
        event = WebhookEvent(
            razorpay_event_id=event_id, event_type="payment.failed", payload=payload
        )
        session.add(event)
        await session.flush()
        await orch.process_payment_failure(event, session)
    async with sm() as reader:
        return (await reader.execute(select(RetryAttempt))).scalars().one()


async def test_a_degraded_llm_hands_over_to_xgboost(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    orch = _orchestrator(monkeypatch, _DegradedAgent())
    attempt = await _run(orch, db_sessionmaker, sample_webhook_payload, "evt_degraded_1")

    assert attempt.agent_type == "xgboost", (
        "a degraded LLM's conservative abandon was accepted as a real decision — "
        "this is the bug that made the XGBoost fallback unreachable"
    )
    assert "Fallback: conservative abandon" not in (attempt.agent_reasoning or "")


async def test_a_healthy_llm_decision_is_still_used(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """The fix must not throw away decisions the provider actually made."""
    orch = _orchestrator(monkeypatch, _HealthyAgent())
    attempt = await _run(orch, db_sessionmaker, sample_webhook_payload, "evt_healthy_1")

    assert attempt.agent_type == "llm"
    assert attempt.agent_reasoning == "a real LLM decision"


async def test_no_agent_at_all_still_reaches_xgboost(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """No API key configured: PolicyAgent cannot even be constructed."""
    orch = _orchestrator(monkeypatch, None)
    attempt = await _run(orch, db_sessionmaker, sample_webhook_payload, "evt_noagent_1")
    assert attempt.agent_type == "xgboost"


async def test_a_raising_agent_reaches_xgboost(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """The path that DID work before, kept working."""

    class _Exploding:
        fallback_count = 0

        async def decide(self, context: FailureContext) -> RetryAction:
            raise RuntimeError("provider exploded")

    orch = _orchestrator(monkeypatch, _Exploding())
    attempt = await _run(orch, db_sessionmaker, sample_webhook_payload, "evt_raise_1")
    assert attempt.agent_type == "xgboost"
