"""
Tests for the worker that makes deferred decisions actually happen.

The headline regression: `retry_at` used to reach the executor, which maps it
onto the same `_create_payment_link` as `retry_now` — so "retry in 4 hours"
created the payment link immediately and `scheduled_at` was written and read by
nothing. Two things have to hold now: the decision to wait must NOT call
Razorpay, and the wait must actually end.

The second theme is re-validation. A deferred retry outlives its own guardrail
check by hours, and the checks that can flip in that window (case closed,
consent withdrawn, blackout hour) must be re-read at fire time rather than
trusted from decision time.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import scheduler
from src.agent.actions import ActionType, FailureContext, RetryAction
from src.cases import record_opt_out
from src.guardrail.gate import GuardrailResult
from src.models import CaseEvent, RecoveryCase, RetryAttempt, WebhookEvent
from src.orchestrator import PaymentRecoveryOrchestrator

PAYMENT = "pay_test_abc123"  # the id inside sample_webhook_payload


class _FixedAgent:
    def __init__(self, action: ActionType, retry_at: datetime | None = None) -> None:
        self.fallback_count = 0
        self._action = action
        self._retry_at = retry_at

    async def decide(self, context: FailureContext) -> RetryAction:
        return RetryAction(
            action=self._action,
            retry_at=self._retry_at,
            reason="fixed test decision",
            confidence=0.9,
        )


def _orchestrator(
    monkeypatch: Any,
    action: ActionType,
    *,
    retry_at: datetime | None = None,
    passed: bool = True,
) -> PaymentRecoveryOrchestrator:
    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(orch, "_get_agent", lambda: _FixedAgent(action, retry_at))
    # Scheduler tests pin the parking/fire mechanics, not the blackout clamp
    # (which has its own suite). Unclamped: +4h from the live clock crosses
    # into the IST window on some runs and the parked row moves past the fire
    # probe — a time-of-day flake, fixed by identity-clamping here.
    monkeypatch.setattr(
        "src.orchestrator.clamp_retry_at_out_of_blackout", lambda dt: dt
    )
    # Pinned: the real gate reads the wall clock for the IST retry blackout, so
    # an unpinned test passes or fails depending on the hour CI runs.
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


async def _attempts(sm: async_sessionmaker[AsyncSession]) -> list[RetryAttempt]:
    async with sm() as reader:
        rows = await reader.execute(
            select(RetryAttempt).where(RetryAttempt.payment_id == PAYMENT)
        )
        return list(rows.scalars().all())


# ── retry_at defers instead of firing ────────────────────────────────────


async def test_retry_at_parks_the_row_and_never_calls_razorpay(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """The bug: retry_at used to create the payment link immediately."""
    due = datetime.now(UTC) + timedelta(hours=4)
    orch = _orchestrator(monkeypatch, "retry_at", retry_at=due)
    calls: list[dict[str, Any]] = []

    async def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True, "payment_link_id": "plink_should_not_exist"}

    monkeypatch.setattr(orch._executor, "execute_retry", spy)
    await _ingest(orch, db_sessionmaker, sample_webhook_payload, "evt_sched_1")

    assert calls == [], "retry_at called Razorpay at decision time"
    rows = await _attempts(db_sessionmaker)
    assert len(rows) == 1
    assert rows[0].result == "scheduled"
    assert rows[0].scheduled_at is not None
    assert rows[0].external_ref is None


async def test_a_scheduled_retry_stays_put_until_its_time(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    due = datetime.now(UTC) + timedelta(hours=4)
    orch = _orchestrator(monkeypatch, "retry_at", retry_at=due)
    monkeypatch.setattr(
        orch._executor, "execute_retry", lambda **k: {"success": True}
    )
    await _ingest(orch, db_sessionmaker, sample_webhook_payload, "evt_sched_2")

    async with db_sessionmaker() as session:
        assert await scheduler.fire_due_retries(session) == 0
    assert (await _attempts(db_sessionmaker))[0].result == "scheduled"


async def test_the_wait_actually_ends(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    due = datetime.now(UTC) + timedelta(hours=4)
    orch = _orchestrator(monkeypatch, "retry_at", retry_at=due)
    monkeypatch.setattr(
        orch._executor, "execute_retry", lambda **k: {"success": True}
    )
    await _ingest(orch, db_sessionmaker, sample_webhook_payload, "evt_sched_3")

    fired: list[dict[str, Any]] = []

    async def spy(**kwargs: Any) -> dict[str, Any]:
        fired.append(kwargs)
        return {"success": True, "payment_link_id": "plink_fired_ok"}

    live = _orchestrator(monkeypatch, "retry_now")
    monkeypatch.setattr(live._executor, "execute_retry", spy)
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: live)

    async with db_sessionmaker() as session:
        assert await scheduler.fire_due_retries(session, now=due + timedelta(minutes=1)) == 1

    assert len(fired) == 1
    # Same key as the parked row: the wait does not mint a new attempt slot.
    assert fired[0]["idempotency_key"] == "retry_pay_test_abc123_0"
    rows = await _attempts(db_sessionmaker)
    assert len(rows) == 1, "firing duplicated the attempt instead of updating it"
    assert rows[0].result == "success"
    assert rows[0].external_ref == "plink_fired_ok"


async def test_firing_twice_does_not_charge_twice(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """The claim is a conditional UPDATE; a second sweep must find nothing."""
    due = datetime.now(UTC) + timedelta(hours=4)
    orch = _orchestrator(monkeypatch, "retry_at", retry_at=due)
    monkeypatch.setattr(orch._executor, "execute_retry", lambda **k: {"success": True})
    await _ingest(orch, db_sessionmaker, sample_webhook_payload, "evt_sched_4")

    calls = 0

    async def spy(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"success": True, "payment_link_id": "plink_once"}

    live = _orchestrator(monkeypatch, "retry_now")
    monkeypatch.setattr(live._executor, "execute_retry", spy)
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: live)

    later = due + timedelta(minutes=1)
    async with db_sessionmaker() as session:
        await scheduler.fire_due_retries(session, now=later)
    async with db_sessionmaker() as session:
        assert await scheduler.fire_due_retries(session, now=later) == 0

    assert calls == 1


# ── re-validation at fire time ───────────────────────────────────────────


async def test_an_opt_out_during_the_wait_cancels_the_retry(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """
    The reason a deferred retry re-validates at all. Consent granted at 22:00 and
    withdrawn at 23:00 must stop the 02:00 retry — the decision-time check cannot
    know that, and firing anyway is the contact a complaint is about.
    """
    due = datetime.now(UTC) + timedelta(hours=4)
    orch = _orchestrator(monkeypatch, "retry_at", retry_at=due)
    monkeypatch.setattr(orch._executor, "execute_retry", lambda **k: {"success": True})
    await _ingest(orch, db_sessionmaker, sample_webhook_payload, "evt_sched_5")

    async with db_sessionmaker() as session:
        assert await record_opt_out(session, "test@example.com") >= 1
        await session.commit()

    calls: list[Any] = []

    async def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True}

    live = _orchestrator(monkeypatch, "retry_now")
    monkeypatch.setattr(live._executor, "execute_retry", spy)
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: live)

    async with db_sessionmaker() as session:
        assert await scheduler.fire_due_retries(session, now=due + timedelta(minutes=1)) == 0

    assert calls == [], "fired a retry at a customer who had opted out"
    rows = await _attempts(db_sessionmaker)
    assert rows[0].result == "cancelled"
    # record_opt_out closes the case outright, so the case-state check is what
    # catches it — an opt-out is a stop, not a skipped contact.
    assert "opted_out" in (rows[0].result_details or {})["scheduler"]


async def test_a_blackout_hour_at_fire_time_rejects_the_retry(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """Approved at decision time, refused at fire time — the guardrail re-runs."""
    due = datetime.now(UTC) + timedelta(hours=4)
    orch = _orchestrator(monkeypatch, "retry_at", retry_at=due)
    monkeypatch.setattr(orch._executor, "execute_retry", lambda **k: {"success": True})
    await _ingest(orch, db_sessionmaker, sample_webhook_payload, "evt_sched_6")

    calls: list[Any] = []

    async def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True}

    live = _orchestrator(monkeypatch, "retry_now", passed=False)
    monkeypatch.setattr(live._executor, "execute_retry", spy)
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: live)

    async with db_sessionmaker() as session:
        assert await scheduler.fire_due_retries(session, now=due + timedelta(minutes=1)) == 0

    assert calls == []
    assert (await _attempts(db_sessionmaker))[0].result == "rejected"


# ── dropped BackgroundTasks ──────────────────────────────────────────────


async def test_a_dropped_event_is_reprocessed(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """
    The router commits the event then hands processing to BackgroundTasks. A
    restart in that window loses the task; Razorpay never re-sends, because we
    already answered 200. Nothing but this sweep would ever notice.
    """
    orch = _orchestrator(monkeypatch, "retry_now")
    monkeypatch.setattr(
        orch._executor, "execute_retry", lambda **k: {"success": True, "payment_link_id": "pl_1"}
    )
    monkeypatch.setattr(scheduler, "get_orchestrator", lambda: orch)
    monkeypatch.setattr("src.orchestrator.get_orchestrator", lambda: orch)

    async with db_sessionmaker() as session:
        session.add(
            WebhookEvent(
                razorpay_event_id="evt_dropped_1",
                event_type="payment.failed",
                payload=sample_webhook_payload,
                received_at=datetime.now(UTC) - timedelta(hours=1),
                processed=False,
            )
        )
        await session.commit()

    async with db_sessionmaker() as session:
        assert await scheduler.reconcile_events(session) == 1
        await session.commit()

    assert len(await _attempts(db_sessionmaker)) == 1
    async with db_sessionmaker() as reader:
        event = (
            await reader.execute(
                select(WebhookEvent).where(WebhookEvent.razorpay_event_id == "evt_dropped_1")
            )
        ).scalar_one()
        assert event.processed is True


async def test_a_still_fresh_event_is_left_to_its_background_task(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
) -> None:
    """The age threshold is what stops the sweep racing work already in flight."""
    async with db_sessionmaker() as session:
        session.add(
            WebhookEvent(
                razorpay_event_id="evt_fresh_1",
                event_type="payment.failed",
                payload=sample_webhook_payload,
                received_at=datetime.now(UTC),
                processed=False,
            )
        )
        await session.commit()
        assert await scheduler.reconcile_events(session) == 0


async def test_a_dropped_capture_is_reattributed(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    The sweep used to reconcile payment.failed only — a dropped capture was
    money that arrived and was never attributed, on a case that kept chasing a
    customer who had already paid. Captures are reconciled now too.
    """
    from src.cases import attach_attempt, open_case
    from src.models import PaymentFailure

    original, link, captured = "pay_caprecon_1", "plink_caprecon_1", "pay_caprecon_new_9"
    async with db_sessionmaker() as session:
        failure = PaymentFailure(
            payment_id=original, order_id="order_caprecon_1", amount=50000,
            method="card", error_code="BAD_REQUEST_ERROR",
            failure_class="insufficient_funds", is_retryable=True,
            webhook_event_id=uuid.uuid4(), failed_at=datetime.now(UTC),
        )
        session.add(failure)
        await session.flush()
        case = await open_case(
            session, risk_type="payment_failure", subject_ref=original,
            amount_at_risk=50000, customer_id="caprecon@example.com",
        )
        attempt = RetryAttempt(
            payment_failure_id=failure.id, payment_id=original,
            idempotency_key=f"retry_{original}_0", attempt_number=1,
            action_type="retry_now", agent_type="xgboost",
            guardrail_passed=True, result="pending",
        )
        attach_attempt(case, attempt, external_ref=link)
        session.add(attempt)

        payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {"entity": {"id": captured, "amount": 50000}},
                "payment_link": {"entity": {"id": link}},
            },
        }
        session.add(
            WebhookEvent(
                razorpay_event_id="evt_dropped_capture_1",
                event_type="payment.captured",
                payload=payload,
                received_at=datetime.now(UTC) - timedelta(hours=1),
                processed=False,
            )
        )
        await session.commit()
        case_id = case.id

    async with db_sessionmaker() as session:
        assert await scheduler.reconcile_events(session) == 1
        await session.commit()

    async with db_sessionmaker() as reader:
        case = await reader.get(RecoveryCase, case_id)
        event = (
            await reader.execute(
                select(WebhookEvent).where(
                    WebhookEvent.razorpay_event_id == "evt_dropped_capture_1"
                )
            )
        ).scalar_one()
    assert case is not None
    assert case.state == "recovered", "the dropped capture was never attributed"
    assert case.amount_recovered == 50000
    assert event.processed is True


