"""
Regression tests for the seams BETWEEN correct components.

Every bug these cover was invisible to a green suite, and all three failed
the same way: two modules that are individually right and individually
tested, wired together by a third that nobody exercised. The unit tests kept
passing throughout, because none of them ran the pieces together.

  1. tick() sweep ORDER — chase_due_accounts must run before chase_due_cases,
     or the AR ladder never fires and a buyer gets one message per invoice.
     Every existing AR test called chase_due_accounts directly.
  2. An open dispute must freeze the PER-CASE chase, not only the statement
     composer. The freeze was implemented in one of the two paths that
     contact people, and it was the path that never sends anything.
  3. A queued voice call must not be claimable once its case is closed.
     record_opt_out closes cases; nothing told the call queue.

They are deliberately in one file: the class of defect is the point, and a
future seam belongs here next to them rather than buried in a module's own
unit tests, where — by construction — nobody was looking.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import src.receivables.models  # noqa: F401  — register the AR tables on Base
from src.agent.actions import ActionType, FailureContext, RetryAction
from src.cases import open_case, record_opt_out
from src.config import get_settings
from src.guardrail.gate import GuardrailResult
from src.models import RecoveryCase, RetryAttempt, VoiceCallQueue
from src.orchestrator import PaymentRecoveryOrchestrator
from src.receivables.accounts import get_or_create_account
from src.receivables.models import ArContactLog
from src.voice.webhook import router as voice_router


class _FixedAgent:
    """A deterministic stand-in for PolicyAgent — these tests are about the
    wiring, not the decision."""

    def __init__(self, action: ActionType = "nudge_customer") -> None:
        self.fallback_count = 0
        self._action = action

    async def decide(self, context: FailureContext) -> RetryAction:
        return RetryAction(action=self._action, reason="seam test", confidence=0.9)


def _orchestrator(monkeypatch: Any, calls: list[str]) -> PaymentRecoveryOrchestrator:
    """An orchestrator whose every outbound edge is pinned, so the only thing
    a test can observe is how many times a customer was actually contacted."""
    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(orch, "_get_agent", lambda: _FixedAgent())
    monkeypatch.setattr(
        orch._guardrail,
        "validate",
        lambda *a, **k: GuardrailResult(passed=True, rules_checked=1, rules_failed=0),
    )
    monkeypatch.setattr(orch._nudge_gen, "_get_client", lambda: None)

    async def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["case"].subject_ref)
        return {
            "success": True,
            "payment_link_id": f"plink_{uuid.uuid4().hex[:8]}",
            "short_url": "https://rzp.io/seam",
            "channels": ["sms"],
            "nudge_sent": True,
        }

    monkeypatch.setattr(orch._executor, "execute_case_action", spy)
    monkeypatch.setattr("src.orchestrator.get_orchestrator", lambda: orch)
    monkeypatch.setattr("src.scheduler.get_orchestrator", lambda: orch)
    return orch


@pytest.fixture
def inside_b2b_window(monkeypatch: Any) -> None:
    """
    Hold the Mon–Fri 09:30–18:30 IST B2B window open, leaving the clock alone.

    The tempting alternative — pin `now` to next_b2b_window(...) — does not
    work and is worth recording: stop_reason() reads datetime.now(UTC)
    directly and ignores any injected `now`, so a `now` in the future makes
    every case read as "next action not due yet" and nothing is chased. In
    production the two clocks are the same instant, so this is a test-only
    seam; freezing the RULE keeps these tests about the wiring they name.
    """
    import src.receivables.ladder as ladder

    monkeypatch.setattr(ladder, "is_b2b_contact_time", lambda _dt: True)


async def _due_invoice(
    session: AsyncSession,
    ref: str,
    *,
    now: datetime,
    days_overdue: int,
    account_id: uuid.UUID | None = None,
) -> RecoveryCase:
    return await open_case(
        session,
        risk_type="invoice_overdue",
        subject_ref=ref,
        amount_at_risk=100_000,
        customer_id="email:ap@buyer.in",
        account_id=account_id,
        due_at=now - timedelta(days=days_overdue),
        next_action_at=now - timedelta(hours=1),
        max_attempts=4,
    )


# ── Seam 1: the tick's sweep order ─────────────────────────────────────────


async def test_tick_consolidates_before_chasing_per_case(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
    inside_b2b_window: None,
) -> None:
    """
    One buyer, two overdue invoices, one tick → ONE contact.

    chase_due_accounts must run first: it picks a single carrier case, defers
    the other joiner, and leaves the carrier due for chase_due_cases to
    deliver. Ordered the other way (as tick() once was) the per-case sweep
    contacts both invoices separately AND pushes their next_action_at forward,
    so consolidation then finds nothing due and no ar_contact_log row is ever
    written — the ladder silently never fires.
    """
    from src.scheduler import tick

    calls: list[str] = []
    _orchestrator(monkeypatch, calls)
    now = datetime.now(UTC)

    async with db_sessionmaker() as session:
        account = await get_or_create_account(session, account_ref="buyer-corp")
        await _due_invoice(session, "INV-1", now=now, days_overdue=10,
                           account_id=account.id)
        await _due_invoice(session, "INV-2", now=now, days_overdue=3,
                           account_id=account.id)
        await session.commit()

        counts = await tick(session, now=now)

        logs = (await session.execute(sa.select(ArContactLog))).scalars().all()

    assert counts["accounts_consolidated"] == 1, (
        "the AR rung never fired — the per-case sweep ran first and left "
        "nothing due for consolidation"
    )
    assert len(calls) == 1, (
        f"the buyer was contacted {len(calls)} times in one tick ({calls}); "
        "consolidation exists to make that exactly one"
    )
    assert len(logs) == 1
    assert {r["ref"] for r in logs[0].case_refs} == {"INV-1", "INV-2"}, (
        "the rung must record that its one contact covered BOTH invoices"
    )


async def test_invoice_chase_defers_outside_the_b2b_window(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    chase_case itself refuses to contact an invoice outside Mon–Fri
    09:30–18:30 IST.

    Not redundant with the sweep order above: process_risk_event chases
    invoice_overdue INLINE (first_action_hours=0), so a merchant pushing an
    overdue invoice at 02:00 on a Sunday reaches chase_case without either
    sweep running. The window has to be enforced at the funnel every contact
    passes through, not only in the sweep that schedules them.
    """
    calls: list[str] = []
    orch = _orchestrator(monkeypatch, calls)
    saturday = datetime(2026, 8, 29, 4, 30, tzinfo=UTC)  # Sat 10:00 IST

    async with db_sessionmaker() as session:
        case = await _due_invoice(session, "INV-SAT", now=saturday, days_overdue=5)
        await session.commit()

        await orch.chase_case(case, session, now=saturday)
        await session.refresh(case)

    assert calls == [], "an invoice was chased on a Saturday"
    assert case.attempts_used == 0, "a deferral must not spend a budget slot"
    assert case.next_action_at is not None
    nxt = case.next_action_at
    nxt = nxt if nxt.tzinfo else nxt.replace(tzinfo=UTC)
    assert nxt > saturday, "the case must be rescheduled to the window's edge"


