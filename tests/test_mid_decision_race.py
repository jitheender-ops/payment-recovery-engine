"""
What can move while the agent is deciding — and the pipeline must notice.

The ledger lock is released for the LLM call (holding a row lock across a
minute of inference was its own failure mode). Everything that used to be
serialised behind that lock by accident must now be re-checked explicitly:
an opt-out that lands mid-decision has to stop the retry, not be overridden
by it, and a capture that recovers the case mid-decision must stop a second
link being minted for money already in the bank.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agent.actions import FailureContext, RetryAction
from src.cases import attribute_capture, record_opt_out
from src.models import CaseEvent, RetryAttempt, WebhookEvent
from src.orchestrator import PaymentRecoveryOrchestrator


class _OptOutMidDecisionAgent:
    """An LLM call during which the customer presses stop."""

    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self.fallback_count = 0
        self._sm = sm

    async def decide(self, context: FailureContext) -> RetryAction:
        async with self._sm() as session:
            assert context.customer_id is not None
            await record_opt_out(session, context.customer_id)
            await session.commit()
        return RetryAction(
            action="retry_now",
            reason="decision made before the opt-out landed",
        )


class _RecoveryMidDecisionAgent:
    """An LLM call during which the customer pays on their own."""

    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self.fallback_count = 0
        self._sm = sm

    async def decide(self, context: FailureContext) -> RetryAction:
        async with self._sm() as session:
            await attribute_capture(
                session,
                amount=context.amount,
                recovered_ref="pay_self_paid_mid_decision",
                order_ref=context.order_id,
            )
            await session.commit()
        return RetryAction(
            action="retry_now",
            reason="decision made before the money landed",
        )


async def _run(
    orch: PaymentRecoveryOrchestrator,
    sm: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
    event_id: str,
) -> None:
    async with sm() as session:
        event = WebhookEvent(
            razorpay_event_id=event_id, event_type="payment.failed", payload=payload
        )
        session.add(event)
        await session.flush()
        await orch.process_payment_failure(event, session)


async def test_an_opt_out_during_the_agent_call_stops_the_retry(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """
    While the lock is released for inference, record_opt_out is no longer
    blocked behind it. The pipeline must re-read the case when the lock is
    re-taken and stop — contacting a customer who pressed stop seconds ago
    is the exact complaint the stop button exists to prevent.
    """
    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(
        orch, "_get_agent", lambda: _OptOutMidDecisionAgent(db_sessionmaker)
    )
    calls: list[str] = []

    async def spy_execute(**kwargs: Any) -> dict[str, Any]:
        calls.append("called")
        return {"success": True, "payment_link_id": "plink_race_1"}

    monkeypatch.setattr(orch._executor, "execute_retry", spy_execute)

    await _run(orch, db_sessionmaker, sample_webhook_payload, "evt_race_optout")

    assert calls == [], "a customer who opted out mid-decision was still contacted"
    async with db_sessionmaker() as reader:
        attempts = list((await reader.execute(select(RetryAttempt))).scalars().all())
        events = list((await reader.execute(select(CaseEvent))).scalars().all())
    assert attempts == [], "the stopped decision must spend no attempt slot"
    assert any(e.event_type == "stopped" for e in events), (
        "the mid-decision stop left no audit trail"
    )


async def test_a_recovery_during_the_agent_call_stops_the_retry(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """
    The same window, the other direction: the customer pays the original
    order while the model is thinking. Minting a retry link for money already
    in the bank is how a customer ends up paying twice.
    """
    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(
        orch, "_get_agent", lambda: _RecoveryMidDecisionAgent(db_sessionmaker)
    )
    calls: list[str] = []

    async def spy_execute(**kwargs: Any) -> dict[str, Any]:
        calls.append("called")
        return {"success": True, "payment_link_id": "plink_race_2"}

    monkeypatch.setattr(orch._executor, "execute_retry", spy_execute)

    await _run(orch, db_sessionmaker, sample_webhook_payload, "evt_race_recovery")

    assert calls == [], "a link was minted for a payment that already recovered"
    async with db_sessionmaker() as reader:
        attempts = list((await reader.execute(select(RetryAttempt))).scalars().all())
    assert attempts == []
