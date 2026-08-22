"""
The retry_attempts row must be durable BEFORE Razorpay is called.

This is the money-safety invariant of the whole pipeline. If the attempt is
recorded only after the API call returns, a crash in between leaves money moved
with nothing in the database saying so — and because idempotency keys are
derived from a row count, the freed slot lets the next payment.failed for that
payment charge the customer a second time.

The read inside the executor spy runs on a SEPARATE connection, so it sees
committed rows only. A flush, or an add-after-execute, both fail it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agent.actions import ActionType, FailureContext, RetryAction
from src.guardrail.gate import GuardrailResult
from src.models import RetryAttempt, WebhookEvent
from src.orchestrator import PaymentRecoveryOrchestrator


class _FixedAgent:
    """Stands in for PolicyAgent so one code path is exercised deterministically."""

    def __init__(self, action: ActionType = "retry_now") -> None:
        self.fallback_count = 0
        self._action = action

    async def decide(self, context: FailureContext) -> RetryAction:
        return RetryAction(action=self._action, reason="fixed test decision", confidence=0.9)


async def _committed_attempts(
    sessionmaker: async_sessionmaker[AsyncSession], payment_id: str
) -> list[RetryAttempt]:
    """retry_attempts rows visible to a fresh connection — i.e. committed ones."""
    async with sessionmaker() as reader:
        rows = await reader.execute(
            select(RetryAttempt).where(RetryAttempt.payment_id == payment_id)
        )
        return list(rows.scalars().all())


def _orchestrator(monkeypatch: Any, passed: bool, action: ActionType = "retry_now") -> Any:
    """Orchestrator with the agent and guardrail pinned; executor left to caller."""
    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(orch, "_get_agent", lambda: _FixedAgent(action))
    # Pinned because the real gate consults the wall clock (IST retry blackout),
    # which would make this test pass or fail depending on the hour it runs.
    monkeypatch.setattr(
        orch._guardrail,
        "validate",
        lambda *a, **k: GuardrailResult(
            passed=passed,
            rejection_reasons=[] if passed else ["pinned rejection"],
            rules_checked=1,
            rules_failed=0 if passed else 1,
        ),
    )
    return orch


async def _run(
    orch: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
    event_id: str,
) -> None:
    async with sessionmaker() as session:
        event = WebhookEvent(
            razorpay_event_id=event_id,
            event_type="payment.failed",
            payload=payload,
        )
        session.add(event)
        await session.flush()
        await orch.process_payment_failure(event, session)


async def test_attempt_is_committed_before_razorpay_is_called(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    orch = _orchestrator(monkeypatch, passed=True)
    seen: dict[str, Any] = {}

    async def spy_execute(**kwargs: Any) -> dict[str, Any]:
        seen["at_call_time"] = await _committed_attempts(
            db_sessionmaker, kwargs["payment_failure"].payment_id
        )
        return {"success": True, "payment_link_id": "plink_test_wa"}

    monkeypatch.setattr(orch._executor, "execute_retry", spy_execute)

    await _run(orch, db_sessionmaker, sample_webhook_payload, "evt_writeahead_1")

    # The invariant.
    pre = seen["at_call_time"]
    assert len(pre) == 1, "no committed attempt row existed when Razorpay was called"
    assert pre[0].result == "pending"
    assert pre[0].idempotency_key == "retry_pay_test_abc123_0"
    assert pre[0].executed_at is None

    # And the outcome still lands afterwards — the reorder must not lose it.
    post = await _committed_attempts(db_sessionmaker, "pay_test_abc123")
    assert len(post) == 1, "the write-ahead row was duplicated instead of updated"
    assert post[0].result == "success"
    assert post[0].executed_at is not None
    assert post[0].result_details == {"success": True, "payment_link_id": "plink_test_wa"}


async def test_failed_execution_overwrites_pending(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """A raising executor must leave 'failed', not a stuck 'pending'."""
    orch = _orchestrator(monkeypatch, passed=True)

    async def boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("razorpay exploded")

    monkeypatch.setattr(orch._executor, "execute_retry", boom)

    await _run(orch, db_sessionmaker, sample_webhook_payload, "evt_writeahead_2")

    rows = await _committed_attempts(db_sessionmaker, "pay_test_abc123")
    assert len(rows) == 1
    assert rows[0].result == "failed"


async def test_guardrail_rejection_writes_one_row_and_never_calls_razorpay(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """The non-executing paths need no write-ahead and must not double-insert."""
    orch = _orchestrator(monkeypatch, passed=False)
    calls: list[str] = []

    async def spy_execute(**kwargs: Any) -> dict[str, Any]:
        calls.append("called")
        return {"success": True}

    monkeypatch.setattr(orch._executor, "execute_retry", spy_execute)

    await _run(orch, db_sessionmaker, sample_webhook_payload, "evt_writeahead_3")

    rows = await _committed_attempts(db_sessionmaker, "pay_test_abc123")
    assert calls == []
    assert len(rows) == 1
    assert rows[0].result == "rejected"
    assert rows[0].guardrail_passed is False