# ── Seam 2: an open dispute freezes the chase ──────────────────────────────


async def test_open_dispute_freezes_the_per_case_chase(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
    inside_b2b_window: None,
) -> None:
    """
    The recovery page promises "the freeze is total". It has to hold on the
    path that actually contacts people.

    CaseDispute used to be consulted only by chase_due_accounts, which
    composes statements and sends nothing — so a buyer who formally disputed
    an invoice kept receiving automated demands from the per-case sweep.
    """
    from src.receivables.disputes import open_dispute
    from src.scheduler import chase_due_cases

    calls: list[str] = []
    _orchestrator(monkeypatch, calls)
    now = datetime.now(UTC)

    async with db_sessionmaker() as session:
        case = await _due_invoice(session, "INV-D", now=now, days_overdue=8)
        dispute = await open_dispute(session, case, reason="wrong quantity billed")
        assert dispute is not None and dispute.status == "open"
        await session.commit()

        await chase_due_cases(session, now=now)
        await session.refresh(case)

    assert calls == [], "a disputed invoice was chased"
    assert case.attempts_used == 0, "a frozen case must not spend budget"
    assert case.state == "open", (
        "the freeze must DEFER, not close — resolve_dispute has to be able to "
        "hand the case back to the chase, which a terminal state cannot"
    )


