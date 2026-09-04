"""
Batch recovery.

Two properties carry this file, and both are about not overclaiming.

**Money is measured, never inferred.** `execute_retry` returning success
means a Payment Link was MINTED; the customer has not paid. An earlier
version counted that as recovery and reported 100% for a batch where nobody
had paid a rupee — the exact overclaim `recovered_via_attempt_id` exists to
prevent everywhere else. Recovery is now attributed to this run's own
attempt ids and can only move when a capture lands.

**Refusals cost nothing.** A case the guardrail blocks must not spend an
attempt, and a case a stopping rule has already halted must never enter the
cohort at all — a stopping rule that can be overridden by asking again is
not a stopping rule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import recovery_batch
from src.cases import open_case
from src.models import PaymentFailure, RecoveryCase, RetryAttempt


@pytest.fixture(autouse=True)
def _demo(monkeypatch: Any) -> Any:
    from src.config import get_settings

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    # Blackout off: this file asserts on execution counts, not on quiet-hours
    # behaviour (test_blackout_clamp.py owns that). Without this, every test
    # here flakes between 23:00 and 07:00 IST because the guardrail defers
    # every nudge out of the window.
    monkeypatch.setenv("RETRY_BLACKOUT_START_HOUR", "0")
    monkeypatch.setenv("RETRY_BLACKOUT_END_HOUR", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _case(
    sm: async_sessionmaker[AsyncSession], ref: str, *,
    failure_class: str = "insufficient_funds", amount: int = 100_000,
    attempts_used: int = 0, state: str = "open",
    next_action_at: datetime | None = None,
) -> Any:
    now = datetime.now(UTC)
    async with sm() as s:
        s.add(PaymentFailure(
            payment_id=ref, order_id=f"o_{ref}", amount=amount, method="card",
            bank="HDFC", error_code="BAD_REQUEST_ERROR",
            error_source="customer", error_step="payment_authorization",
            error_reason="insufficient_funds", failure_class=failure_class,
            is_retryable=failure_class != "fraud_block",
            webhook_event_id=__import__("uuid").uuid4(), failed_at=now,
        ))
        case = await open_case(
            s, risk_type="payment_failure", subject_ref=ref,
            customer_id=f"{ref}@example.invalid", amount_at_risk=amount,
            max_attempts=3,
        )
        case.attempts_used = attempts_used
        case.state = state
        case.next_action_at = next_action_at
        await s.commit()
        return case.id


async def test_a_stopped_case_never_enters_the_cohort(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A stopping rule that a batch can talk its way past is not a rule."""
    await _case(db_sessionmaker, "pay_live")
    await _case(db_sessionmaker, "pay_spent", attempts_used=3)
    await _case(
        db_sessionmaker, "pay_held",
        next_action_at=datetime.now(UTC) + timedelta(days=2),
    )

    async with db_sessionmaker() as s:
        plan = await recovery_batch.plan(s)

    refs = {c.ref for c in plan.candidates}
    assert "pay_live" in refs
    # Budget-spent is filtered by the query itself; the held case reaches
    # the plan but must be refused with the stopping rule named.
    held = next((c for c in plan.candidates if c.ref == "pay_held"), None)
    assert held is not None and not held.eligible
    assert any("Stopping rule" in r for r in held.blocked_by)
    assert "pay_spent" not in refs


