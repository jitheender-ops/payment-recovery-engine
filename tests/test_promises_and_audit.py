"""
Tests for the schema widening: non-payment risk types, promises, audit trail.

Two things here are regression guards rather than feature tests. The first is
that a case with no payment behind it can record an attempt at all — `risk_type`
named five sources but `retry_attempts.payment_failure_id` was NOT NULL, so four
of them could not write a row. The second is that a pending promise actually
silences the workflow; a promise you log but keep chasing through is worse than
no promise tracker, because now the audit shows you knew.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.cases import (
    attach_attempt,
    attribute_capture,
    due_cases,
    expire_promises,
    log_event,
    open_case,
    record_opt_out,
    record_promise,
    stop_reason,
)
from src.models import CaseEvent, PromiseToPay, RecoveryCase, RetryAttempt

INVOICE = "inv_2026_0042"
CUSTOMER = "ap@buyer-co.example"


async def _events(session: AsyncSession, case: RecoveryCase) -> list[str]:
    result = await session.execute(
        select(CaseEvent.event_type)
        .where(CaseEvent.recovery_case_id == case.id)
        .order_by(CaseEvent.id)
    )
    return [row for (row,) in result.all()]


async def _open_invoice(
    session: AsyncSession, *, amount: int = 250_000, days_overdue: int = 30
) -> RecoveryCase:
    """An overdue receivable — no payment, no webhook, nothing but a due date."""
    now = datetime.now(UTC)
    return await open_case(
        session,
        risk_type="invoice_overdue",
        subject_ref=INVOICE,
        amount_at_risk=amount,
        customer_id=CUSTOMER,
        due_at=now - timedelta(days=days_overdue),
        next_action_at=now - timedelta(minutes=1),
    )


# ── The unblocker ────────────────────────────────────────────────────────


async def test_a_case_with_no_payment_can_record_an_attempt(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An invoice has no payment_failure row. Before this, the insert failed."""
    async with db_sessionmaker() as session:
        case = await _open_invoice(session)
        attempt = RetryAttempt(
            payment_failure_id=None,
            payment_id=None,
            idempotency_key=f"chase_{INVOICE}_0",
            attempt_number=1,
            action_type="nudge_customer",
            agent_type="deterministic",
            guardrail_passed=True,
        )
        attach_attempt(case, attempt, channel="voice", language="hinglish")
        session.add(attempt)
        await session.commit()

        stored = (
            await session.execute(
                select(RetryAttempt).where(RetryAttempt.recovery_case_id == case.id)
            )
        ).scalar_one()
        assert stored.payment_id is None
        assert stored.channel == "voice"
        assert stored.language == "hinglish"


