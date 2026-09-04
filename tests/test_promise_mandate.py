"""
The promise that collects itself: UPI Autopay mandate debit.

A promise used to be a deferral plus a reminder plus a link — whether the money
arrived was entirely up to the customer remembering. Where they authorise a
mandate, the scheduler debits it on the date they named.

This is the only path in the engine that takes money with the customer absent,
so the tests here are about the ways that could go wrong rather than the happy
path: charging without the RBI notice, charging twice, charging after the
feature was switched off, and charging a promise the expiry sweep should have
been allowed to break first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import scheduler
from src.cases import open_case
from src.config import get_settings
from src.models import PromiseToPay, RetryAttempt


@pytest.fixture(autouse=True)
def _mandate_on(monkeypatch: Any) -> Any:
    """
    Demo gateway, mandate on — and the orchestrator singleton reset around it.

    get_orchestrator() caches an orchestrator whose RetryExecutor captured a
    real razorpay.Client at construction. Clearing only the settings cache
    leaves that stale executor in place, so these tests passed alone and failed
    in the full suite with "Authentication failed" — the fake was never
    reached. Reset the singleton, not just the settings.

    Blackout off: the mandate debit runs the FULL guardrail, whose
    23:00–07:00 IST quiet-hours rule refused every debit when the suite ran
    late at night — these tests asserted on debit counts, not on quiet-hours
    behaviour (test_blackout_clamp.py owns that). Same fix as
    test_recovery_batch.py's fixture: an empty window (start == end) is off.
    """
    import src.orchestrator as orch

    get_settings.cache_clear()
    orch._orchestrator = None
    monkeypatch.setenv("PROMISE_MANDATE_ENABLED", "true")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("RETRY_BLACKOUT_START_HOUR", "0")
    monkeypatch.setenv("RETRY_BLACKOUT_END_HOUR", "0")
    yield
    orch._orchestrator = None
    get_settings.cache_clear()


async def _case_with_mandated_promise(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    subject: str,
    amount: int = 250_000,
    due_offset_hours: int = -1,
    notified_hours_ago: float | None = 25.0,
    mandate_status: str = "active",
) -> tuple[uuid.UUID, uuid.UUID]:
    """A case carrying one promise with an authorised mandate, already due.

    `notified_hours_ago` plants the successful nudge that the RBI pre-debit
    rule reads through FailureContext.last_notification_sent_at — that is the
    promise reminder in production. None means no notice ever went out.
    """
    now = datetime.now(UTC)
    # Authorise through the real executor path rather than inventing a token.
    # The demo gateway refuses to debit a token it never issued — deliberately,
    # because "money taken on consent that does not exist" is the bug this
    # feature could ship, and demo mode must not be where it looks like it
    # works. So the test authorises the way a customer would.
    from src.executor.retry_executor import RetryExecutor

    authorization = await RetryExecutor().create_mandate_authorization(
        amount_paise=amount,
        customer_email=f"{subject}@example.in",
        customer_contact=None,
        expire_at=now + timedelta(days=30),
        idempotency_key=f"auth_{subject}",
        description="Autopay for your invoice",
    )

    async with db_sessionmaker() as session:
        case = await open_case(
            session,
            risk_type="invoice_overdue",
            subject_ref=subject,
            amount_at_risk=amount,
            currency="INR",
            customer_id=f"email:{subject}@example.in",
        )
        await session.commit()

        promise = PromiseToPay(
            recovery_case_id=case.id,
            customer_id=case.customer_id,
            amount_promised=amount,
            due_at=now + timedelta(hours=due_offset_hours),
            status="pending",
            channel="payment_link",
            mandate_status=mandate_status,
            mandate_token=authorization["mandate_token"],
            mandate_customer_ref=authorization.get("gateway_customer_id")
            or "cust_demo_test",
            mandate_registered_at=now - timedelta(days=2),
        )
        session.add(promise)

        if notified_hours_ago is not None:
            session.add(
                RetryAttempt(
                    payment_id=subject,
                    idempotency_key=f"notice_{subject}",
                    attempt_number=1,
                    action_type="nudge_customer",
                    agent_type="promise_reminder",
                    guardrail_passed=True,
                    result="success",
                    recovery_case_id=case.id,
                    executed_at=now - timedelta(hours=notified_hours_ago),
                )
            )
        await session.commit()
        return case.id, promise.id


async def _promise(
    db_sessionmaker: async_sessionmaker[AsyncSession], promise_id: uuid.UUID
) -> PromiseToPay:
    async with db_sessionmaker() as session:
        row = await session.get(PromiseToPay, promise_id)
        assert row is not None
        return row


# ── The RBI pre-debit notice ────────────────────────────────────────────────


async def test_a_debit_without_the_predebit_notice_is_refused(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    check_mandate_predebit_notification has been written, tested and wired into
    the gate since long before anything could reach it — it guarded an action
    that minted a Payment Link. This is the caller it was written for, and the
    first test that can actually prove the rule stops money.
    """
    _, promise_id = await _case_with_mandated_promise(
        db_sessionmaker, subject="INV-NO-NOTICE", notified_hours_ago=None
    )
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 0

    async with db_sessionmaker() as session:
        attempt = (
            await session.execute(
                RetryAttempt.__table__.select().where(
                    RetryAttempt.idempotency_key == f"mandate_debit_{promise_id}"
                )
            )
        ).first()
    assert attempt is not None, "the refusal must still be audited"
    assert attempt.result == "rejected"


