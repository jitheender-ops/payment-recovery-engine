"""The receivables chaser's own checks — every module's non-trivial logic.

Run alongside the suite (``pytest tests/test_receivables.py``) or directly
(``python -m tests.test_receivables``) — the ``__main__`` block runs the
pure-logic asserts without pytest, per the project's runnable-check rule.

Grouped by module, in dependency order. DB tests use the suite's throwaway
SQLite harness (conftest.db_sessionmaker) — the same portability shim the
rest of the suite relies on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.cases import open_case
from src.receivables import (
    account_ref_for_case,
    active_contacts,
    add_contact,
    classify,
    compose_stage_message,
    create_plan,
    entry_stage_level,
    gap_multiplier,
    get_or_create_account,
    is_b2b_contact_time,
    next_b2b_window,
    next_stage_gap_hours,
    open_dispute,
    plan_progress,
    record_external_payment,
    resolve_dispute,
    stage_after_break,
    stage_for_aging,
)
from src.receivables.ladder import INVOICE_LADDER
from src.receivables.statement import statement_lines

IST = ZoneInfo("Asia/Kolkata")


# ── ladder.py ──────────────────────────────────────────────────────────────

def test_ladder_shape_is_frozen_and_ascending() -> None:
    """5 stages, ascending thresholds, budget spent only on the 4 rungs."""
    levels = [s.level for s in INVOICE_LADDER]
    assert levels == [0, 1, 2, 3, 4]
    days = [s.days_past_due for s in INVOICE_LADDER]
    assert days == sorted(days)
    spenders = [s for s in INVOICE_LADDER if s.spends_budget]
    assert len(spenders) == 4, "exactly 4 budget-spending rungs (policy promise)"
    assert spenders[0].pre_due is False


def test_stage_for_level_returns_none_past_the_top_rung() -> None:
    """The bound is exclusive: the ladder is indexed 0..len-1.

    `level <= len(INVOICE_LADDER)` walked one index past the end and raised
    IndexError instead of the None this signature promises — and
    stage_after_break can legitimately hand back len-1, so the first caller
    to wire the ratchet in would have tripped it.
    """
    from src.receivables.ladder import stage_for_level

    assert stage_for_level(1) is INVOICE_LADDER[1]
    assert stage_for_level(len(INVOICE_LADDER) - 1) is INVOICE_LADDER[-1]
    assert stage_for_level(len(INVOICE_LADDER)) is None
    assert stage_for_level(0) is None
    assert stage_for_level(99) is None


def test_stage_for_aging_picks_highest_reached() -> None:
    # Not-yet-due / due today maps to the pre-due courtesy stage; the
    # opt-in gate for actually sending it lives with the integration caller.
    assert stage_for_aging(-3).tone == "courtesy"
    assert stage_for_aging(0).tone == "courtesy"
    assert stage_for_aging(1).tone == "friendly"
    assert stage_for_aging(7).tone == "firm"
    assert stage_for_aging(13).tone == "firm"
    assert stage_for_aging(14).tone == "urgent"
    assert stage_for_aging(28).tone == "final"


def test_broken_promise_ratchet_never_goes_back() -> None:
    assert stage_after_break(1) == 2
    assert stage_after_break(2) == 3
    assert stage_after_break(4) == 4, "past the top: stay at the top, budget stops it"


def test_b2b_window() -> None:
    tue_10 = datetime(2026, 9, 1, 10, 0, tzinfo=IST)  # a Tuesday
    tue_19 = datetime(2026, 9, 1, 19, 0, tzinfo=IST)
    sat_10 = datetime(2026, 9, 5, 10, 0, tzinfo=IST)
    assert is_b2b_contact_time(tue_10)
    assert not is_b2b_contact_time(tue_19)
    assert not is_b2b_contact_time(sat_10)


def test_next_b2b_window_finds_next_morning() -> None:
    fri_1850 = datetime(2026, 9, 4, 18, 50, tzinfo=IST)  # Friday
    nxt = next_b2b_window(fri_1850)
    assert nxt.weekday() == 0  # Monday
    assert (nxt.hour, nxt.minute) == (9, 30)


def test_next_stage_gap_hours() -> None:
    # rung 1 (day 1) → rung 2 (day 7): 6 days
    assert next_stage_gap_hours(1) == 144.0
    # past the final rung: the 30-day window
    assert next_stage_gap_hours(4) == 720.0


# ── statement.py ───────────────────────────────────────────────────────────

def _statement_case(ref: str, due: datetime, paise: int, paid: int = 0) -> dict[str, object]:
    return {
        "subject_ref": ref,
        "due_at": due,
        "amount_at_risk": paise,
        "amount_recovered": paid,
        "pay_url": f"https://pay.example/{ref}",
    }


def test_statement_totals_and_aging() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    due_new = now - timedelta(days=2)
    due_old = now - timedelta(days=20)
    s = statement_lines(
        [
            _statement_case("INV-1", due_new, 100_000, 40_000),
            _statement_case("INV-2", due_old, 200_000),
        ],
        now=now,
    )
    assert s["count"] == 2
    # Outstanding is at-risk MINUS recovered — the part-paid truth.
    assert s["total_outstanding"] == 60_000 + 200_000
    assert s["oldest_days"] == 20
    assert s["lines"][0]["outstanding"] == 60_000


def test_compose_stage_message_caps_sms() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    cases = [
        _statement_case("INV-" + "x" * 40, now - timedelta(days=25), 12_34_50_000)
    ]
    msg = compose_stage_message(
        cases,
        tone="final",
        merchant_name="Acme Trading Pvt Ltd",
        statement_link="https://recover.example/s/abc123def456",
        now=now,
    )
    assert len(msg["sms"]) <= 160
    assert "12,34,500" in msg["email_text"]  # Indian grouping
    assert "Acme Trading Pvt Ltd" in msg["subject"]


# ── segments.py ────────────────────────────────────────────────────────────

def test_segment_classification_rules() -> None:
    # No history: standard, never a guess.
    assert classify(median_days_to_pay=None, closed_cases=0) == "standard"
    # Current-cycle facts beat statistics.
    assert classify(
        median_days_to_pay=1.0, closed_cases=9, broken_promises=2
    ) == "default_risk"
    assert classify(
        median_days_to_pay=1.0, closed_cases=9, disputes=1
    ) == "default_risk"
    # The statistics ladder.
    assert classify(median_days_to_pay=2.0, closed_cases=5) == "prompt"
    assert classify(median_days_to_pay=8.0, closed_cases=5) == "slow"
    assert classify(median_days_to_pay=20.0, closed_cases=5) == "chronic_late"


def test_segments_only_tighten() -> None:
    """Adaptivity may skip rungs or stretch gaps — never exceed the envelope."""
    for segment in ("prompt", "standard", "slow", "chronic_late", "default_risk"):
        assert entry_stage_level(segment) >= 1
        assert gap_multiplier(segment) >= 1.0


# ── accounts.py / disputes.py / plans.py / external.py (DB) ───────────────

async def test_account_consolidation_and_contacts(db_sessionmaker) -> None:  # type: ignore[no-untyped-def]
    async with db_sessionmaker() as session:
        acct = await get_or_create_account(
            session, account_ref="acme-corp", display_name="Acme Corp"
        )
        # Idempotent: same ref finds the same row.
        again = await get_or_create_account(session, account_ref="acme-corp")
        assert again.id == acct.id
        assert again.display_name == "Acme Corp"

        await add_contact(
            session, account_id=acct.id, role="finance_manager",
            email="FM@Acme.in", name="Finance Manager",
        )
        clerk = await add_contact(
            session, account_id=acct.id, role="ap_clerk", email="ap@acme.in"
        )
        # Escalation ordering: clerk before manager regardless of insert order.
        contacts = await active_contacts(session, acct.id)
        assert [c.role for c in contacts] == ["ap_clerk", "finance_manager"]
        assert contacts[0].id == clerk.id
        await session.commit()


async def test_case_account_ref_derivation(db_sessionmaker) -> type:  # type: ignore[no-untyped-def]
    async with db_sessionmaker() as session:
        case = await open_case(
            session,
            risk_type="invoice_overdue",
            subject_ref="INV-100",
            amount_at_risk=50_000,
            customer_id="ap@acme.in",
        )
        ref = await account_ref_for_case(session, case)
        assert ref == "derived:email:ap@acme.in"
        await session.commit()
        return type(case)


async def test_dispute_freeze_and_uphold(db_sessionmaker) -> None:  # type: ignore[no-untyped-def]
    async with db_sessionmaker() as session:
        case = await open_case(
            session,
            risk_type="invoice_overdue",
            subject_ref="INV-200",
            amount_at_risk=50_000,
            customer_id="dispute@acme.in",
        )
        d1 = await open_dispute(session, case, reason="  wrong quantity  ")
        assert d1 is not None and d1.status == "open"
        # Double-tap idempotent: same open dispute.
        d2 = await open_dispute(session, case, reason="again")
        assert d2 is not None and d2.id == d1.id
        # Empty reason refused.
        assert await open_dispute(session, case, reason="   ") is None

        resolved = await resolve_dispute(session, d1, outcome="upheld",
                                         note="qty confirmed wrong")
        assert resolved.status == "upheld"
        await session.commit()
        await session.refresh(case)
        assert case.state == "abandoned", "upheld dispute closes the case"
        # Idempotent resolve.
        again = await resolve_dispute(session, resolved, outcome="rejected")
        assert again.status == "upheld"


async def test_plan_creation_and_progress(db_sessionmaker) -> None:  # type: ignore[no-untyped-def]
    async with db_sessionmaker() as session:
        case = await open_case(
            session,
            risk_type="invoice_overdue",
            subject_ref="INV-300",
            amount_at_risk=100_000,
            customer_id="plan@acme.in",
        )
        now = datetime.now(UTC)
        # Shape law: sum mismatch refused.
        bad = await create_plan(
            session, case,
            amounts_paise=[40_000, 40_000],  # sums to 80k ≠ 100k
            due_dates=[now + timedelta(days=7), now + timedelta(days=21)],
        )
        assert bad is None
        # One instalment is a promise, not a plan.
        bad1 = await create_plan(
            session, case,
            amounts_paise=[100_000], due_dates=[now + timedelta(days=7)],
        )
        assert bad1 is None

        plan = await create_plan(
            session, case,
            amounts_paise=[60_000, 40_000],
            due_dates=[now + timedelta(days=7), now + timedelta(days=30)],
        )
        assert plan is not None
        # Second active plan on the same case: refused.
        dup = await create_plan(
            session, case,
            amounts_paise=[100_000],
            due_dates=[now + timedelta(days=14)],
        )
        assert dup is None

        progress = await plan_progress(session, plan)
        assert progress["total"] == 2
        assert progress["pending"] == 2
        assert progress["completed"] is False
        assert progress["defaulted"] is False

        # The plan's promises pushed the case quiet until instalment 1.
        await session.commit()
        await session.refresh(case)
        assert case.next_action_at is not None
        await session.commit()


async def test_external_payment_closes_case(db_sessionmaker) -> None:  # type: ignore[no-untyped-def]
    async with db_sessionmaker() as session:
        case = await open_case(
            session,
            risk_type="invoice_overdue",
            subject_ref="INV-400",
            amount_at_risk=80_000,
            customer_id="neft@acme.in",
        )
        await session.commit()

        r1 = await record_external_payment(
            session, case_id=str(case.id), amount_paise=80_000,
            paid_ref="NEFT-8899", method="neft",
        )
        assert r1 == "recorded"
        await session.commit()
        await session.refresh(case)
        assert case.state == "recovered"
        assert case.amount_recovered == 80_000
        # Honesty: external money counts, never claimed as engine-attributed.
        assert case.recovered_via_attempt_id is None

        r2 = await record_external_payment(
            session, case_id=str(case.id), amount_paise=80_000,
            paid_ref="NEFT-8899",
        )
        # Same bank ref re-POSTed: an idempotent ack, not a refusal — the
        # merchant's retry of their own POST must look successful to them.
        assert r2 == "already_recorded"
        await session.refresh(case)
        assert case.amount_recovered == 80_000, "no double-count on replay"
        # A DIFFERENT ref on a terminal case is the anomaly path: refused.
        r2b = await record_external_payment(
            session, case_id=str(case.id), amount_paise=80_000,
            paid_ref="NEFT-OTHER",
        )
        assert r2b == "refused_terminal"
        r3 = await record_external_payment(
            session, case_id="not-a-uuid", amount_paise=100, paid_ref="X",
        )
        assert r3 == "refused_no_case"
        r4 = await record_external_payment(
            session, case_id=str(case.id), amount_paise=0, paid_ref="X",
        )
        # Amount validation runs before the case lookup: zero is refused
        # as an amount error regardless of case state.
        assert r4 == "refused_amount"


# ── Integration seams (Step 1–4 wiring) ────────────────────────────────────


async def test_risk_event_links_account(db_sessionmaker, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """process_risk_event consolidates invoice cases under one AR account."""
    from sqlalchemy import select as sa_select

    from src.guardrail.gate import GuardrailResult
    from src.models import RecoveryCase, RiskEvent

    async with db_sessionmaker() as session:
        # Pin the decision stack exactly like tests/test_chasers.py does:
        # no real agent call, no real gate (it reads the wall clock), no
        # real Razorpay link mint (the executor is spied below).
        from src.orchestrator import PaymentRecoveryOrchestrator

        orch = PaymentRecoveryOrchestrator()
        calls: list[dict[str, object]] = []

        async def _fixed_decide(context: object, subject: str) -> tuple[object, str]:
            from src.agent.actions import RetryAction

            return RetryAction(action="nudge_customer", reason="pinned"), "pinned"

        monkeypatch.setattr(orch, "_decide_action", _fixed_decide)
        monkeypatch.setattr(
            orch._guardrail,
            "validate",
            lambda *a, **k: GuardrailResult(
                passed=True, rejection_reasons=[], rules_checked=1, rules_failed=0
            ),
        )
        monkeypatch.setattr(orch._nudge_gen, "_get_client", lambda: None)

        async def _spy(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {
                "success": True,
                "payment_link_id": f"plink_{uuid.uuid4().hex[:8]}",
                "short_url": "https://rzp.io/test",
                "channels": ["sms"],
                "nudge_sent": True,
            }

        # The executor owns the link mint (the orchestrator has no
        # _execute_case_action — that spy target was a pre-existing typo and
        # raised AttributeError the moment the test ran).
        monkeypatch.setattr(orch._executor, "execute_case_action", _spy)
        monkeypatch.setattr(
            "src.orchestrator.get_orchestrator", lambda: orch
        )

        event = RiskEvent(
            event_id="evt-acct-1",
            risk_type="invoice_overdue",
            reference_id="INV-9001",
            amount=50_000,
            customer_email="ap@buyer.in",
            account_ref="buyer-corp",
            occurred_at=datetime.now(UTC),
            meta={},
            payload={},
            processed=True,
        )
        session.add(event)
        await session.commit()

        await orch.process_risk_event(event, session)

        case = (
            await session.execute(
                sa_select(RecoveryCase).where(
                    RecoveryCase.subject_ref == "INV-9001"
                )
            )
        ).scalar_one()
        assert case.account_id is not None, "invoice case linked to its AR account"

        # A second invoice for the SAME buyer lands on the SAME account —
        # the consolidation this layer exists for.
        event2 = RiskEvent(
            event_id="evt-acct-2",
            risk_type="invoice_overdue",
            reference_id="INV-9002",
            amount=30_000,
            customer_email="ap@buyer.in",
            account_ref="buyer-corp",
            occurred_at=datetime.now(UTC),
            meta={},
            payload={},
            processed=True,
        )
        session.add(event2)
        await session.commit()
        await orch.process_risk_event(event2, session)
        case2 = (
            await session.execute(
                sa_select(RecoveryCase).where(
                    RecoveryCase.subject_ref == "INV-9002"
                )
            )
        ).scalar_one()
        assert case2.account_id == case.account_id


async def test_consolidation_sweep_contacts_account_once(db_sessionmaker) -> None:  # type: ignore[no-untyped-def]
    """chase_due_accounts: one log per rung, carrier due, joiners deferred."""
    from sqlalchemy import select as sa_select

    from src.receivables.models import ArContactLog

    async with db_sessionmaker() as session:
        account = await get_or_create_account(
            session, account_ref="buyer-corp", display_name="Buyer Corp"
        )
        # Two overdue invoices, one buyer. The oldest is 10 days past due ->
        # stage 2 (firm, day 7). Both due now.
        now = datetime.now(UTC)
        c1 = await open_case(
            session,
            risk_type="invoice_overdue",
            subject_ref="INV-A1",
            amount_at_risk=100_000,
            customer_id="email:ap@buyer.in",
            account_id=account.id,
            due_at=now - timedelta(days=10),
            next_action_at=now - timedelta(hours=1),
            max_attempts=4,
        )
        c2 = await open_case(
            session,
            risk_type="invoice_overdue",
            subject_ref="INV-A2",
            amount_at_risk=40_000,
            customer_id="email:ap@buyer.in",
            account_id=account.id,
            due_at=now - timedelta(days=3),
            next_action_at=now - timedelta(hours=1),
            max_attempts=4,
        )
        await session.commit()

        # The sweep runs only inside B2B hours -- force a business-hour moment.
        from src.receivables.ladder import next_b2b_window

        biz_now = next_b2b_window(now)
        from src.scheduler import chase_due_accounts

        consolidated = await chase_due_accounts(session, now=biz_now)
        assert consolidated == 1, "one rung fired for the account"

        logs = (await session.execute(sa_select(ArContactLog))).scalars().all()
        assert len(logs) == 1
        log = logs[0]
        assert log.stage_level == 2, "10 days past due -> firm rung"
        assert {r["ref"] for r in log.case_refs} == {"INV-A1", "INV-A2"}
        # The log records what the rung SAYS; delivery is the carrier's
        # retry_attempts row, so sent_at stays NULL here -- the honesty rule.
        assert log.sent_at is None

        # SQLite returns naive datetimes; coerce with the sweep's own zone.
        def _aware(ts: datetime) -> datetime:
            return ts if ts.tzinfo is not None else ts.replace(tzinfo=IST)

        await session.refresh(c1)
        await session.refresh(c2)

        # The carrier (both at 0 attempts; c1 has the older due date) stays
        # DUE -- the per-case sweep delivers the rung's contact through the
        # full pipeline. The other joiner is deferred to the next rung's gap.
        assert _aware(c1.next_action_at) <= biz_now, "carrier stays due"
        assert _aware(c2.next_action_at) > biz_now, "joiner deferred to next rung"

        # Idempotency: a second sweep at the same stage contacts nobody --
        # the log is the record that the rung already fired.
        c1.next_action_at = biz_now  # simulate the carrier's 72h floor expiring
        c2.next_action_at = biz_now
        await session.commit()
        consolidated2 = await chase_due_accounts(session, now=biz_now)
        assert consolidated2 == 0, "rung already fired -- no second contact"
        logs2 = (await session.execute(sa_select(ArContactLog))).scalars().all()
        assert len(logs2) == 1, "no new log row for an already-fired rung"

        # A disputed case is excluded from the next rung entirely. Age the
        # account past the stage-3 threshold (14 days) so the next sweep is
        # a NEW rung, not the fired stage 2.
        d = await open_dispute(session, c2, reason="quantity mismatch")
        assert d is not None and d.status == "open"
        c1.due_at = biz_now - timedelta(days=16)
        c1.next_action_at = biz_now - timedelta(minutes=1)
        c2.due_at = biz_now - timedelta(days=4)
        c2.next_action_at = biz_now - timedelta(minutes=1)
        await session.commit()

        consolidated3 = await chase_due_accounts(session, now=biz_now)
        assert consolidated3 == 1
        logs3 = (await session.execute(sa_select(ArContactLog))).scalars().all()
        assert len(logs3) == 2
        # The disputed invoice never appears on the new statement.
        assert "INV-A2" not in {r["ref"] for r in logs3[1].case_refs}


async def test_open_case_accepts_account_id(db_sessionmaker) -> None:  # type: ignore[no-untyped-def]
    """open_case passes account_id through when given (the sweep's input)."""
    from src.receivables.models import ArAccount

    async with db_sessionmaker() as session:
        account = ArAccount(account_ref="x-corp")
        session.add(account)
        await session.flush()
        case = await open_case(
            session,
            risk_type="invoice_overdue",
            subject_ref="INV-LINK",
            amount_at_risk=10_000,
            account_id=account.id,
        )
        assert case.account_id == account.id
        await session.commit()


# ── Runnable without pytest ────────────────────────────────────────────────

def _run_pure_asserts() -> None:
    test_ladder_shape_is_frozen_and_ascending()
    test_stage_for_aging_picks_highest_reached()
    test_broken_promise_ratchet_never_goes_back()
    test_b2b_window()
    test_next_b2b_window_finds_next_morning()
    test_next_stage_gap_hours()
    test_statement_totals_and_aging()
    test_compose_stage_message_caps_sms()
    test_segment_classification_rules()
    test_segments_only_tighten()
    print("receivables: all pure-logic checks passed")


if __name__ == "__main__":
    _run_pure_asserts()


async def test_a_plan_is_sized_to_the_outstanding_not_the_opening_figure(
    db_sessionmaker,  # type: ignore[no-untyped-def]
) -> None:
    """
    A part-paid case's plan must cover the BALANCE.

    amount_at_risk never shrinks, so sizing a plan against it asked a customer
    who had already paid ₹600 of ₹1,000 to schedule the full ₹1,000 — and then
    refused the plan they actually proposed for "not summing to the
    outstanding amount". Same rule the /promise route and the minted link
    already follow (cases.outstanding_paise).
    """
    from src.receivables.plans import create_plan

    async with db_sessionmaker() as session:
        case = await open_case(
            session, risk_type="invoice_overdue", subject_ref="INV-PLAN-PART",
            amount_at_risk=100_000, max_attempts=4,
        )
        case.amount_recovered = 60_000  # ₹600 of ₹1,000 already paid
        await session.commit()

        now = datetime.now(UTC)
        # Two instalments summing to the ₹400 balance.
        plan = await create_plan(
            session, case,
            amounts_paise=[20_000, 20_000],
            due_dates=[now + timedelta(days=7), now + timedelta(days=21)],
        )
        assert plan is not None, "a plan matching the balance was refused"
        assert plan.principal_paise == 40_000, (
            "the plan recorded the stale opening figure as its principal"
        )

        # And the opening figure is no longer accepted: that money is partly
        # in the bank already.
        stale = await create_plan(
            session, case,
            amounts_paise=[50_000, 50_000],
            due_dates=[now + timedelta(days=7), now + timedelta(days=21)],
        )
        assert stale is None


async def test_a_fully_paid_case_cannot_open_a_plan(
    db_sessionmaker,  # type: ignore[no-untyped-def]
) -> None:
    """Nothing outstanding, nothing to schedule."""
    from src.receivables.plans import create_plan

    async with db_sessionmaker() as session:
        case = await open_case(
            session, risk_type="invoice_overdue", subject_ref="INV-PLAN-DONE",
            amount_at_risk=50_000, max_attempts=4,
        )
        case.amount_recovered = 50_000
        await session.commit()

        now = datetime.now(UTC)
        plan = await create_plan(
            session, case,
            amounts_paise=[25_000, 25_000],
            due_dates=[now + timedelta(days=7), now + timedelta(days=21)],
        )
        assert plan is None