async def test_customer_contact_buys_quiet_until_the_next_rung(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        case = await _open_invoice(session)
        assert stop_reason(case) is None  # due now

        attempt = RetryAttempt(
            idempotency_key=f"chase_{INVOICE}_0",
            attempt_number=1,
            action_type="nudge_customer",
            agent_type="deterministic",
            guardrail_passed=True,
        )
        attach_attempt(case, attempt, channel="sms")

        assert case.escalation_level == 1
        assert case.next_action_at is not None
        assert case.next_action_at > datetime.now(UTC)
        stop = stop_reason(case)
        assert stop is not None and "not due until" in stop


# ── Promises ─────────────────────────────────────────────────────────────


async def test_a_pending_promise_silences_the_case(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        case = await _open_invoice(session)
        due = datetime.now(UTC) + timedelta(days=3)
        promise = await record_promise(
            session, case, amount=250_000, due_at=due, channel="voice", language="hinglish"
        )
        await session.commit()

        assert promise.status == "pending"
        assert case.next_action_at == due
        stop = stop_reason(case)
        assert stop is not None and "not due until" in stop
        assert "promise_made" in await _events(session, case)


async def test_a_promise_never_pulls_contact_earlier_than_the_backoff(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """"I'll pay in an hour" is permission to wait, not permission to call again."""
    async with db_sessionmaker() as session:
        case = await _open_invoice(session)
        attach_attempt(
            case,
            RetryAttempt(
                idempotency_key=f"chase_{INVOICE}_0",
                attempt_number=1,
                action_type="nudge_customer",
                agent_type="deterministic",
                guardrail_passed=True,
            ),
        )
        after_nudge = case.next_action_at
        assert after_nudge is not None

        await record_promise(
            session, case, amount=250_000, due_at=datetime.now(UTC) + timedelta(hours=1)
        )
        assert case.next_action_at == after_nudge


async def test_capture_keeps_the_promise_and_closes_the_case(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        case = await _open_invoice(session)
        attempt = RetryAttempt(
            idempotency_key=f"chase_{INVOICE}_0",
            attempt_number=1,
            action_type="nudge_customer",
            agent_type="deterministic",
            guardrail_passed=True,
            result="pending",
        )
        attach_attempt(case, attempt, external_ref="plink_invoice_42", channel="email")
        session.add(attempt)
        await record_promise(
            session, case, amount=250_000, due_at=datetime.now(UTC) + timedelta(days=2)
        )
        await session.commit()

        credited = await attribute_capture(
            session,
            amount=250_000,
            recovered_ref="pay_settled_777",
            link_id="plink_invoice_42",
        )
        await session.commit()

        assert credited is not None
        assert credited.state == "recovered"
        promise = (
            await session.execute(
                select(PromiseToPay).where(PromiseToPay.recovery_case_id == case.id)
            )
        ).scalar_one()
        assert promise.status == "kept"
        assert promise.resolved_ref == "pay_settled_777"
        assert await _events(session, case) == [
            "opened", "promise_made", "attributed", "promise_kept", "closed",
        ]


async def test_an_overdue_promise_breaks_and_hands_the_case_back(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        case = await _open_invoice(session)
        await record_promise(
            session, case, amount=250_000,
            # Past due AND past the grace window — the break condition is
            # due_at + grace, not due_at (bank posting delays are real).
            due_at=datetime.now(UTC) - timedelta(hours=30)
        )
        await session.commit()

        assert await expire_promises(session) == 1
        await session.commit()

        # Due now, not NULL: NULL means "a webhook will trigger this", and
        # due_cases() skips those — releasing the case that way would drop it
        # into a queue nothing reads.
        assert case.next_action_at is not None
        assert case.next_action_at <= datetime.now(UTC)
        assert stop_reason(case) is None  # chaseable again
        assert case in await due_cases(session)
        assert "promise_broken" in await _events(session, case)


async def test_expire_does_not_reopen_a_closed_case(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A recovered case with a stale promise must not become chaseable again."""
    async with db_sessionmaker() as session:
        case = await _open_invoice(session)
        await record_promise(
            session, case, amount=250_000,
            due_at=datetime.now(UTC) - timedelta(hours=30),
        )
        case.state = "recovered"
        case.next_action_at = datetime.now(UTC) + timedelta(days=9)
        await session.commit()

        assert await expire_promises(session) == 1
        await session.commit()
        assert case.next_action_at is not None
        assert await due_cases(session) == []


async def test_opt_out_cancels_promises_rather_than_breaking_them(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        case = await _open_invoice(session)
        await record_promise(
            session, case, amount=250_000, due_at=datetime.now(UTC) + timedelta(days=2)
        )
        await session.commit()

        assert await record_opt_out(session, CUSTOMER) == 1
        await session.commit()

        promise = (
            await session.execute(
                select(PromiseToPay).where(PromiseToPay.recovery_case_id == case.id)
            )
        ).scalar_one()
        assert promise.status == "cancelled"
        assert case.state == "opted_out"
        assert "promise_cancelled" in await _events(session, case)


# ── Finding work ─────────────────────────────────────────────────────────


async def test_webhook_driven_cases_are_never_swept(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    NULL next_action_at means "a webhook is the trigger". Sweeping those would
    re-run the pipeline over every payment failure ever recorded.
    """
    async with db_sessionmaker() as session:
        await open_case(
            session,
            risk_type="payment_failure",
            subject_ref="pay_webhook_driven_1",
            amount_at_risk=50_000,
        )
        overdue = await _open_invoice(session)
        await session.commit()

        assert [c.id for c in await due_cases(session)] == [overdue.id]
        assert await due_cases(session, risk_type="checkout_abandonment") == []


# ── Audit trail ──────────────────────────────────────────────────────────


async def test_audit_rows_land_in_the_same_transaction_as_the_change(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An audit committed separately can outlive a rollback of what it describes."""
    async with db_sessionmaker() as session:
        case = await _open_invoice(session)
        log_event(session, case, "contacted", actor="agent", channel="voice")
        await session.rollback()

    async with db_sessionmaker() as session:
        orphaned = await session.execute(
            select(CaseEvent).where(CaseEvent.recovery_case_id == case.id)
        )
        assert orphaned.scalars().all() == []
        assert await session.get(RecoveryCase, case.id) is None


async def test_audit_detail_survives_the_round_trip(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        case = await _open_invoice(session)
        log_event(session, case, "contacted", actor="agent", channel="voice", level=2)
        await session.commit()

        stored = (
            await session.execute(
                select(CaseEvent).where(CaseEvent.event_type == "contacted")
            )
        ).scalar_one()
        assert stored.actor == "agent"
        assert stored.detail == {"channel": "voice", "level": 2}
        assert stored.recovery_case_id == case.id


async def test_case_events_are_scoped_to_their_case(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        invoice = await _open_invoice(session)
        cart = await open_case(
            session,
            risk_type="checkout_abandonment",
            subject_ref=f"cart_{uuid.uuid4().hex[:8]}",
            amount_at_risk=9_900,
            customer_id=CUSTOMER,
        )
        await session.commit()

        assert await _events(session, invoice) == ["opened"]
        assert await _events(session, cart) == ["opened"]
