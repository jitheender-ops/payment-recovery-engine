"""
Tests for the recovery case lifecycle — bounds, consent, and money attribution.

The attribution tests are the point of this file. Recovery goes out as a Razorpay
Payment Link, so the payment that eventually succeeds has an id we have never
seen; every test here that asserts on `recovered_ref` uses an id deliberately
different from `subject_ref`, because the bug this module fixes was comparing
those two values and matching nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.cases import (
    attach_attempt,
    attribute_capture,
    batch_summary,
    close_case,
    open_case,
    record_opt_out,
    stop_reason,
)
from src.models import PaymentFailure, RecoveryCase, RetryAttempt, RetryLedger

ORIGINAL = "pay_original_fail_001"
LINK_ID = "plink_test_abc"
CAPTURED = "pay_brand_new_999"  # NOT the original — this is the whole problem
ORDER = "order_test_001"


async def _seed_case_with_attempt(
    session: AsyncSession,
    *,
    amount: int = 50000,
    external_ref: str | None = LINK_ID,
    idempotency_key: str = "retry_pay_original_fail_001_0",
    action_type: str = "retry_now",
) -> tuple[RecoveryCase, RetryAttempt]:
    """An open case with one executed attempt against it, as the pipeline leaves it."""
    failure = PaymentFailure(
        payment_id=ORIGINAL,
        order_id=ORDER,
        amount=amount,
        method="card",
        error_code="BAD_REQUEST_ERROR",
        failure_class="insufficient_funds",
        is_retryable=True,
        webhook_event_id=uuid.uuid4(),
        failed_at=datetime.now(UTC),
    )
    session.add(failure)
    await session.flush()

    case = await open_case(
        session,
        risk_type="payment_failure",
        subject_ref=ORIGINAL,
        amount_at_risk=amount,
        customer_id="test@example.com",
        batch_id="batch-1",
    )
    attempt = RetryAttempt(
        payment_failure_id=failure.id,
        payment_id=ORIGINAL,
        idempotency_key=idempotency_key,
        attempt_number=1,
        action_type=action_type,
        guardrail_passed=True,
        result="pending",
    )
    attach_attempt(case, attempt, external_ref=external_ref)
    session.add(attempt)
    await session.commit()
    return case, attempt


# ── Opening cases ────────────────────────────────────────────────────────


async def test_open_case_is_idempotent(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Re-ingesting a failure must find the existing case, not open a second one."""
    async with db_sessionmaker() as session:
        first = await open_case(
            session,
            risk_type="payment_failure",
            subject_ref=ORIGINAL,
            amount_at_risk=50000,
        )
        await session.commit()
        second = await open_case(
            session,
            risk_type="payment_failure",
            subject_ref=ORIGINAL,
            amount_at_risk=50000,
        )
        assert second.id == first.id

        count = await session.execute(select(func.count()).select_from(RecoveryCase))
        assert count.scalar_one() == 1


