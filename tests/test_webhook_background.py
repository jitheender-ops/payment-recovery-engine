"""
The background half of the webhook route — where captures become revenue.

`_process_event_background` is the function that actually runs the pipeline
after the 200 goes back to Razorpay. Its `payment.captured` branch is the
attribution path, and it is the branch that was silently broken: recovery goes
out as a Payment Link, so the payment that eventually succeeds carries an id we
have never seen. The old code compared that new id against
`RetryAttempt.payment_id` — which holds the ORIGINAL failed id — so the match
never fired and no rupee was ever creditable.

These tests run the real function against the test database, with only the
Razorpay call itself stubbed.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.cases import attach_attempt, open_case
from src.ingestion import router as router_mod
from src.models import CaseEvent, PaymentFailure, RecoveryCase, RetryAttempt, WebhookEvent

ORIGINAL = "pay_bg_original_1"
LINK = "plink_bg_1"
CAPTURED = "pay_bg_brand_new_9"  # deliberately unlike ORIGINAL — that is the bug
ORDER = "order_bg_1"


def _captured_payload(
    payment_id: str = CAPTURED,
    *,
    link_id: str | None = LINK,
    notes: dict[str, str] | None = None,
    order_id: str | None = None,
    amount: int = 50000,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "entity": "event",
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount,
                    "currency": "INR",
                    "method": "upi",
                    "notes": notes or {},
                    "created_at": int(time.time()),
                }
            }
        },
        "created_at": int(time.time()),
    }
    if order_id:
        payload["payload"]["payment"]["entity"]["order_id"] = order_id
    if link_id:
        payload["payload"]["payment_link"] = {"entity": {"id": link_id}}
    return payload


async def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    external_ref: str | None = LINK,
    idempotency_key: str = "retry_pay_bg_original_1_0",
) -> uuid.UUID:
    """An open case with one executed attempt against it, as the pipeline leaves it."""
    async with sessionmaker() as session:
        failure = PaymentFailure(
            payment_id=ORIGINAL, order_id=ORDER, amount=50000, method="card",
            error_code="BAD_REQUEST_ERROR", failure_class="insufficient_funds",
            is_retryable=True, webhook_event_id=uuid.uuid4(),
            failed_at=datetime.now(UTC),
        )
        session.add(failure)
        await session.flush()

        case = await open_case(
            session, risk_type="payment_failure", subject_ref=ORIGINAL,
            amount_at_risk=50000, customer_id="bg@example.com",
        )
        attempt = RetryAttempt(
            payment_failure_id=failure.id, payment_id=ORIGINAL,
            idempotency_key=idempotency_key, attempt_number=1,
            action_type="retry_now", agent_type="xgboost",
            guardrail_passed=True, result="pending",
        )
        attach_attempt(case, attempt, external_ref=external_ref)
        session.add(attempt)
        await session.commit()
        return case.id


async def _run(
    monkeypatch: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
) -> None:
    """Invoke the real background handler against the test database."""
    monkeypatch.setattr(router_mod, "async_session_factory", sessionmaker)
    await router_mod._process_event_background(
        f"{payload['event']}_x_{payload['created_at']}", payload["event"], payload
    )


async def _case(sm: async_sessionmaker[AsyncSession], case_id: uuid.UUID) -> RecoveryCase:
    async with sm() as reader:
        case = await reader.get(RecoveryCase, case_id)
        assert case is not None
        return case


# ── Attribution ──────────────────────────────────────────────────────────


async def test_a_capture_on_our_link_credits_the_case(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    case_id = await _seed(db_sessionmaker)
    await _run(monkeypatch, db_sessionmaker, _captured_payload())

    case = await _case(db_sessionmaker, case_id)
    assert case.amount_recovered == 50000
    assert case.recovered_ref == CAPTURED
    assert case.state == "recovered"
    assert case.recovered_via_attempt_id is not None, "recovery was not credited to us"


async def test_the_notes_idempotency_key_also_resolves_it(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    Razorpay copies a link's notes onto the payment. The executor has always
    written this breadcrumb; nothing read it back until the case layer existed.
    """
    key = "retry_pay_bg_original_1_0"
    case_id = await _seed(db_sessionmaker, external_ref=None, idempotency_key=key)
    await _run(
        monkeypatch, db_sessionmaker,
        _captured_payload(link_id=None, notes={"retry_idempotency_key": key}),
    )
    case = await _case(db_sessionmaker, case_id)
    assert case.state == "recovered"
    assert case.recovered_via_attempt_id is not None


