"""
The "24h" in "max retries per customer per 24h" has to actually mean 24 hours.

The ledger columns only ever incremented, so the fifth retry of a customer's
LIFETIME tripped a limit named "per 24h" — a permanent ban arrived at through a
naming accident. These tests pin the rolling window from both sides: reads
report what counts now (_effective_counts), writes reset a rolled tally before
incrementing (_update_retry_ledger), and both rules agree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agent.actions import ActionType, FailureContext, RetryAction
from src.guardrail.gate import GuardrailResult
from src.models import RetryLedger, WebhookEvent
from src.orchestrator import PaymentRecoveryOrchestrator

PAYMENT = "pay_test_abc123"  # the id inside sample_webhook_payload


class _FixedAgent:
    def __init__(self, action: ActionType) -> None:
        self.fallback_count = 0
        self._action = action
        self.seen_contexts: list[FailureContext] = []

    async def decide(self, context: FailureContext) -> RetryAction:
        self.seen_contexts.append(context)
        return RetryAction(action=self._action, reason="fixed test decision", confidence=0.9)


def _orchestrator(
    monkeypatch: Any,
    action: ActionType,
    *,
    passed: bool = True,
) -> tuple[PaymentRecoveryOrchestrator, _FixedAgent]:
    orch = PaymentRecoveryOrchestrator()
    agent = _FixedAgent(action)
    monkeypatch.setattr(orch, "_get_agent", lambda: agent)
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

    async def no_razorpay(**kwargs: Any) -> dict[str, Any]:
        return {"success": True, "payment_link_id": "plink_test"}

    monkeypatch.setattr(orch._executor, "execute_retry", no_razorpay)
    return orch, agent


async def _ingest(
    orch: PaymentRecoveryOrchestrator,
    sessionmaker: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
    event_id: str,
) -> None:
    async with sessionmaker() as session:
        event = WebhookEvent(
            razorpay_event_id=event_id, event_type="payment.failed", payload=payload
        )
        session.add(event)
        await session.flush()
        await orch.process_payment_failure(event, session)


# ── Reads: the window as it counts NOW ───────────────────────────────────


def test_a_tally_whose_last_contact_is_stale_reads_as_zero() -> None:
    ledger = RetryLedger(
        customer_id="c@example.com",
        total_retries_24h=5,
        total_nudges_24h=2,
        last_retry_at=datetime.now(UTC) - timedelta(hours=25),
        last_nudge_at=None,
    )
    retries, nudges = PaymentRecoveryOrchestrator._effective_counts(
        ledger, datetime.now(UTC)
    )
    assert retries == 0, "a lifetime total must not masquerade as a 24h rate"
    assert nudges == 0


def test_a_tally_inside_the_window_survives() -> None:
    now = datetime.now(UTC)
    ledger = RetryLedger(
        customer_id="c@example.com",
        total_retries_24h=3,
        total_nudges_24h=1,
        last_retry_at=now - timedelta(hours=23),
        last_nudge_at=now - timedelta(hours=1),
    )
    assert PaymentRecoveryOrchestrator._effective_counts(ledger, now) == (3, 1)


def test_the_window_edge_is_exclusive() -> None:
    now = datetime.now(UTC)
    ledger = RetryLedger(
        customer_id="c@example.com",
        total_retries_24h=4,
        last_retry_at=now - timedelta(hours=24, seconds=1),
    )
    retries, _ = PaymentRecoveryOrchestrator._effective_counts(ledger, now)
    assert retries == 0


# ── Writes: incrementing a rolled tally starts from zero ─────────────────


async def test_incrementing_after_the_window_starts_fresh(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """Five lifetime retries then 25 quiet hours → the next one is the first."""
    async with db_sessionmaker() as session:
        session.add(
            RetryLedger(
                customer_id="test@example.com",
                total_retries_24h=5,
                last_retry_at=datetime.now(UTC) - timedelta(hours=25),
            )
        )
        await session.commit()

    orch, _ = _orchestrator(monkeypatch, "retry_now")
    await _ingest(orch, db_sessionmaker, sample_webhook_payload, "evt_window_1")

    async with db_sessionmaker() as reader:
        ledger = (
            await reader.execute(select(RetryLedger))
        ).scalar_one()
    assert ledger.total_retries_24h == 1, "the stale tally was not rolled before writing"


# ── Rejections contact nobody and must not burn quota ────────────────────


async def test_a_guardrail_rejection_does_not_bump_the_customer_ledger(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    orch, _ = _orchestrator(monkeypatch, "retry_now", passed=False)
    await _ingest(orch, db_sessionmaker, sample_webhook_payload, "evt_window_2")

    async with db_sessionmaker() as reader:
        ledgers = list((await reader.execute(select(RetryLedger))).scalars().all())
    assert ledgers == [], (
        "a vetoed retry counted against the customer's contact limits"
    )


async def test_an_approved_retry_does_bump_the_customer_ledger(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    orch, _ = _orchestrator(monkeypatch, "retry_now")
    await _ingest(orch, db_sessionmaker, sample_webhook_payload, "evt_window_3")

    async with db_sessionmaker() as reader:
        ledger = (await reader.execute(select(RetryLedger))).scalar_one()
    assert ledger.total_retries_24h == 1


# ── Context quality ──────────────────────────────────────────────────────


async def test_previous_outcomes_are_read_even_without_a_customer_id(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """
    Previous outcomes are per-PAYMENT context; gating their lookup on having an
    email meant anonymous webhooks never saw their own attempt history.
    """
    from src.models import RetryAttempt

    async with db_sessionmaker() as session:
        session.add(
            RetryAttempt(
                payment_failure_id=None,
                payment_id=PAYMENT,
                idempotency_key=f"retry_{PAYMENT}_0",
                attempt_number=1,
                action_type="retry_now",
                guardrail_passed=True,
                result="failed",
            )
        )
        await session.commit()

    anonymous = dict(sample_webhook_payload)
    entity = anonymous["payload"]["payment"]["entity"]
    entity.pop("email")
    entity.pop("contact")

    orch, agent = _orchestrator(monkeypatch, "abandon")
    await _ingest(orch, db_sessionmaker, anonymous, "evt_window_4")

    assert len(agent.seen_contexts) == 1
    ctx = agent.seen_contexts[0]
    assert ctx.customer_id is None
    assert ctx.previous_retry_outcomes == ["failed"]


@pytest.mark.parametrize(
    ("utc", "expected_hour"),
    [
        # 18:30 UTC is 00:00 IST — the whole-hour shortcut said 23.
        pytest.param(datetime(2026, 8, 25, 18, 30, tzinfo=UTC), 0, id="half-hour boundary"),
        # 17:59 UTC is 23:29 IST — inside the blackout's opening hour.
        pytest.param(datetime(2026, 8, 25, 17, 59, tzinfo=UTC), 23, id="blackout start"),
        # 12:00 UTC is 17:30 IST — mid-afternoon either way.
        pytest.param(datetime(2026, 8, 25, 12, 0, tzinfo=UTC), 17, id="afternoon"),
    ],
)
async def test_context_hour_is_true_ist_not_utc_plus_five(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
    utc: datetime,
    expected_hour: int,
) -> None:
    import uuid

    from src.models import PaymentFailure

    orch, _ = _orchestrator(monkeypatch, "abandon")
    failure = PaymentFailure(
        payment_id=PAYMENT,
        amount=50000,
        currency="INR",
        method="card",
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_reason="insufficient_funds",
        failure_class="insufficient_funds",
        is_retryable=True,
        customer_email=None,
        customer_contact=None,
        webhook_event_id=uuid.uuid4(),
        failed_at=utc,
    )

    async with db_sessionmaker() as session:
        ctx = await orch._build_failure_context(failure, session, now=utc)

    assert ctx.hour_of_day == expected_hour