async def test_a_refused_case_spends_no_attempt(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The whole point of a refusal is that nothing was spent."""
    await _case(db_sessionmaker, "pay_fraud", failure_class="fraud_block")

    async with db_sessionmaker() as s:
        plan = await recovery_batch.plan(s)
        result = await recovery_batch.execute(s, plan)

    assert plan.blocked, "a hard decline must be refused"
    assert all(c.blocked_by for c in plan.blocked), "silent refusal"
    async with db_sessionmaker() as s:
        spent = await s.scalar(select(func.count()).select_from(RetryAttempt))
    assert spent == 0
    assert result["attempted"] == 0


async def test_an_accepted_attempt_is_not_counted_as_recovered(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    THE property. In demo mode the fake gateway accepts every link, so a
    batch that equated "accepted" with "recovered" would report 100% with
    nobody having paid. Money moves only when a capture is attributed.
    """
    for i in range(3):
        await _case(db_sessionmaker, f"pay_acc_{i}")

    async with db_sessionmaker() as s:
        plan = await recovery_batch.plan(s)
        result = await recovery_batch.execute(s, plan)

    assert result["attempted"] >= 1
    assert result["accepted"] == result["attempted"], "the fake accepts everything"
    assert result["recovered_paise"] == 0, (
        "a minted payment link is not a recovered rupee"
    )
    assert result["recovered_cases"] == 0

    # And the cases genuinely have not recovered.
    async with db_sessionmaker() as s:
        total = await s.scalar(
            select(func.coalesce(func.sum(RecoveryCase.amount_recovered), 0))
        )
    assert total == 0


async def test_recovery_is_attributed_to_this_runs_own_attempts(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    Money that an EARLIER attempt earned must not be credited to this run.
    A before/after snapshot of the cohort would get this wrong; joining
    through recovered_via_attempt_id cannot.
    """
    case_id = await _case(db_sessionmaker, "pay_prior")
    async with db_sessionmaker() as s:
        prior = RetryAttempt(
            payment_id="pay_prior", idempotency_key="prior_1", attempt_number=1,
            recovery_case_id=case_id, action_type="retry_now",
            agent_type="xgboost", guardrail_passed=True, result="success",
            executed_at=datetime.now(UTC),
        )
        s.add(prior)
        await s.flush()
        case = await s.get(RecoveryCase, case_id)
        assert case is not None
        case.amount_recovered = 100_000       # already recovered, by an
        case.recovered_via_attempt_id = prior.id   # attempt this run did not make
        await s.commit()

    await _case(db_sessionmaker, "pay_fresh")
    async with db_sessionmaker() as s:
        plan = await recovery_batch.plan(s)
        result = await recovery_batch.execute(s, plan)

    assert result["recovered_paise"] == 0, (
        "this run credited itself with money an earlier attempt earned"
    )


async def test_the_cohort_is_capped(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An unbounded write loop behind an HTTP request is how a payments
    system goes down. The cap is not advisory."""
    async with db_sessionmaker() as s:
        plan = await recovery_batch.plan(s, limit=10_000)
    assert len(plan.candidates) <= recovery_batch.MAX_COHORT


async def test_an_abandon_is_a_refusal_not_an_approval(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """
    A policy that says "abandon" has nothing for the batch to run, and the
    guardrail passes it precisely because it is the safe action — so
    "guardrail passed" was the wrong test for eligibility. It made a cohort
    of hard declines look 100% approved, then spent one attempt per case on
    execute_retry's no-op ("No action taken"), bumping attempts_used and
    exhausting budgets while reporting itself 100% accepted.

    The agent is stubbed rather than steered through settings: whether the
    trained model is on disk decided this before, which is how it passed
    locally and failed in CI.
    """
    from src.agent.actions import RetryAction

    class _AlwaysAbandon:
        def predict(self, context: Any) -> RetryAction:
            return RetryAction(
                action="abandon", confidence=0.95,
                reason="Rule-based: fraud_block is a hard decline",
            )

    monkeypatch.setattr(recovery_batch, "_agent", lambda: _AlwaysAbandon())
    await _case(db_sessionmaker, "pay_fraud", failure_class="fraud_block")

    async with db_sessionmaker() as s:
        plan = await recovery_batch.plan(s)
        result = await recovery_batch.execute(s, plan)

    assert not plan.approved, "an abandon must never be approved to run"
    assert plan.blocked and all(c.blocked_by for c in plan.blocked), "silent refusal"
    assert any("Nothing to chase" in r for c in plan.blocked for r in c.blocked_by)
    assert result["attempted"] == 0
    async with db_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(RetryAttempt)) == 0
        case = await s.scalar(select(RecoveryCase))
        assert case is not None and case.attempts_used == 0