async def test_same_subject_ref_different_risk_type_are_separate_cases(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The natural key is (risk_type, subject_ref) — ids from different sources can collide."""
    async with db_sessionmaker() as session:
        a = await open_case(
            session, risk_type="payment_failure", subject_ref="ref_1", amount_at_risk=100
        )
        b = await open_case(
            session, risk_type="invoice_overdue", subject_ref="ref_1", amount_at_risk=100
        )
        await session.commit()
        assert a.id != b.id


# ── Stopping rules ───────────────────────────────────────────────────────


async def test_stop_reason_allows_a_fresh_case() -> None:
    case = RecoveryCase(
        risk_type="payment_failure",
        subject_ref=ORIGINAL,
        amount_at_risk=50000,
        max_attempts=3,
        attempts_used=0,
        state="open",
    )
    assert stop_reason(case) is None


async def test_stop_reason_fires_when_budget_is_spent() -> None:
    case = RecoveryCase(
        risk_type="payment_failure",
        subject_ref=ORIGINAL,
        amount_at_risk=50000,
        max_attempts=3,
        attempts_used=3,
        state="open",
    )
    reason = stop_reason(case)
    assert reason is not None
    assert "budget spent" in reason


async def test_stop_reason_fires_on_a_closed_case() -> None:
    case = RecoveryCase(
        risk_type="payment_failure",
        subject_ref=ORIGINAL,
        amount_at_risk=50000,
        max_attempts=3,
        attempts_used=0,
        state="open",
    )
    close_case(case, "recovered", "paid")
    reason = stop_reason(case)
    assert reason is not None
    assert "already recovered" in reason


async def test_stop_reason_fires_on_opt_out_even_with_budget_left() -> None:
    """Consent is not a rate limit — budget remaining does not license contact."""
    case = RecoveryCase(
        risk_type="payment_failure",
        subject_ref=ORIGINAL,
        amount_at_risk=50000,
        max_attempts=3,
        attempts_used=0,
        state="open",
    )
    ledger = RetryLedger(customer_id="test@example.com", consent_status="opted_out")
    reason = stop_reason(case, ledger)
    assert reason == "customer opted out of contact"


async def test_close_case_keeps_the_first_reason() -> None:
    """A later close must not overwrite why the case actually ended."""
    case = RecoveryCase(
        risk_type="payment_failure",
        subject_ref=ORIGINAL,
        amount_at_risk=50000,
        max_attempts=3,
        state="open",
    )
    close_case(case, "recovered", "customer paid")
    close_case(case, "exhausted", "budget spent")
    assert case.state == "recovered"
    assert case.close_reason == "customer paid"


# ── Attempt budget and escalation ────────────────────────────────────────


async def test_attach_attempt_spends_budget_without_escalating() -> None:
    """A rail switch is invisible to the customer, so it escalates nothing."""
    case = RecoveryCase(
        risk_type="payment_failure",
        subject_ref=ORIGINAL,
        amount_at_risk=50000,
        max_attempts=3,
        attempts_used=0,
        escalation_level=0,
        state="open",
    )
    attempt = RetryAttempt(
        payment_failure_id=uuid.uuid4(),
        payment_id=ORIGINAL,
        idempotency_key="k1",
        attempt_number=1,
        action_type="switch_rail",
        guardrail_passed=True,
    )
    attach_attempt(case, attempt, external_ref=LINK_ID)
    assert case.attempts_used == 1
    assert case.escalation_level == 0
    assert attempt.recovery_case_id == case.id
    assert attempt.external_ref == LINK_ID


async def test_attach_attempt_escalates_on_customer_contact() -> None:
    case = RecoveryCase(
        risk_type="payment_failure",
        subject_ref=ORIGINAL,
        amount_at_risk=50000,
        max_attempts=3,
        attempts_used=0,
        escalation_level=0,
        state="open",
    )
    attempt = RetryAttempt(
        payment_failure_id=uuid.uuid4(),
        payment_id=ORIGINAL,
        idempotency_key="k1",
        attempt_number=1,
        action_type="nudge_customer",
        guardrail_passed=True,
    )
    attach_attempt(case, attempt)
    assert case.escalation_level == 1


# ── Money attribution ────────────────────────────────────────────────────


async def test_capture_on_our_link_credits_the_case(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    The regression test for the defect this module exists to fix.

    The captured payment id differs from the case's subject_ref, which is why
    `WHERE RetryAttempt.payment_id == <captured id>` matched nothing and left
    every recovered rupee unattributable.
    """
    async with db_sessionmaker() as session:
        case, attempt = await _seed_case_with_attempt(session)
        assert CAPTURED != case.subject_ref  # the premise of the bug

        credited = await attribute_capture(
            session,
            amount=50000,
            recovered_ref=CAPTURED,
            link_id=LINK_ID,
        )
        await session.commit()

        assert credited is not None
        assert credited.id == case.id
        assert credited.amount_recovered == 50000
        assert credited.recovered_ref == CAPTURED
        assert credited.recovered_via_attempt_id == attempt.id
        assert credited.state == "recovered"
        assert credited.closed_at is not None


async def test_capture_resolves_pending_attempts(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Write-ahead rows must not sit at 'pending' forever once the money lands."""
    async with db_sessionmaker() as session:
        _, attempt = await _seed_case_with_attempt(session)
        assert attempt.result == "pending"

        await attribute_capture(
            session, amount=50000, recovered_ref=CAPTURED, link_id=LINK_ID
        )
        await session.commit()

        # Re-read: the bulk UPDATE does not refresh the in-memory object.
        result = await session.execute(
            select(RetryAttempt.result, RetryAttempt.executed_at).where(
                RetryAttempt.id == attempt.id
            )
        )
        row_result, executed_at = result.one()
        assert row_result == "superseded"
        assert executed_at is not None


async def test_capture_resolves_via_notes_idempotency_key(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    Razorpay copies the link's notes onto the payment, so the key we wrote at
    creation comes back on the capture. It works even when the link id is absent.
    """
    async with db_sessionmaker() as session:
        case, _ = await _seed_case_with_attempt(
            session, external_ref=None, idempotency_key="retry_key_from_notes"
        )
        credited = await attribute_capture(
            session,
            amount=50000,
            recovered_ref=CAPTURED,
            link_id=None,
            idempotency_key="retry_key_from_notes",
        )
        await session.commit()

        assert credited is not None
        assert credited.id == case.id
        assert credited.amount_recovered == 50000


async def test_self_recovery_credits_money_but_not_the_engine(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    The customer pays the order directly, ignoring our link.

    The money is real, so the case closes and stops burning budget — but
    `recovered_via_attempt_id` stays NULL, because crediting the engine here
    would be taking credit for the control group.
    """
    async with db_sessionmaker() as session:
        case, _ = await _seed_case_with_attempt(session)
        credited = await attribute_capture(
            session,
            amount=50000,
            recovered_ref=CAPTURED,
            link_id="plink_never_sent",
            idempotency_key=None,
            order_ref=ORDER,
        )
        await session.commit()

        assert credited is not None
        assert credited.id == case.id
        assert credited.amount_recovered == 50000
        assert credited.state == "recovered"
        assert credited.recovered_via_attempt_id is None


async def test_partial_recovery_leaves_the_case_open(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A part-paid invoice is still money at risk."""
    async with db_sessionmaker() as session:
        case, _ = await _seed_case_with_attempt(session, amount=100000)
        credited = await attribute_capture(
            session, amount=40000, recovered_ref=CAPTURED, link_id=LINK_ID
        )
        await session.commit()

        assert credited is not None
        assert credited.amount_recovered == 40000
        assert credited.state == "open"
        assert credited.closed_at is None


async def test_unrelated_capture_credits_nothing(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Most payments never failed. Those captures must not touch any case."""
    async with db_sessionmaker() as session:
        await _seed_case_with_attempt(session)
        credited = await attribute_capture(
            session,
            amount=50000,
            recovered_ref="pay_someone_else",
            link_id="plink_unknown",
            idempotency_key="key_unknown",
            order_ref="order_unknown",
        )
        await session.commit()
        assert credited is None

        untouched = await session.execute(select(RecoveryCase.amount_recovered))
        assert untouched.scalar_one() == 0


# ── Consent ──────────────────────────────────────────────────────────────


async def test_opt_out_closes_open_cases(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    An opt-out must close the work, not defer it. Skipping one nudge and leaving
    the case open means the next tick contacts them again.
    """
    async with db_sessionmaker() as session:
        case, _ = await _seed_case_with_attempt(session)
        closed = await record_opt_out(session, "test@example.com")
        await session.commit()

        assert closed == 1
        refreshed = await session.get(RecoveryCase, case.id)
        assert refreshed is not None
        assert refreshed.state == "opted_out"

        ledger = await session.execute(
            select(RetryLedger).where(RetryLedger.customer_id == "test@example.com")
        )
        row = ledger.scalar_one()
        assert row.consent_status == "opted_out"
        assert row.opted_out_at is not None


async def test_opt_out_for_unknown_customer_creates_the_ledger_row(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An opt-out from someone we have never retried still has to stick."""
    async with db_sessionmaker() as session:
        closed = await record_opt_out(session, "stranger@example.com")
        await session.commit()
        assert closed == 0

        ledger = await session.execute(
            select(RetryLedger).where(RetryLedger.customer_id == "stranger@example.com")
        )
        assert ledger.scalar_one().consent_status == "opted_out"


# ── Batch reporting ──────────────────────────────────────────────────────


async def test_batch_summary_separates_attributed_from_total_recovery(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    The headline number. Both cases recovered; only one was ours, so
    attributed_paise must be strictly less than recovered_paise.
    """
    async with db_sessionmaker() as session:
        ours = await open_case(
            session,
            risk_type="payment_failure",
            subject_ref="pay_ours",
            amount_at_risk=60000,
            batch_id="batch-1",
        )
        theirs = await open_case(
            session,
            risk_type="payment_failure",
            subject_ref="pay_theirs",
            amount_at_risk=40000,
            batch_id="batch-1",
        )
        ours.amount_recovered = 60000
        ours.recovered_via_attempt_id = uuid.uuid4()
        ours.attempts_used = 2
        theirs.amount_recovered = 40000  # self-recovery: attempt id stays NULL
        theirs.attempts_used = 1
        await session.commit()

        summary = await batch_summary(session, "batch-1")

        assert summary["cases"] == 2
        assert summary["at_risk_paise"] == 100000
        assert summary["recovered_paise"] == 100000
        assert summary["attributed_paise"] == 60000
        assert summary["attempts_used"] == 3
        assert summary["recovery_rate_pct"] == 100.0
        assert summary["attributed_rate_pct"] == 60.0


async def test_batch_summary_excludes_other_batches(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        for i, batch in enumerate(("batch-1", "batch-2")):
            case = await open_case(
                session,
                risk_type="payment_failure",
                subject_ref=f"pay_{i}",
                amount_at_risk=10000,
                batch_id=batch,
            )
            case.amount_recovered = 10000
        await session.commit()

        assert (await batch_summary(session, "batch-1"))["recovered_paise"] == 10000
        assert (await batch_summary(session))["recovered_paise"] == 20000


async def test_batch_summary_on_an_empty_batch_reports_zero_not_a_crash(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Percentages divide by amount at risk — an empty batch must not raise."""
    async with db_sessionmaker() as session:
        summary = await batch_summary(session, "batch-that-does-not-exist")
        assert summary["cases"] == 0
        assert summary["recovery_rate_pct"] == 0.0
        assert summary["attributed_rate_pct"] == 0.0