# ── the tick ─────────────────────────────────────────────────────────────


async def test_tick_reports_what_each_sweep_did(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        assert await scheduler.tick(session) == {
            "retries_fired": 0,
            "events_reconciled": 0,
            "risk_events_reconciled": 0,
            "attempts_reconciled": 0,
            "links_cancelled": 0,
            "superseded_links_cancelled": 0,
            "promises_expired": 0,
            "promises_reminded": 0,
            "cases_chased": 0,
            "accounts_consolidated": 0,
            "due_cases_reported": 0,
            "plans_reconciled": 0,
            "alerts_delivered": 0,
        }


# ── write-ahead attempts whose outcome never landed ──────────────────────


async def _insert_pending(
    sm: async_sessionmaker[AsyncSession], key: str, age_seconds: int
) -> RetryAttempt:
    from src.cases import open_case

    async with sm() as session:
        case = await open_case(
            session, risk_type="payment_failure", subject_ref=f"pay_{key}",
            amount_at_risk=50000, customer_id="c@example.com",
        )
        attempt = RetryAttempt(
            payment_id=f"pay_{key}",
            idempotency_key=key,
            attempt_number=1,
            action_type="retry_now",
            guardrail_passed=True,
            result="pending",
            created_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        )
        attempt.recovery_case_id = case.id
        session.add(attempt)
        await session.commit()
        return attempt


async def test_a_stale_pending_attempt_is_resolved_failed(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    The write-ahead commit exists so a crash mid-Razorpay-call leaves a
    recorded unknown instead of a silent gap. Nothing resolved that unknown:
    the row sat 'pending' forever and the dashboard read 'in flight' for a
    payment nobody was looking at.
    """
    await _insert_pending(db_sessionmaker, "retry_stale_1", age_seconds=10_000)

    async with db_sessionmaker() as session:
        assert await scheduler.reconcile_stale_attempts(session) == 1
        await session.commit()

    async with db_sessionmaker() as reader:
        row = (
            await reader.execute(
                select(RetryAttempt).where(RetryAttempt.idempotency_key == "retry_stale_1")
            )
        ).scalar_one()
        events = list(
            (await reader.execute(
                select(CaseEvent.event_type).order_by(CaseEvent.id)
            )).scalars()
        )
    assert row.result == "failed"
    assert "stale-pending" in str(row.result_details)
    assert "reconciled" in events, "the resolution left no trace in the audit trail"


async def test_a_fresh_pending_attempt_is_left_alone(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The age threshold is what stops the sweep racing a live Razorpay call."""
    await _insert_pending(db_sessionmaker, "retry_fresh_1", age_seconds=5)

    async with db_sessionmaker() as session:
        assert await scheduler.reconcile_stale_attempts(session) == 0
        await session.commit()

    async with db_sessionmaker() as reader:
        row = (
            await reader.execute(
                select(RetryAttempt).where(RetryAttempt.idempotency_key == "retry_fresh_1")
            )
        ).scalar_one()
    assert row.result == "pending"


async def test_a_resolved_attempt_is_not_resolved_twice(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_pending(db_sessionmaker, "retry_once_1", age_seconds=10_000)

    async with db_sessionmaker() as session:
        assert await scheduler.reconcile_stale_attempts(session) == 1
        await session.commit()
    async with db_sessionmaker() as session:
        assert await scheduler.reconcile_stale_attempts(session) == 0
        await session.commit()

    async with db_sessionmaker() as reader:
        row = (
            await reader.execute(
                select(RetryAttempt).where(RetryAttempt.idempotency_key == "retry_once_1")
            )
        ).scalar_one()
    assert row.result == "failed"


async def test_deferring_is_recorded_in_the_audit_trail(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    due = datetime.now(UTC) + timedelta(hours=4)
    orch = _orchestrator(monkeypatch, "retry_at", retry_at=due)
    monkeypatch.setattr(orch._executor, "execute_retry", lambda **k: {"success": True})
    await _ingest(orch, db_sessionmaker, sample_webhook_payload, "evt_sched_audit")

    async with db_sessionmaker() as reader:
        case = (await reader.execute(select(RecoveryCase))).scalar_one()
        kinds = [
            row
            for (row,) in (
                await reader.execute(
                    select(CaseEvent.event_type)
                    .where(CaseEvent.recovery_case_id == case.id)
                    .order_by(CaseEvent.id)
                )
            ).all()
        ]
    assert kinds == ["opened", "deferred"]