async def test_a_notice_younger_than_24h_is_not_enough(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The framework's notice is 24 hours, not 'we told them at some point'."""
    await _case_with_mandated_promise(
        db_sessionmaker, subject="INV-FRESH-NOTICE", notified_hours_ago=3.0
    )
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 0


async def test_a_debit_with_a_valid_notice_collects(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, promise_id = await _case_with_mandated_promise(
        db_sessionmaker, subject="INV-COLLECTS"
    )
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 1

    async with db_sessionmaker() as session:
        attempt = (
            await session.execute(
                RetryAttempt.__table__.select().where(
                    RetryAttempt.idempotency_key == f"mandate_debit_{promise_id}"
                )
            )
        ).first()
    assert attempt is not None
    assert attempt.result == "success"
    # external_ref is the join key attribute_capture uses to bring the money
    # home — the same slot a payment link id fills for a link-driven recovery.
    assert attempt.external_ref, "a debit with no order id cannot be attributed"


# ── Charging twice is the failure that costs a customer real money ─────────


async def test_a_second_sweep_does_not_charge_again(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    The idempotency key carries the PROMISE id, not an attempt counter,
    precisely so a re-run of this sweep cannot mint a second charge against one
    authorisation.
    """
    await _case_with_mandated_promise(db_sessionmaker, subject="INV-ONCE")
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 1
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 0

    async with db_sessionmaker() as session:
        rows = (
            await session.execute(
                RetryAttempt.__table__.select().where(
                    RetryAttempt.action_type == "retry_now",
                    RetryAttempt.agent_type == "promise_mandate",
                )
            )
        ).all()
    assert len(rows) == 1, "one authorisation, one debit, forever"


# ── The off switch has to actually stop collection ─────────────────────────


async def test_disabling_the_feature_stops_collecting_existing_mandates(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    A flag that only gates NEW authorisations is not an off switch. Turning the
    feature off must stop debits against mandates already authorised, because
    that is what someone flipping it in an incident means by off.
    """
    await _case_with_mandated_promise(db_sessionmaker, subject="INV-FLAG-OFF")
    monkeypatch.setenv("PROMISE_MANDATE_ENABLED", "false")
    get_settings.cache_clear()
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 0


# ── Ordering: charge before break, never the other way round ───────────────


async def test_the_tick_charges_a_due_promise_before_it_can_break(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    charge_due_promises runs before expire_promises in the tick, and the order
    is a correctness property: a promise the engine is about to collect must
    never be recorded as broken by the same pass that collects it.
    """
    _, promise_id = await _case_with_mandated_promise(
        db_sessionmaker,
        subject="INV-ORDER",
        # Past the grace window, so expire_promises would break it on sight.
        due_offset_hours=-(get_settings().promise_grace_hours + 2),
    )
    async with db_sessionmaker() as session:
        counts = await scheduler.tick(session)

    assert counts["promises_charged"] == 1
    row = await _promise(db_sessionmaker, promise_id)
    assert row.status != "broken", "a promise that just paid was called broken"


# ── An incomplete authorisation must not reach the gateway ─────────────────


async def test_a_mandate_missing_its_gateway_ids_is_never_charged(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    Charging with an empty token is a call to Razorpay with no credentials —
    it cannot succeed, and finding that out costs an attempt slot the case
    needs. An authorisation that never completed is marked failed instead.
    """
    _, promise_id = await _case_with_mandated_promise(
        db_sessionmaker, subject="INV-INCOMPLETE"
    )
    async with db_sessionmaker() as session:
        row = await session.get(PromiseToPay, promise_id)
        assert row is not None
        row.mandate_customer_ref = None
        await session.commit()

    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 0
    assert (await _promise(db_sessionmaker, promise_id)).mandate_status == "failed"


# ── A promise with no mandate behaves exactly as it always did ─────────────


async def test_an_unmandated_promise_is_untouched_by_the_sweep(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    mandate_status='none' is the default and the fallback. Above the RBI
    unattended-debit threshold it is the only lawful path, so it must stay
    exactly as it was: no debit, no attempt, no change.
    """
    _, promise_id = await _case_with_mandated_promise(
        db_sessionmaker, subject="INV-NO-MANDATE", mandate_status="none"
    )
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 0
    row = await _promise(db_sessionmaker, promise_id)
    assert row.status == "pending" and row.mandate_status == "none"


async def test_a_charged_promise_still_breaks_if_the_capture_never_lands(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    The reprieve above is bounded. A debit that never settles is a promise that
    was not kept, and a promise protected forever is a row that hangs pending
    forever — worse than being broken, because nothing ever looks at it again.
    """
    grace = get_settings().promise_grace_hours
    _, promise_id = await _case_with_mandated_promise(
        db_sessionmaker,
        subject="INV-NEVER-SETTLES",
        # Past its own grace already, so the ONLY thing protecting it from the
        # expiry sweep is the in-flight reprieve this test is bounding.
        due_offset_hours=-(grace + 2),
    )
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 1

    async with db_sessionmaker() as session:
        row = await session.get(PromiseToPay, promise_id)
        assert row is not None
        assert row.mandate_status == "charged"
        # Wind the charge back past its own grace window: no capture arrived.
        row.mandate_charged_at = datetime.now(UTC) - timedelta(hours=grace + 1)
        await session.commit()

    async with db_sessionmaker() as session:
        await scheduler.expire_promises(session, now=datetime.now(UTC))
        await session.commit()

    assert (await _promise(db_sessionmaker, promise_id)).status == "broken"


# ── The money has to find its way home, and be credited to the engine ──────


async def test_the_capture_credits_the_case_and_keeps_the_promise(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    A mandate debit has no Payment Link, so the capture arrives carrying only
    an order id. If that resolved through the self-recovery hop instead of the
    attempt, the engine would collect money and then report the customer paid
    on their own — understating the one number this product exists to prove.
    """
    from src.cases import attribute_capture
    from src.models import RecoveryCase

    case_id, promise_id = await _case_with_mandated_promise(
        db_sessionmaker, subject="INV-ATTRIBUTED"
    )
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 1

    async with db_sessionmaker() as session:
        attempt = (
            await session.execute(
                RetryAttempt.__table__.select().where(
                    RetryAttempt.idempotency_key == f"mandate_debit_{promise_id}"
                )
            )
        ).first()
        assert attempt is not None
        order_id = attempt.external_ref

        credited = await attribute_capture(
            session,
            amount=250_000,
            recovered_ref="pay_capture_test",
            order_ref=order_id,
        )
        await session.commit()
        assert credited is not None and credited.id == case_id

    async with db_sessionmaker() as session:
        case = await session.get(RecoveryCase, case_id)
        assert case is not None
        assert case.amount_recovered == 250_000
        assert case.recovered_via_attempt_id is not None, (
            "money the engine collected was recorded as the customer self-paying"
        )
        promise = await session.get(PromiseToPay, promise_id)
        assert promise is not None and promise.status == "kept"


# ── Confirmation: only the customer's approval may arm a debit ─────────────


async def test_an_unconfirmed_mandate_is_never_debited(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    Authorising mints a link and sets 'pending'. The customer has not approved
    anything yet — they are on their way to their UPI app. Only the
    confirmation webhook may promote to 'active'. Marking it active at mint
    time would let the sweep debit a mandate nobody agreed to, and Razorpay's
    event catalogue is unverified, so this failure mode must stay safe.
    """
    _, promise_id = await _case_with_mandated_promise(
        db_sessionmaker, subject="INV-UNCONFIRMED", mandate_status="pending"
    )
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 0
    assert (await _promise(db_sessionmaker, promise_id)).mandate_status == "pending"


async def test_the_reconciler_arms_a_mandate_the_gateway_confirms(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    Confirmation asks the gateway rather than recognising a webhook event name.

    The event-name approach was deleted, not refined: Razorpay's reachable
    documentation never carried the event catalogue, so the engine held a
    GUESSED list of names. If the real ones differed, autopay would have
    silently never collected while every test still passed. The token's own
    status is not a guess.
    """
    _, promise_id = await _case_with_mandated_promise(
        db_sessionmaker,
        subject="INV-RECONCILE",
        mandate_status="pending",
        due_offset_hours=6,
    )
    async with db_sessionmaker() as session:
        assert await scheduler.reconcile_pending_mandates(session) == 1

    row = await _promise(db_sessionmaker, promise_id)
    assert row.mandate_status == "active"
    # promised_rail had a column, a migration and a docstring since 0008 and
    # no caller ever set it. This is the caller.
    assert row.promised_rail == "upi_autopay"


async def test_the_reconciler_never_arms_on_an_unreadable_answer(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    "unknown" is not a verdict. A lookup that failed, or a payload we cannot
    read, leaves the mandate pending to be asked again — it must never be able
    to arm a debit, because that would be consent inferred from a network
    error.
    """
    from src.executor.retry_executor import RetryExecutor

    _, promise_id = await _case_with_mandated_promise(
        db_sessionmaker,
        subject="INV-UNREADABLE",
        mandate_status="pending",
        due_offset_hours=6,
    )

    async def _unknown(*_a: Any, **_k: Any) -> str:
        return "unknown"

    monkeypatch.setattr(RetryExecutor, "fetch_mandate_status", _unknown)
    async with db_sessionmaker() as session:
        assert await scheduler.reconcile_pending_mandates(session) == 0
    assert (await _promise(db_sessionmaker, promise_id)).mandate_status == "pending"


async def test_a_declined_authorisation_leaves_the_plain_promise_standing(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """The customer said no to autopay, not to paying. They still named a date."""
    from src.executor.retry_executor import RetryExecutor

    _, promise_id = await _case_with_mandated_promise(
        db_sessionmaker,
        subject="INV-DECLINED",
        mandate_status="pending",
        due_offset_hours=6,
    )

    async def _failed(*_a: Any, **_k: Any) -> str:
        return "failed"

    monkeypatch.setattr(RetryExecutor, "fetch_mandate_status", _failed)
    async with db_sessionmaker() as session:
        assert await scheduler.reconcile_pending_mandates(session) == 0

    row = await _promise(db_sessionmaker, promise_id)
    assert row.mandate_status == "failed"
    assert row.status == "pending", "the promise itself must survive"


def test_the_status_reader_never_invents_consent() -> None:
    """Anything unrecognised is 'unknown', never an optimistic default."""
    from src.executor.retry_executor import RetryExecutor as R

    assert R._read_mandate_status({"recurring_details": {"status": "confirmed"}}) == "active"
    assert R._read_mandate_status({"status": "active"}) == "active"
    assert R._read_mandate_status({"status": "rejected"}) == "failed"
    assert R._read_mandate_status({"status": "initiated"}) == "pending"
    assert R._read_mandate_status({}) == "unknown"
    assert R._read_mandate_status({"status": "something_new"}) == "unknown"
    # The recurring sub-object wins: it is where authorisation state lives.
    assert R._read_mandate_status(
        {"status": "active", "recurring_details": {"status": "rejected"}}
    ) == "failed"


# ── The page must not offer what it cannot honour ──────────────────────────


def test_autopay_is_not_offered_above_the_unattended_debit_cap() -> None:
    """
    RBI exempts unattended e-mandate debits from per-transaction authentication
    only below a threshold. Above it every debit needs the customer present,
    which a 9 AM sweep cannot arrange — so the option is absent, not shown and
    then refused.
    """
    from src.customer.routes import _mandate_offerable

    cap = get_settings().mandate_max_auto_debit_paise
    assert _mandate_offerable(cap) is True
    assert _mandate_offerable(cap + 1) is False
    assert _mandate_offerable(0) is False


def test_autopay_is_not_offered_when_the_feature_is_off(monkeypatch: Any) -> None:
    from src.customer.routes import _mandate_offerable

    monkeypatch.setenv("PROMISE_MANDATE_ENABLED", "false")
    get_settings.cache_clear()
    assert _mandate_offerable(100_000) is False
    get_settings.cache_clear()


def test_the_autopay_copy_carries_no_dashes() -> None:
    """
    docs/design-system/pages/customer-recovery.md: no em-dashes or en-dashes in
    rendered copy, in either language. This is the screen where a customer
    hands over a standing instruction; the copy rules apply hardest here.
    """
    from src.customer.i18n import CATALOGS

    for lang in ("en", "hi"):
        for key in ("autopay_offer", "autopay_detail"):
            assert "—" not in CATALOGS[lang][key]
            assert "–" not in CATALOGS[lang][key]


# ── The ambiguous failure: charged, but we never heard back ────────────────


async def test_a_timeout_after_the_charge_still_leaves_the_money_findable(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    The order id is the ONLY join key a mandate capture carries. Recording it
    after the debit meant a timeout on the charge left an attempt with no order
    id — and if the money did move, the capture would arrive quoting an id
    nothing in the database had ever seen. The money would be invisible.

    So: order created and committed first, then charged. This test kills the
    charge and proves the capture still finds its case.
    """
    from src.cases import attribute_capture
    from src.executor.retry_executor import RetryExecutor
    from src.models import RecoveryCase

    case_id, promise_id = await _case_with_mandated_promise(
        db_sessionmaker, subject="INV-TIMEOUT"
    )

    async def _timeout(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise TimeoutError("gateway did not answer")

    monkeypatch.setattr(RetryExecutor, "charge_mandate", _timeout)
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 0

    async with db_sessionmaker() as session:
        attempt = (
            await session.execute(
                RetryAttempt.__table__.select().where(
                    RetryAttempt.idempotency_key == f"mandate_debit_{promise_id}"
                )
            )
        ).first()
    assert attempt is not None
    assert attempt.result == "failed"
    assert attempt.external_ref, "the order id was lost with the timeout"

    # The debit may in fact have gone through. If it did, the capture must
    # still come home.
    async with db_sessionmaker() as session:
        credited = await attribute_capture(
            session,
            amount=250_000,
            recovered_ref="pay_after_timeout",
            order_ref=attempt.external_ref,
        )
        await session.commit()
        assert credited is not None and credited.id == case_id

    async with db_sessionmaker() as session:
        case = await session.get(RecoveryCase, case_id)
        assert case is not None and case.amount_recovered == 250_000


async def test_an_ambiguous_debit_is_never_retried(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    "Collected once, recorded as failed" is survivable. "Charged twice" is not.
    The idempotency key is UNIQUE and carries the promise id, so the next sweep
    sees the row and stops.
    """
    from src.executor.retry_executor import RetryExecutor

    await _case_with_mandated_promise(db_sessionmaker, subject="INV-AMBIGUOUS")

    async def _timeout(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise TimeoutError("gateway did not answer")

    monkeypatch.setattr(RetryExecutor, "charge_mandate", _timeout)
    async with db_sessionmaker() as session:
        await scheduler.charge_due_promises(session)

    # Charge works again — but the attempt already exists, so nothing re-fires.
    monkeypatch.undo()
    async with db_sessionmaker() as session:
        assert await scheduler.charge_due_promises(session) == 0

    async with db_sessionmaker() as session:
        rows = (
            await session.execute(
                RetryAttempt.__table__.select().where(
                    RetryAttempt.agent_type == "promise_mandate"
                )
            )
        ).all()
    assert len(rows) == 1, "an ambiguous debit was presented a second time"


async def test_a_debit_does_not_spend_the_contact_budget(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    A debit sends the customer nothing. Spending an attempt slot on it would
    mean a case that used its touches chasing then declines to collect the
    mandate those touches earned. The bound still holds — stop_reason refuses a
    case already out of attempts — this only stops the debit consuming it.
    """
    from src.models import RecoveryCase

    case_id, _ = await _case_with_mandated_promise(
        db_sessionmaker, subject="INV-BUDGET"
    )
    async with db_sessionmaker() as session:
        before = (await session.get(RecoveryCase, case_id)).attempts_used
        assert await scheduler.charge_due_promises(session) == 1

    async with db_sessionmaker() as session:
        after = (await session.get(RecoveryCase, case_id)).attempts_used
    assert after == before, "the debit spent a contact slot it never used"