async def test_a_self_recovery_counts_the_money_but_not_the_credit(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    The customer paid the original order on their own. Real revenue, but a
    headline that cannot tell this apart is taking credit for the control group.
    """
    case_id = await _seed(db_sessionmaker, external_ref=None)
    await _run(
        monkeypatch, db_sessionmaker,
        _captured_payload(link_id=None, order_id=ORDER),
    )
    case = await _case(db_sessionmaker, case_id)
    assert case.amount_recovered == 50000
    assert case.recovered_via_attempt_id is None, "credited an attempt that earned nothing"


async def test_pending_attempts_are_resolved_once_the_money_lands(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    await _seed(db_sessionmaker)
    await _run(monkeypatch, db_sessionmaker, _captured_payload())

    async with db_sessionmaker() as reader:
        attempt = (await reader.execute(select(RetryAttempt))).scalar_one()
    assert attempt.result == "superseded", "the outstanding attempt was left pending"


async def test_an_unrelated_capture_credits_nothing(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """Most payments never fail. This is the common path and must be a no-op."""
    case_id = await _seed(db_sessionmaker)
    await _run(
        monkeypatch, db_sessionmaker,
        _captured_payload("pay_stranger", link_id="plink_someone_else"),
    )
    case = await _case(db_sessionmaker, case_id)
    assert case.amount_recovered == 0
    assert case.state == "open"


async def test_a_capture_with_no_payment_id_is_ignored(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    payload = _captured_payload()
    del payload["payload"]["payment"]["entity"]["id"]
    case_id = await _seed(db_sessionmaker)
    await _run(monkeypatch, db_sessionmaker, payload)
    assert (await _case(db_sessionmaker, case_id)).amount_recovered == 0


async def test_a_second_capture_on_a_closed_case_is_an_overpayment_not_double_credit(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    Every retry mints a NEW link while the old ones stay live until the cancel
    sweep reaches them. A customer paying two links in that window used to
    credit the case TWICE — amount_recovered sailed past amount_at_risk and
    the headline revenue was inflated. The second money arrival on a terminal
    case is an overpayment: logged loudly for manual refund, never credited.
    """
    case_id = await _seed(db_sessionmaker)
    await _run(monkeypatch, db_sessionmaker, _captured_payload())

    case = await _case(db_sessionmaker, case_id)
    assert case.state == "recovered"

    # The same attempt's second live link is paid too — resolves through the
    # notes idempotency key this time.
    second = _captured_payload(
        "pay_bg_second_9",
        link_id=None,
        notes={"retry_idempotency_key": "retry_pay_bg_original_1_0"},
    )
    await _run(monkeypatch, db_sessionmaker, second)

    case = await _case(db_sessionmaker, case_id)
    assert case.amount_recovered == 50000, "the second capture double-credited the case"
    assert case.state == "recovered"

    async with db_sessionmaker() as reader:
        events = (
            await reader.execute(
                select(CaseEvent).where(CaseEvent.recovery_case_id == case_id)
            )
        ).scalars().all()
    overpayments = [e for e in events if e.event_type == "overpayment"]
    assert len(overpayments) == 1, "the double payment was not surfaced for refund"
    assert overpayments[0].detail["amount"] == 50000


# ── The failed-payment branch ────────────────────────────────────────────


async def test_a_missing_event_row_does_not_raise(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """The handler must survive an event id that is not in the store."""
    monkeypatch.setattr(router_mod, "async_session_factory", db_sessionmaker)
    await router_mod._process_event_background("evt_not_stored", "payment.failed", {})


async def test_a_raising_pipeline_rearms_the_event_instead_of_burying_it(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """
    An exception must leave a record AND leave the event retriable. Razorpay
    already got its 200, so an event marked processed=True here is a payment
    nobody will ever look at again — the old code did exactly that, and one
    transient database blip permanently dropped a real payment failure.
    """
    async with db_sessionmaker() as session:
        session.add(
            WebhookEvent(
                razorpay_event_id="evt_boom",
                event_type="payment.failed",
                payload=sample_webhook_payload,
            )
        )
        await session.commit()

    async def boom(event: Any, session: Any) -> None:
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(router_mod, "async_session_factory", db_sessionmaker)
    monkeypatch.setattr("src.orchestrator.process_payment_failure", boom)
    await router_mod._process_event_background("evt_boom", "payment.failed", sample_webhook_payload)

    async with db_sessionmaker() as reader:
        event = (
            await reader.execute(
                select(WebhookEvent).where(WebhookEvent.razorpay_event_id == "evt_boom")
            )
        ).scalar_one()
    assert event.processed is False, "a first failure must stay visible to the reconciler"
    assert event.processing_attempts == 1
    assert event.processing_error is not None, "the failure left no trace"


async def test_a_persistently_raising_pipeline_rests_after_the_cap(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """
    A payload that raises on EVERY attempt must stop eating sweep batches:
    after the cap it rests with processed=True and the error recorded.
    """
    async with db_sessionmaker() as session:
        session.add(
            WebhookEvent(
                razorpay_event_id="evt_persistent_boom",
                event_type="payment.failed",
                payload=sample_webhook_payload,
            )
        )
        await session.commit()

    async def boom(event: Any, session: Any) -> None:
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(router_mod, "async_session_factory", db_sessionmaker)
    monkeypatch.setattr("src.orchestrator.process_payment_failure", boom)
    for _ in range(router_mod.EVENT_RECONCILE_MAX_ATTEMPTS):
        await router_mod._process_event_background(
            "evt_persistent_boom", "payment.failed", sample_webhook_payload
        )

    async with db_sessionmaker() as reader:
        event = (
            await reader.execute(
                select(WebhookEvent).where(
                    WebhookEvent.razorpay_event_id == "evt_persistent_boom"
                )
            )
        ).scalar_one()
    assert event.processed is True, "the cap must eventually stop the retries"
    assert event.processing_attempts == router_mod.EVENT_RECONCILE_MAX_ATTEMPTS
    assert event.processing_error is not None


async def test_a_successful_capture_is_marked_processed(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    Most captures match no case (the common path). They must still be marked
    processed, or the reconciler re-attributes them on every tick.
    """
    case_id = await _seed(db_sessionmaker)
    payload = _captured_payload("pay_stranger_2", link_id="plink_stranger_2")
    event_id = f"payment.captured_x_{payload['created_at']}"

    async with db_sessionmaker() as session:
        session.add(
            WebhookEvent(
                razorpay_event_id=event_id, event_type="payment.captured", payload=payload
            )
        )
        await session.commit()

    await _run(monkeypatch, db_sessionmaker, payload)

    async with db_sessionmaker() as reader:
        event = (
            await reader.execute(
                select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event_id)
            )
        ).scalar_one()
        case = await reader.get(RecoveryCase, case_id)
    assert event.processed is True
    assert case is not None and case.amount_recovered == 0