async def test_resolved_dispute_releases_the_case_again(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
    inside_b2b_window: None,
) -> None:
    """The other half: once a human resolves it, the chase resumes."""
    from src.receivables.disputes import open_dispute, resolve_dispute
    from src.scheduler import chase_due_cases

    calls: list[str] = []
    _orchestrator(monkeypatch, calls)
    now = datetime.now(UTC)

    async with db_sessionmaker() as session:
        case = await _due_invoice(session, "INV-R", now=now, days_overdue=8)
        dispute = await open_dispute(session, case, reason="disputed line item")
        assert dispute is not None
        await session.commit()

        await resolve_dispute(session, dispute, outcome="rejected", note="invoice stands")
        case.next_action_at = now - timedelta(hours=1)  # due again
        await session.commit()

        await chase_due_cases(session, now=now)

    assert calls == ["INV-R"], "a resolved dispute must release the chase"


# ── Seam 3: the voice queue and the opt-out ────────────────────────────────


SECRET = "seam-voice-secret"


@pytest.fixture
def voice_client(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> AsyncIterator[Any]:
    monkeypatch.setattr("src.voice.webhook.async_session_factory", db_sessionmaker)
    monkeypatch.setenv("VOICE_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(voice_router)
    yield TestClient(app)
    get_settings.cache_clear()


def _signed(body: bytes) -> dict[str, str]:
    return {
        "x-voice-signature": hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "content-type": "application/json",
    }


async def _queue_a_call(session: AsyncSession, case: RecoveryCase) -> VoiceCallQueue:
    attempt = RetryAttempt(
        idempotency_key=f"seam_{case.subject_ref}",
        attempt_number=1,
        recovery_case_id=case.id,
        action_type="nudge_customer",
        guardrail_passed=True,
        result="success",
    )
    session.add(attempt)
    await session.flush()
    row = VoiceCallQueue(
        recovery_case_id=case.id,
        retry_attempt_id=attempt.id,
        customer_contact="+919812345678",
        risk_type=case.risk_type,
        amount_paise=case.amount_at_risk,
        state="queued",
    )
    session.add(row)
    await session.commit()
    return row


async def test_optout_makes_a_queued_voice_call_unclaimable(
    db_sessionmaker: async_sessionmaker[AsyncSession], voice_client: Any
) -> None:
    """
    A queue row is a write-ahead intent, and every other deferred action in
    this codebase re-validates before firing. This one did not: a customer who
    opted out — on the page, by SMS, or by saying "band karo" on an earlier
    call — was still dialled from a row queued before they said stop.
    """
    async with db_sessionmaker() as session:
        case = await open_case(
            session, risk_type="invoice_overdue", subject_ref="INV-V",
            amount_at_risk=100_000, customer_id="email:ap@buyer.in",
            max_attempts=4,
        )
        await session.commit()
        await _queue_a_call(session, case)

        await record_opt_out(session, "email:ap@buyer.in")
        await session.commit()
        await session.refresh(case)
        assert case.state == "opted_out"

    body = b'{"worker": "exotel-bridge-1"}'
    response = voice_client.post("/voice/queue/claim", content=body, headers=_signed(body))

    assert response.status_code == 200
    assert response.json()["call"] is None, (
        "a call was handed to the telephony leg for a customer who opted out"
    )


async def test_recovered_case_makes_a_queued_voice_call_unclaimable(
    db_sessionmaker: async_sessionmaker[AsyncSession], voice_client: Any
) -> None:
    """Same guarantee for the happy path: pay, and the collections call dies
    with the case."""
    from src.cases import close_case

    async with db_sessionmaker() as session:
        case = await open_case(
            session, risk_type="invoice_overdue", subject_ref="INV-P",
            amount_at_risk=100_000, customer_id="email:ap@buyer.in",
            max_attempts=4,
        )
        await session.commit()
        await _queue_a_call(session, case)

        close_case(case, "recovered", "paid in full")
        await session.commit()

    body = b'{"worker": "exotel-bridge-1"}'
    response = voice_client.post("/voice/queue/claim", content=body, headers=_signed(body))

    assert response.json()["call"] is None, (
        "a customer who already paid was queued for a collections call"
    )


async def test_open_case_is_still_claimable(
    db_sessionmaker: async_sessionmaker[AsyncSession], voice_client: Any
) -> None:
    """The guard must not break the feature it is guarding."""
    async with db_sessionmaker() as session:
        case = await open_case(
            session, risk_type="invoice_overdue", subject_ref="INV-OK",
            amount_at_risk=100_000, customer_id="email:ap@buyer.in",
            max_attempts=4,
        )
        await session.commit()
        await _queue_a_call(session, case)

    body = b'{"worker": "exotel-bridge-1"}'
    call = voice_client.post(
        "/voice/queue/claim", content=body, headers=_signed(body)
    ).json()["call"]

    assert call is not None and call["phone"] == "+919812345678"


# ── Seam 4: self-recovery on a chaser case ─────────────────────────────────


async def test_notes_subject_ref_attributes_a_chaser_case_self_recovery(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    A buyer paying an invoice through the merchant's OWN Razorpay object must
    close the case.

    attribute_capture's order_ref hop resolves through payment_failures, so it
    reaches the payment rail only — the four chaser types have no row in that
    table. The money arrived and the case went on chasing them for it.

    Self-recovery, so the honesty rule holds: amount_recovered rises, the case
    closes, and recovered_via_attempt_id stays NULL because no attempt of ours
    earned it.
    """
    from src.ingestion.router import attribute_captured_payload

    async with db_sessionmaker() as session:
        case = await open_case(
            session, risk_type="invoice_overdue", subject_ref="INV-SELF",
            amount_at_risk=100_000, customer_id="email:ap@buyer.in",
            max_attempts=4,
        )
        await session.commit()

        credited = await attribute_captured_payload(session, {
            "payload": {"payment": {"entity": {
                "id": "pay_merchant_own_link",
                "amount": 100_000,
                "notes": {"risk_type": "invoice_overdue", "subject_ref": "INV-SELF"},
            }}},
        })
        await session.commit()
        await session.refresh(case)

    assert credited is not None, "the buyer's own payment matched no case"
    assert case.state == "recovered"
    assert case.amount_recovered == 100_000
    assert case.recovered_via_attempt_id is None, (
        "no attempt of ours earned this — the engine must not take the credit"
    )


async def test_notes_cannot_address_a_closed_case_or_an_unknown_risk_type(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`notes` is merchant-controlled data addressing a case by natural key,
    so the pair is validated before it is trusted."""
    from src.cases import close_case
    from src.ingestion.router import attribute_captured_payload

    async with db_sessionmaker() as session:
        settled = await open_case(
            session, risk_type="invoice_overdue", subject_ref="INV-DONE",
            amount_at_risk=100_000, max_attempts=4,
        )
        close_case(settled, "recovered", "already paid")
        await open_case(
            session, risk_type="invoice_overdue", subject_ref="INV-OPEN",
            amount_at_risk=100_000, max_attempts=4,
        )
        await session.commit()

        def _capture(notes: dict[str, str]) -> dict[str, Any]:
            return {"payload": {"payment": {"entity": {
                "id": f"pay_{uuid.uuid4().hex[:8]}", "amount": 100_000, "notes": notes,
            }}}}

        closed = await attribute_captured_payload(
            session, _capture({"risk_type": "invoice_overdue", "subject_ref": "INV-DONE"})
        )
        bogus = await attribute_captured_payload(
            session, _capture({"risk_type": "../etc/passwd", "subject_ref": "INV-OPEN"})
        )

    assert closed is None, "a settled case was re-credited from a stray note"
    assert bogus is None, "an unknown risk type addressed a case"


# ── Seam 5: what a parked attempt fires as ─────────────────────────────────


async def _park_a_mandate_collection(
    session: AsyncSession, *, rail: str | None
) -> tuple[RecoveryCase, RetryAttempt]:
    """A mandate case with a collection parked for the scheduler, and no
    pre-debit notice anywhere on it."""
    case = await open_case(
        session, risk_type="mandate_failure", subject_ref=f"MND-{rail}",
        amount_at_risk=100_000, customer_id="email:payer@buyer.in",
        max_attempts=3,
    )
    case.attempts_used = 1
    attempt = RetryAttempt(
        idempotency_key=f"chase_mandate_failure_MND-{rail}_0",
        attempt_number=1,
        recovery_case_id=case.id,
        action_type="retry_at",
        target_rail=rail,
        guardrail_passed=True,
        result="scheduled",
        scheduled_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    session.add(attempt)
    await session.commit()
    return case, attempt


async def test_a_parked_retry_fires_as_what_it_is_not_as_its_rail(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    A rail preference must not re-label a collection as a rail switch.

    Both fire paths reconstructed the action as
    `"switch_rail" if attempt.target_rail else "retry_now"`, inferring WHAT
    the action is from whether it happens to carry a rail. The guardrail keys
    rules on the action type — check_mandate_predebit_notification guards
    retry_now only — so stamping "upi" on a mandate's parked collection walked
    it straight past the RBI pre-debit notice. `action_type` was on the row
    the whole time.
    """
    from src.scheduler import fire_due_retries

    calls: list[str] = []
    _orchestrator(monkeypatch, calls)
    # The REAL guardrail: this test is about which of its rules gets to run.
    monkeypatch.undo()
    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(orch._nudge_gen, "_get_client", lambda: None)

    async def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["case"].subject_ref)
        return {"success": True, "payment_link_id": "pl", "short_url": "https://rzp.io/x"}

    monkeypatch.setattr(orch._executor, "execute_case_action", spy)
    monkeypatch.setattr("src.scheduler.get_orchestrator", lambda: orch)

    async with db_sessionmaker() as session:
        _, attempt = await _park_a_mandate_collection(session, rail="upi")

        await fire_due_retries(session, now=datetime.now(UTC))
        await session.refresh(attempt)

    assert calls == [], (
        "a mandate was collected with no pre-debit notice — the rail "
        "preference re-labelled it switch_rail and bypassed the RBI rule"
    )
    assert attempt.result == "rejected"
    assert "e-mandate" in (attempt.result_details or {}).get("scheduler", "")


async def test_a_fire_time_rejection_still_advances_the_ladder(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    A rejected fire must leave the case scheduled, not due.

    The ladder-advance block sat inline AFTER the execute call, so every early
    return above it — a guardrail rejection most of all — left next_action_at
    at the fired instant, already in the past. The due-case sweep then
    re-chased the case on the very next tick and the policy's re_chase_hours
    floor was silently skipped.
    """
    from src.chasers.policy import policy_for
    from src.scheduler import fire_due_retries

    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(orch._nudge_gen, "_get_client", lambda: None)
    monkeypatch.setattr("src.scheduler.get_orchestrator", lambda: orch)

    now = datetime.now(UTC)
    async with db_sessionmaker() as session:
        case, attempt = await _park_a_mandate_collection(session, rail=None)
        case.next_action_at = now - timedelta(minutes=5)
        await session.commit()

        await fire_due_retries(session, now=now)
        await session.refresh(case)
        await session.refresh(attempt)

    assert attempt.result == "rejected"
    policy = policy_for("mandate_failure")
    assert policy is not None
    nxt = case.next_action_at
    assert nxt is not None
    nxt = nxt if nxt.tzinfo else nxt.replace(tzinfo=UTC)
    assert nxt > now, (
        "the case is still due at a past instant — the next tick will re-chase "
        "it immediately, ignoring the re_chase_hours floor"
    )
