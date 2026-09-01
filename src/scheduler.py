"""
Background worker — the thing that makes deferred decisions actually happen.

Nine sweeps, one tick:

    fire_due_retries()            an agent said "retry in 4 hours"; four hours have passed
    reconcile_events()            a webhook was stored but its background task never ran
    reconcile_risk_events()       a merchant risk event was stored but its task never ran
    reconcile_stale_attempts()    a write-ahead attempt was committed but its outcome never landed
    cancel_links_for_closed_cases()  links of finished cases die with them
    cancel_superseded_links()        an open case keeps only its newest link
    expire_promises()             a promise-to-pay came due (+grace) with no money (src/cases.py)
    remind_promises()             a pending promise inside the 48h pre-due window gets its one
                                  reminder — a real, budgeted contact through the chase pipeline
    chase_due_cases()             a chaser-driven case whose wait elapsed (cart, subscription,
                                  invoice, mandate — the risk types with no inbound webhook)
    chase_due_accounts()          the B2B layer: one consolidated statement per buyer
                                  account, staged by aging, inside the same bounds
    report_due_cases()            payment-failure cases whose wait elapsed with nothing to do

Why this exists at all: `retry_at` was an advertised action that never took
effect. The executor maps it onto the same `_create_payment_link` as
`retry_now`, so the agent's decision to WAIT was executed immediately and
`retry_attempts.scheduled_at` was written and never read by anything. A guardrail
that approves "not yet" and a system that does it now is worse than no timing at
all, because the audit trail records a delay that never happened.

An asyncio task inside the FastAPI process, not Celery or APScheduler. It needs
no broker, no second deployment and no new dependency, and at the volume a
single Razorpay account produces, one poll a minute over an indexed column is
not the bottleneck. Swap in a real queue when there is more than one app
process, or when the tick has to be seconds rather than minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.actions import RetryAction
from src.cases import (
    due_cases,
    expire_promises,
    log_event,
    promises_due_for_reminder,
)
from src.chasers.policy import RISK_POLICIES
from src.config import get_settings
from src.database import async_session_factory
from src.ingestion.router import attribute_captured_payload, rearm
from src.models import (
    PaymentFailure,
    RecoveryCase,
    RetryAttempt,
    RiskEvent,
    SchedulerHeartbeat,
    WebhookEvent,
)
from src.orchestrator import (
    PaymentRecoveryOrchestrator,
    get_orchestrator,
    process_payment_failure,
    process_risk_event,
)
from src.receivables.ladder import is_b2b_contact_time
from src.receivables.models import CaseDispute

logger = logging.getLogger(__name__)

# How many times reconcile_events will re-arm an event that keeps raising
# before it gives up and leaves the error recorded. A transient database blip
# must not permanently skip a real payment failure; a payload that raises on
# every tick must also not eat the whole sweep batch forever. Three is the
# compromise: enough to survive a bad minute, few enough to starve nothing.
# Defined in src/ingestion/router.py because the first-pass background task
# re-arms under the same cap — one number, one meaning.

# Fraction of one tick interval fire_due_retries may spend before yielding
# back to the loop. Each fire can hold a Razorpay call for up to the executor
# timeout, so an unbounded sweep of 50 due rows could run ~8 minutes against a
# 60s interval — exactly when Razorpay is slow, i.e. when retries matter most.
# The budget caps the overrun; whatever does not fit waits for the next tick
# (the rows stay "scheduled" and the indexed query re-finds them).
_FIRE_TIME_BUDGET_FRACTION = 0.8


def _aware(ts: datetime) -> datetime:
    """
    Force a timestamp to UTC-aware before it meets datetime.now(UTC).

    Postgres hands back aware values; the SQLite test harness hands back naive
    wall clocks. The chase path compares a case's next_action_at against the
    tick's clock on every sweep, so it gets the same coercion every other
    boundary in this codebase applies instead of a TypeError in tests.
    """
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


async def fire_due_retries(session: AsyncSession, *, now: datetime | None = None) -> int:
    """
    Execute every attempt whose scheduled time has arrived. Returns how many ran.

    The guardrail runs AGAIN here, against the clock at fire time rather than at
    decision time. That is the whole reason a deferred retry is not just a
    delayed call: in the hours since the agent decided, the customer may have
    opted out, the consent window may have closed, the attempt budget may have
    been spent by another path, and 03:00 IST is inside the retry blackout that
    22:00 was not. Re-validating is cheap; discovering it from a chargeback is
    not.
    """
    now = now or datetime.now(UTC)
    orchestrator = get_orchestrator()

    due = await session.execute(
        select(RetryAttempt)
        .where(
            RetryAttempt.result == "scheduled",
            RetryAttempt.scheduled_at.is_not(None),
            RetryAttempt.scheduled_at <= now,
        )
        .order_by(RetryAttempt.scheduled_at)
        .limit(get_settings().scheduler_batch_size)
    )

    fired = 0
    # The time budget: stop claiming when this tick has spent its share of the
    # interval. Without it, a slow-Razorpay day makes each tick run into the
    # next and the backlog compounds exactly when retries matter most.
    budget_seconds = (
        get_settings().scheduler_interval_seconds * _FIRE_TIME_BUDGET_FRACTION
    )
    started = time.monotonic()

    for attempt in due.scalars().all():
        if time.monotonic() - started > budget_seconds:
            logger.info(
                "Fire sweep hit its time budget (%.1fs) with rows left due — "
                "deferring them to the next tick",
                budget_seconds,
            )
            break
        # Claim the row before doing anything with it. A conditional UPDATE
        # rather than SELECT ... FOR UPDATE SKIP LOCKED because the test harness
        # runs on SQLite, which has neither — and this is portable, atomic, and
        # exactly as correct: whoever flips "scheduled" to "pending" owns the
        # attempt, and the loser's rowcount is 0.
        #
        # The claim marker records WHO claimed and WHEN. It is the boundary the
        # stale sweep reads: a row still carrying only this marker crashed
        # before the write-ahead phase, which means Razorpay was never called —
        # safe to re-park rather than fail-closed (see reconcile_stale_attempts).
        claimed = await session.execute(
            update(RetryAttempt)
            .where(RetryAttempt.id == attempt.id, RetryAttempt.result == "scheduled")
            .values(
                result="pending",
                result_details={
                    "claimed_by": "scheduler",
                    "claimed_at": now.isoformat(),
                },
            )
        )
        # CursorResult, not Result — rowcount is the whole point of the claim,
        # and the base Result type does not declare it.
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            logger.debug("Attempt %s claimed by another worker", attempt.id)
            continue
        await session.commit()

        if await _fire_one(orchestrator, attempt, session, now=now):
            fired += 1

    return fired


async def _fire_one(
    orchestrator: PaymentRecoveryOrchestrator,
    attempt: RetryAttempt,
    session: AsyncSession,
    *,
    now: datetime,
) -> bool:
    """Re-validate one claimed attempt and execute it. True if Razorpay was called."""
    if attempt.payment_failure_id is None:
        # No payment behind this attempt — it belongs to a chaser-driven case
        # (cart, subscription, invoice, mandate) whose agent parked a retry_at.
        # Fire it through the case path instead.
        return await _fire_case_attempt(orchestrator, attempt, session, now=now)

    failure = await session.get(PaymentFailure, attempt.payment_failure_id)
    if failure is None:  # pragma: no cover — FK-less orphan
        await _mark(session, attempt, "skipped", "payment_failure row is gone")
        return False

    case = (
        await session.get(RecoveryCase, attempt.recovery_case_id)
        if attempt.recovery_case_id
        else None
    )

    # ── Re-validate against the clock at fire time ────────────────────────
    if case is not None:
        ledger = await orchestrator._get_ledger(case.customer_id, session)
        # Deliberately NOT stop_reason(). Two of its three rules are wrong here:
        # the attempt budget was already spent when this attempt was created, so
        # re-checking it rejects exactly the retry that filled the last slot —
        # the one the agent most deliberately chose to wait for — and
        # next_action_at IS this scheduled action, so a defer check would cancel
        # every retry the moment it came due. What genuinely can change in four
        # hours is whether the case is still open and whether consent still
        # holds, and those are what get re-read.
        stop: str | None = None
        if case.state != "open":
            stop = f"case is {case.state}: {case.close_reason or 'no reason recorded'}"
        elif ledger is not None and ledger.consent_status == "opted_out":
            stop = "customer opted out of contact"
        if stop is not None:
            await _mark(session, attempt, "cancelled", stop)
            log_event(session, case, "stopped", reason=stop, at="fire_time", actor="scheduler")
            await session.commit()
            logger.info("Scheduled retry cancelled: %s — %s", attempt.idempotency_key, stop)
            return False

    context = await orchestrator._build_failure_context(failure, session)
    action = RetryAction(
        action=_fired_action_type(attempt),  # type: ignore[arg-type]
        rail=attempt.target_rail,  # type: ignore[arg-type]
        reason=attempt.agent_reasoning or "scheduled retry",
        confidence=attempt.agent_confidence,
    )
    guardrail = orchestrator._guardrail.validate(
        action, context, attempt.idempotency_key, attempt.attempt_number - 1
    )
    if not guardrail.passed:
        reason = "; ".join(guardrail.rejection_reasons)
        await _mark(session, attempt, "rejected", reason)
        if case is not None:
            log_event(
                session, case, "stopped", reason=reason, at="fire_time", actor="scheduler"
            )
        await session.commit()
        logger.info("Scheduled retry rejected at fire time: %s — %s", attempt.id, reason)
        return False

    if case is None:  # pragma: no cover — pre-case rows only
        await _mark(session, attempt, "skipped", "no recovery case")
        return False

    # The row is already committed as "pending" by the claim above, which is
    # the write-ahead this path needs — so _execute_and_record's own commit is
    # a no-op re-assertion rather than the first durable record.
    await orchestrator._execute_and_record(
        attempt=attempt,
        case=case,
        failure_record=failure,
        action=action,
        idem_key=attempt.idempotency_key,
        actor="scheduler",
        session=session,
    )
    await session.commit()
    logger.info(
        "Scheduled retry fired: payment=%s key=%s result=%s (due %s, now %s)",
        failure.payment_id,
        attempt.idempotency_key,
        attempt.result,
        attempt.scheduled_at.isoformat() if attempt.scheduled_at else "?",
        now.isoformat(),
    )
    return True


def _fired_action_type(attempt: RetryAttempt) -> str:
    """
    The action a parked attempt fires as — read from the row, not inferred.

    Both fire paths used to reconstruct this as
    ``"switch_rail" if attempt.target_rail else "retry_now"``, which couples
    two unrelated facts: what the action IS, and whether it happens to carry
    a rail preference. `retry_attempts.action_type` records the first one
    directly and was simply ignored.

    The coupling is not cosmetic — the guardrail keys several rules on the
    action type. `check_mandate_predebit_notification` guards `retry_now`
    only, so stamping a rail onto a mandate's parked retry re-labelled it
    `switch_rail` and walked it straight past the RBI pre-debit notice. Rules
    that must fire have to see the real action.

    `retry_at` maps to `retry_now` because the wait it asked for is over —
    re-sending `retry_at` would park the row right back where it came from.
    """
    return "retry_now" if attempt.action_type == "retry_at" else attempt.action_type


async def _mark(
    session: AsyncSession, attempt: RetryAttempt, result: str, reason: str
) -> None:
    attempt.result = result
    attempt.executed_at = datetime.now(UTC)
    attempt.result_details = {"scheduler": reason}
    session.add(attempt)


def _advance_ladder(
    session: AsyncSession,
    case: RecoveryCase,
    policy: Any,
    *,
    now: datetime,
    actor: str,
) -> None:
    """
    Move a chaser case to its next rung after a fired attempt resolves.

    Same floor as chase_case's own tail — a re_chase_hours gap so the due-case
    sweep does not re-chase immediately, and a spent budget closes the case
    rather than leaving it open with a stale next_action_at for the sweep to
    knock on forever.

    Its own function because BOTH outcomes need it. It used to sit inline
    after the execute call, so every early return above it — a guardrail
    rejection most of all — left the case due at an instant already in the
    past, and the next tick chased it again with no gap at all.
    """
    if case.state != "open":
        return
    if case.attempts_used >= case.max_attempts:
        from src.cases import close_case

        close_case(
            case, "exhausted",
            f"attempt budget spent ({case.attempts_used}/{case.max_attempts})",
        )
        log_event(
            session, case, "closed", actor=actor,
            state="exhausted", reason=case.close_reason,
        )
        return
    next_at = case.next_action_at
    if next_at is None or _aware(next_at) <= now:
        case.next_action_at = now + timedelta(hours=policy.re_chase_hours)


async def _fire_case_attempt(
    orchestrator: PaymentRecoveryOrchestrator,
    attempt: RetryAttempt,
    session: AsyncSession,
    *,
    now: datetime,
) -> bool:
    """
    Fire a scheduled attempt belonging to a chaser-driven case (no payment
    behind it). Same fire-time discipline as the payment rail: re-validate the
    case and consent against the clock at fire time, re-run the guardrail,
    then execute. The wait the agent asked for is over; what can have changed
    in the meantime is whether the case is still open and consent still holds.
    """
    from src.chasers.policy import policy_for

    case = (
        await session.get(RecoveryCase, attempt.recovery_case_id)
        if attempt.recovery_case_id
        else None
    )
    if case is None:
        await _mark(session, attempt, "skipped", "no recovery case")
        await session.commit()
        return False

    policy = policy_for(case.risk_type)
    if policy is None:
        await _mark(session, attempt, "skipped", f"no chase policy for {case.risk_type}")
        await session.commit()
        return False

    # Re-validate against the clock at fire time — the same two facts that can
    # genuinely change while a retry waits (case still open, consent still
    # held), and deliberately not the budget/defer rules for the same reasons
    # as the payment path above.
    ledger = await orchestrator._get_ledger(case.customer_id, session)
    stop: str | None = None
    if case.state != "open":
        stop = f"case is {case.state}: {case.close_reason or 'no reason recorded'}"
    elif ledger is not None and ledger.consent_status == "opted_out":
        stop = "customer opted out of contact"
    if stop is not None:
        await _mark(session, attempt, "cancelled", stop)
        log_event(session, case, "stopped", reason=stop, at="fire_time", actor="scheduler")
        await session.commit()
        logger.info("Scheduled case retry cancelled: %s — %s", attempt.idempotency_key, stop)
        return False

    event = await orchestrator._latest_risk_event(case, session)
    meta = event.meta if event is not None else None
    context = await orchestrator._build_case_context(case, policy, session, now=now, meta=meta)

    action = RetryAction(
        action=_fired_action_type(attempt),  # type: ignore[arg-type]
        rail=attempt.target_rail,  # type: ignore[arg-type]
        reason=attempt.agent_reasoning or "scheduled case retry",
        confidence=attempt.agent_confidence,
    )
    guardrail = orchestrator._guardrail.validate(
        action, context, attempt.idempotency_key, attempt.attempt_number - 1
    )
    if not guardrail.passed:
        reason = "; ".join(guardrail.rejection_reasons)
        await _mark(session, attempt, "rejected", reason)
        log_event(session, case, "stopped", reason=reason, at="fire_time", actor="scheduler")
        # The ladder advances on a rejection too. Returning here without it
        # left next_action_at at the fired instant — already in the past — so
        # the due-case sweep re-chased the case on the very next tick and the
        # policy's re_chase_hours floor was silently skipped. The slot the
        # attempt spent is NOT refunded: chase_case counts guardrail
        # rejections on purpose, so a case that keeps tripping the gate runs
        # out of budget instead of looping through the agent forever.
        _advance_ladder(session, case, policy, now=now, actor="scheduler")
        await session.commit()
        logger.info("Scheduled case retry rejected at fire time: %s — %s", attempt.id, reason)
        return False

    # The row is already committed as "pending" by the claim in
    # fire_due_retries, which is the write-ahead this path needs.
    await orchestrator._execute_case_and_record(
        attempt=attempt,
        case=case,
        policy=policy,
        action=action,
        idem_key=attempt.idempotency_key,
        actor="scheduler",
        session=session,
        customer_email=event.customer_email if event is not None else None,
        customer_contact=event.customer_contact if event is not None else None,
    )

    _advance_ladder(session, case, policy, now=now, actor="scheduler")

    await session.commit()
    logger.info(
        "Scheduled case retry fired: case=%s key=%s result=%s",
        case.id, attempt.idempotency_key, attempt.result,
    )
    return True


async def _reconcile_sweep(
    session: AsyncSession,
    *,
    model: type[Any],
    lookup_col: Any,
    stale_where: Sequence[Any],
    handle: Any,
    event_ref: Any,
    label: str,
    now: datetime,
) -> int:
    """
    The shared body of both reconcile sweeps: find stale unprocessed rows,
    claim each atomically (whoever flips processed owns it — with
    WEB_CONCURRENCY > 1 two schedulers can select the same row in the same
    second; the idempotency key makes duplicate processing safe but wasted),
    run the handler, and on failure re-arm under the shared cap.

    The age threshold is what keeps this from racing the in-flight background
    task that is still legitimately running.
    """
    settings = get_settings()
    cutoff = now - timedelta(seconds=settings.event_reconcile_after_seconds)

    stale = await session.execute(
        select(model)
        .where(model.processed.is_(False), model.received_at <= cutoff, *stale_where)
        .order_by(model.received_at)
        .limit(settings.scheduler_batch_size)
    )

    recovered = 0
    for event in stale.scalars().all():
        claimed = await session.execute(
            update(model)
            .where(model.id == event.id, model.processed.is_(False))
            .values(processed=True)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            logger.debug("%s claimed by another worker", event_ref(event))
            continue
        await session.commit()

        ref = event_ref(event)
        try:
            await handle(event, session)
            await session.commit()
            recovered += 1
            logger.info("Reconciled dropped %s: %s", label, ref)
        except Exception:
            logger.exception("Reconcile failed for %s %s", label, ref)
            await session.rollback()
            await rearm(
                session,
                model=model,
                lookup_col=lookup_col,
                id_col=model.id,
                event_id=ref,
                label=label.capitalize(),
                context="Reconcile attempt",
            )
    return recovered


async def reconcile_events(session: AsyncSession, *, now: datetime | None = None) -> int:
    """
    Re-run webhook events whose background task never finished. Returns how many.

    FastAPI's BackgroundTasks run in the request's process after the response is
    sent. Restart, crash, or deploy in that window and the task is simply gone —
    the event is durably stored (the router commits before returning 200, on
    purpose) and `processed` stays False forever. Razorpay will not re-send it:
    we already said 200. Without this sweep every such payment silently gets no
    recovery attempt at all, and nothing in the system reports a gap.

    Both money-bearing event types are covered. payment.failed is obvious;
    payment.captured matters MORE: a dropped capture is money that arrived and
    was never attributed, on a case that keeps chasing a customer who already
    paid. The first-pass handler re-arms its failures under the same cap, so
    an event lands here only if that task died before it could even try.

    Transient failures do not consume the event: it is RE-ARMED until the
    shared cap, then rests with the error recorded — a database blip no
    longer loses a payment, and a deterministically-broken payload stops
    eating the batch.
    """
    now = now or datetime.now(UTC)

    async def handle(event: WebhookEvent, session: AsyncSession) -> None:
        if event.event_type == "payment.failed":
            await process_payment_failure(event, session)
        else:
            await attribute_captured_payload(session, event.payload)

    return await _reconcile_sweep(
        session,
        model=WebhookEvent,
        lookup_col=WebhookEvent.razorpay_event_id,
        stale_where=[WebhookEvent.event_type.in_(["payment.failed", "payment.captured"])],
        handle=handle,
        event_ref=lambda e: e.razorpay_event_id,
        label="event",
        now=now,
    )


async def reconcile_stale_attempts(
    session: AsyncSession, *, now: datetime | None = None
) -> int:
    """
    Resolve write-ahead attempts whose outcome never landed. Returns how many.

    A pending attempt is the intent log the money path depends on: it is
    committed BEFORE Razorpay is called precisely so a crash mid-call leaves a
    recorded unknown rather than a silent gap. But nothing resolved that unknown
    — the row sat as "pending" forever, its dashboard tile read "in flight"
    indefinitely, and nothing distinguished a slow call from a lost one. The
    executor's own timeout bounds how long a live call can hold the state; past
    `attempt_stale_after_seconds` the honest resolution is failed-outcome-
    unknown, marked here.

    TWO resolutions, split by the phase marker the write-ahead writes:

    - phase=write_ahead: the Razorpay call was in flight or its outcome never
      landed. The link MIGHT exist. Fail-closed: the attempt keeps occupying
      its budget slot and is marked failed-outcome-unknown. A later capture
      still attributes through the idempotency-key breadcrumb.

    - claimed_by=scheduler (no phase marker yet): the fire sweep claimed the
      row but crashed before the write-ahead — Razorpay was NEVER called, so
      there is nothing to be unknown about. Re-park it as "scheduled" a minute
      out instead of burning the agent's most deliberate decision on a deploy
      that happened to overlap it. The re-park is itself claimed-atomic: the
      conditional UPDATE only lands while the row is still pending.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=get_settings().attempt_stale_after_seconds)

    stale = await session.execute(
        select(RetryAttempt)
        .where(
            RetryAttempt.result == "pending",
            RetryAttempt.created_at <= cutoff,
        )
        .order_by(RetryAttempt.created_at)
        .limit(get_settings().scheduler_batch_size)
    )

    resolved = 0
    reparked = 0
    for attempt in stale.scalars().all():
        details = attempt.result_details if isinstance(attempt.result_details, dict) else {}
        reached_write_ahead = details.get("phase") == "write_ahead"

        if not reached_write_ahead and details.get("claimed_by") == "scheduler":
            # Crash BEFORE the write-ahead: Razorpay was never called. Re-park.
            reparked_row = await session.execute(
                update(RetryAttempt)
                .where(RetryAttempt.id == attempt.id, RetryAttempt.result == "pending")
                .values(
                    result="scheduled",
                    scheduled_at=now + timedelta(minutes=1),
                    result_details={
                        "reparked_by": "scheduler",
                        "reason": (
                            "stale scheduler claim — crashed before the "
                            "write-ahead; Razorpay never called, retry re-parked"
                        ),
                    },
                )
            )
            if reparked_row.rowcount == 1:  # type: ignore[attr-defined]
                if attempt.recovery_case_id:
                    case = await session.get(RecoveryCase, attempt.recovery_case_id)
                    if case is not None:
                        log_event(
                            session,
                            case,
                            "deferred",
                            actor="scheduler",
                            attempt_id=str(attempt.id),
                            reason="stale scheduler claim re-parked (pre-write-ahead)",
                        )
                logger.info(
                    "Stale scheduler claim re-parked: key=%s (crashed pre-write-ahead)",
                    attempt.idempotency_key,
                )
                reparked += 1
            continue

        # Same portable claim as everywhere else in this module: whoever flips
        # the row out of "pending" owns it, so a sweep cannot race an execution
        # that resolved the row a moment ago.
        claimed = await session.execute(
            update(RetryAttempt)
            .where(RetryAttempt.id == attempt.id, RetryAttempt.result == "pending")
            .values(
                result="failed",
                executed_at=now,
                result_details={
                    "scheduler": (
                        "stale-pending: no outcome after "
                        f"{get_settings().attempt_stale_after_seconds}s — "
                        "marked failed, outcome unknown (fail-closed)"
                    )
                },
            )
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            continue

        if attempt.recovery_case_id:
            case = await session.get(RecoveryCase, attempt.recovery_case_id)
            if case is not None:
                log_event(
                    session,
                    case,
                    "reconciled",
                    actor="scheduler",
                    attempt_id=str(attempt.id),
                    idempotency_key=attempt.idempotency_key,
                    reason="stale pending — outcome unknown",
                )
        logger.warning(
            "Stale pending attempt resolved: key=%s age>%ss",
            attempt.idempotency_key,
            get_settings().attempt_stale_after_seconds,
        )
        resolved += 1
    if reparked:
        logger.info("Stale scheduler claims re-parked: %d", reparked)
    return resolved


async def cancel_links_for_closed_cases(
    session: AsyncSession,
    orchestrator: PaymentRecoveryOrchestrator,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> int:
    """
    Kill the Payment Links of finished cases, so they can never be paid late.

    Every retry mints a NEW link for the full amount while the old ones stay
    live on Razorpay's side. Nothing stopped a customer paying an old link
    after a newer one had already settled — a real double payment the case
    then credited twice. When a case reaches a terminal state, its links must
    die with it: this sweep finds attempts carrying a live external_ref whose
    case is terminal and cancels them through the Razorpay API.

    Cancelling an already-paid or already-cancelled link is refused by
    Razorpay with a 400 — which executor.cancel_payment_link treats as inert-
    success, so paid links resolve on the first pass and never loop. A link
    whose cancel call FAILS (network) stays unmarked and is retried next tick.
    """
    now = now or datetime.now(UTC)
    limit = limit or get_settings().scheduler_batch_size

    result = await session.execute(
        select(RetryAttempt)
        .join(RecoveryCase, RecoveryCase.id == RetryAttempt.recovery_case_id)
        .where(
            RetryAttempt.external_ref.is_not(None),
            RecoveryCase.state.in_(
                ["recovered", "exhausted", "abandoned", "expired", "opted_out"]
            ),
            RetryAttempt.result.notin_(["cancelled"]),
        )
        .order_by(RetryAttempt.created_at)
        .limit(limit)
    )

    cancelled = await _cancel_links(
        session, orchestrator, result.scalars().all(), now=now, why="closed case"
    )
    if cancelled:
        logger.info("Cancelled %d payment link(s) on closed cases", cancelled)
    return cancelled


async def _cancel_links(
    session: AsyncSession,
    orchestrator: PaymentRecoveryOrchestrator,
    attempts: Sequence[RetryAttempt],
    *,
    now: datetime,
    why: str,
) -> int:
    """Cancel each attempt's live link and stamp it, skipping ones already dead."""
    cancelled = 0
    for attempt in attempts:
        details = dict(attempt.result_details) if isinstance(attempt.result_details, dict) else {}
        if details.get("link_cancelled_at") is not None:
            continue
        link_id = attempt.external_ref
        if link_id is None:  # pragma: no cover — filtered by the callers' queries
            continue
        ok = await orchestrator._executor.cancel_payment_link(link_id)
        if not ok:
            # The call itself failed (network). Leave it unstamped; the next
            # tick retries. Stamping on failure would abandon a live link.
            continue
        details["link_cancelled_at"] = now.isoformat()
        details["link_cancelled_because"] = why
        attempt.result_details = details
        session.add(attempt)
        cancelled += 1

    if cancelled:
        await session.commit()
    return cancelled


async def cancel_superseded_links(
    session: AsyncSession,
    orchestrator: PaymentRecoveryOrchestrator,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> int:
    """
    Kill the older links of an OPEN case, leaving only the newest one payable.

    cancel_links_for_closed_cases only fires once a case reaches a terminal
    state, and executor.cancel_payment_link's own docstring already promised
    the other half — "when a case closes (OR AN ATTEMPT IS SUPERSEDED), the
    links it spawned must die with it" — which had no implementation and no
    caller. So while a case was open, every retry and every nudge minted a
    fresh link for the full amount and every earlier one stayed live on
    Razorpay's side.

    That is a real double charge, not a theoretical one: a customer with two
    of our SMS messages in their inbox has two payable links for the same
    money, and nothing downstream merges them. attribute_capture catches the
    second only AFTER it settles, as an `overpayment` event that needs a
    manual refund — the customer's money has already left.

    Only the newest link-bearing attempt survives, because that is the one the
    latest message points at and the one the recovery page reuses. Ties on
    created_at keep BOTH alive: cancelling the wrong one of a pair we cannot
    order is worse than briefly leaving two up, and the next tick re-reads.
    """
    now = now or datetime.now(UTC)
    limit = limit or get_settings().scheduler_batch_size

    # The newest link-bearing attempt per open case — the one to spare.
    newest = (
        select(
            RetryAttempt.recovery_case_id.label("case_id"),
            func.max(RetryAttempt.created_at).label("newest_at"),
        )
        .join(RecoveryCase, RecoveryCase.id == RetryAttempt.recovery_case_id)
        .where(
            RetryAttempt.external_ref.is_not(None),
            RecoveryCase.state == "open",
        )
        .group_by(RetryAttempt.recovery_case_id)
        .subquery()
    )

    result = await session.execute(
        select(RetryAttempt)
        .join(newest, newest.c.case_id == RetryAttempt.recovery_case_id)
        .where(
            RetryAttempt.external_ref.is_not(None),
            RetryAttempt.created_at < newest.c.newest_at,
            RetryAttempt.result.notin_(["cancelled"]),
        )
        .order_by(RetryAttempt.created_at)
        .limit(limit)
    )

    cancelled = await _cancel_links(
        session, orchestrator, result.scalars().all(), now=now, why="superseded"
    )
    if cancelled:
        logger.info(
            "Cancelled %d superseded payment link(s) on open cases", cancelled
        )
    return cancelled


async def chase_due_cases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    deadline: float | None = None,
) -> int:
    """
    Chase every chaser-driven case whose wait has elapsed. Returns how many
    cases were chased.

    This is the sweep the non-payment risk types live on: an abandoned cart,
    a halted subscription, an overdue invoice and a failed mandate debit have
    NO inbound webhook, so their cases carry a next_action_at and something
    has to go looking. Each due case runs through the full chase pipeline
    (orchestrator.chase_case): stopping rule → agent → guardrail → write-ahead
    execution → ladder advance. The pipeline itself decides whether the case
    is contacted, deferred or closed.

    All four types come back in ONE oldest-first query rather than one query
    per type: under a backlog the longest-waiting case is served first
    whatever its type, instead of the first type in the dict eating the whole
    tick while invoices starve.

    The time budget is the same property fire_due_retries already has, and for
    the same reason: each chase can spend an agent decision and a Razorpay
    call, so an unbounded sweep on a slow-Razorpay day runs the tick into the
    next one and the backlog compounds exactly when chasing matters most.
    `deadline` is the tick's shared monotonic deadline (tick() passes it, so
    the fire sweep's spend counts against the same budget); standalone callers
    get a fresh budget of one interval fraction.

    Concurrency is handled the same way as the webhook path, not with row
    locks: two workers chasing the same case build the same deterministic
    idempotency key, and the UNIQUE constraint on retry_attempts hands the
    attempt to exactly one of them. chase_case also refuses to act while an
    earlier attempt is still pending, so a slow Razorpay day cannot stack
    contacts on one case.

    payment_failure cases are excluded: that rail is webhook-driven — the
    event is its trigger — and sweeping it would run the pipeline a second
    time on every payment failure ever recorded.
    """
    now = now or datetime.now(UTC)
    orchestrator = get_orchestrator()

    if deadline is None:
        deadline = time.monotonic() + (
            get_settings().scheduler_interval_seconds * _FIRE_TIME_BUDGET_FRACTION
        )

    due = await due_cases(
        session, now=now,
        risk_types=tuple(RISK_POLICIES),
        # The old per-type loop admitted batch_size cases of EACH type; keep
        # the same total ceiling now that one query fetches them all. The time
        # budget, not this limit, is what bounds the tick's work.
        limit=get_settings().scheduler_batch_size * len(RISK_POLICIES),
    )

    chased = 0
    for idx, case in enumerate(due):
        if time.monotonic() > deadline:
            logger.info(
                "Chase sweep hit its time budget with %d case(s) still due — "
                "deferring them to the next tick",
                len(due) - idx,
            )
            break
        try:
            await orchestrator.chase_case(case, session, actor="chaser", now=now)
            chased += 1
        except Exception:
            # One broken case must not starve the rest of the batch. The
            # case keeps its next_action_at, so the next tick retries it —
            # the same re-arm philosophy the event reconcilers use.
            logger.exception("Chase failed for case %s — will retry", case.id)
            await session.rollback()
    return chased


async def remind_promises(
    session: AsyncSession, *, now: datetime | None = None, limit: int | None = None
) -> int:
    """
    Send the one pre-due reminder each pending promise is owed. Returns how
    many were handled (sent or skipped-with-reason — both stamp reminded_at).

    Memory decay is the top reason promises break, and the dunning research's
    answer is a friendly reminder ~48h before the date with the payment link
    repeated. The reminder is a REAL contact: orchestrator.send_promise_reminder
    runs it through the guardrail and spends an attempt slot, because a promise
    buys silence for the CHASE, not a free lane to remind from.

    `reminded_at` is stamped on EVERY terminal outcome — sent, guardrail-
    refused, no-policy — so no path re-fires next tick. The one case where
    the marker is deliberately NOT stamped: an exception, because the promise
    deserves its reminder when the blip clears.
    """
    now = now or datetime.now(UTC)
    orchestrator = get_orchestrator()
    batch = limit or get_settings().scheduler_batch_size

    due = await promises_due_for_reminder(session, now=now, limit=batch)
    if not due:
        return 0

    handled = 0
    for promise in due:
        case = await session.get(RecoveryCase, promise.recovery_case_id)
        if case is None:  # pragma: no cover — orphan promise
            promise.reminded_at = now
            continue
        try:
            outcome = await orchestrator.send_promise_reminder(
                case, promise, session, now=now
            )
            promise.reminded_at = now
            await session.commit()
            if outcome not in ("success", "already_sent"):
                log_event(
                    session,
                    case,
                    "promise_reminder_skipped",
                    promise_id=str(promise.id),
                    outcome=outcome,
                )
                await session.commit()
            handled += 1
        except Exception:
            # No reminded_at stamp: the reminder deserves another chance once
            # the blip clears. Rolled back so the promise's rows stay clean.
            logger.exception(
                "Promise reminder failed for %s — will retry next tick",
                promise.id,
            )
            await session.rollback()
    return handled


async def reconcile_risk_events(
    session: AsyncSession, *, now: datetime | None = None
) -> int:
    """
    Re-run merchant risk events whose background task never finished. Returns
    how many recovered.

    The risk router commits before returning 200 and processes in a background
    task — restart, crash or deploy in that window and the task is gone while
    the event sits stored with processed=False. The merchant will not
    re-deliver (we already said 200), so the database is the only retry
    mechanism there is. Same re-arm discipline as reconcile_events: a
    transient failure counts one processing_attempt and is re-armed until the
    shared cap; only a deterministically-broken payload rests.
    """
    now = now or datetime.now(UTC)
    return await _reconcile_sweep(
        session,
        model=RiskEvent,
        lookup_col=RiskEvent.event_id,
        stale_where=[],
        handle=process_risk_event,
        event_ref=lambda e: e.event_id,
        label="risk event",
        now=now,
    )


# ── AR account consolidation (B2B receivables) ─────────────────────────────


async def chase_due_accounts(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> int:
    """
    The B2B receivables sweep: one contact per buyer account per dunning rung.

    A buyer with four overdue invoices is one account with one AR balance, not
    four independent chases to the same desk. This sweep enforces that by
    SCHEDULING, not by sending:

      * it places the account on the ladder rung its oldest invoice's aging
        demands (stage_for_aging — the tone follows the most-injured invoice)
      * it picks ONE carrier case (the least-contacted joiner, so link
        minting and budget rotate across the account's invoices) and leaves
        it due — chase_due_cases, running right after this sweep in the same
        tick, chases it through the full existing pipeline: agent → guardrail
        → write-ahead attempt → Razorpay link + notify. That chase IS the
        rung's contact; the per-case pipeline stays the only path that spends
        budget or mints links, so every money-safety property holds unchanged
      * every OTHER joiner's next_action_at moves out to the next rung's gap,
        which is what stops the same person getting four separate messages
      * one ar_contact_log row records that this rung's single contact
        covered exactly these invoices — the fact retry_attempts cannot
        express, because it is per-case

    Idempotency is the log: a rung already fired for the account (latest
    log's stage matches what aging demands) means the only due work is the
    carrier's 72h re_chase floor expiring mid-rung, and the answer is to
        defer it to the next rung's gap — the ladder's 6-7 day rungs replace
    the flat 72h cadence for account-linked invoices.

    Honours the same bounds as everything else: the B2B contact window
    (Mon–Fri 09:30–18:30 IST — outside it, every due invoice case is deferred
    to the window's edge, spending nothing, the same defer-not-burn pattern
    the IST blackout uses), the per-case 4-contact budget, and open disputes
    (a disputed case is excluded from the statement entirely until a human
    resolves it). Stage 3 (urgent) also raises the human call task —
    merchant-side work that never touches the customer's contact budget.
    """
    # is_b2b_contact_time is imported at module level (see the import block at
    # the top): it is the one name here that is a wall-clock RULE rather than a
    # helper, and a function-local import gives tests no seam to hold it open
    # without rewiring next_b2b_window() for everyone else.
    from src import recovery_link
    from src.receivables.ladder import (
        next_b2b_window,
        next_stage_gap_hours,
        stage_for_aging,
    )
    from src.receivables.models import ArAccount, ArContactLog
    from src.receivables.statement import compose_stage_message

    now = now or datetime.now(UTC)

    # Every due open invoice case, account-linked or not. The window check
    # below needs the ungrouped set; the account loop re-uses the rows.
    due_invoices = (
        (
            await session.execute(
                select(RecoveryCase)
                .where(
                    RecoveryCase.risk_type == "invoice_overdue",
                    RecoveryCase.state == "open",
                    RecoveryCase.next_action_at.isnot(None),
                    RecoveryCase.next_action_at <= now,
                )
                .order_by(RecoveryCase.due_at)
                .limit(limit * 5)
            )
        )
        .scalars()
        .all()
    )

    # The B2B window is a timing rule, not a case verdict — and an invoice
    # is a B2B contact whether or not the merchant supplied an account_ref.
    # Defer to the window's edge rather than letting the per-case sweep burn
    # a budget slot on a Sunday morning.
    if not is_b2b_contact_time(now):
        edge = next_b2b_window(now)
        moved = 0
        for case in due_invoices:
            case.next_action_at = edge
            moved += 1
        if moved:
            await session.commit()
            logger.info(
                "AR consolidation: %d invoice case(s) deferred to the B2B "
                "window (%s)",
                moved, edge.isoformat(),
            )
        return 0

    # Only account-linked cases consolidate; the rest keep the per-case
    # chase exactly as before this layer existed.
    by_account: dict[uuid.UUID, list[RecoveryCase]] = {}
    for case in due_invoices:
        if case.account_id is not None:
            by_account.setdefault(case.account_id, []).append(case)
    if not by_account:
        return 0

    consolidated = 0
    for account_id, cases in list(by_account.items())[:limit]:
        account = await session.get(ArAccount, account_id)
        if account is None:  # pragma: no cover — backfill guarantees presence
            continue

        # Exclude disputed cases: the freeze is total until a human answers.
        undisputed: list[RecoveryCase] = []
        for case in cases:
            dispute = await session.scalar(
                select(CaseDispute).where(
                    CaseDispute.case_id == case.id,
                    CaseDispute.status == "open",
                )
            )
            if dispute is None:
                undisputed.append(case)
        # Cases with budget left may join the statement; a spent case is
        # chase_due_cases' to close as exhausted, not this sweep's.
        joiners: list[RecoveryCase] = [
            c for c in undisputed
            if c.attempts_used < c.max_attempts and c.due_at is not None
        ]
        if not joiners:
            continue

        # Coerce every joiner's due_at to aware UTC once, up front — DB
        # dialects disagree on awareness (Postgres returns aware, SQLite
        # naive) and every rung decision below reads these.
        aware_due: dict[uuid.UUID, datetime] = {}
        for c in joiners:
            due = c.due_at
            assert due is not None  # narrowed by the joiners filter
            aware_due[c.id] = due if due.tzinfo else due.replace(tzinfo=UTC)

        # The aging clock places the account on its rung: the OLDEST joiner's
        # days-past-due decides the tone, because a statement must match the
        # most-injured invoice, not the average one.
        oldest_due = min(aware_due.values())
        stage = stage_for_aging(max(0, (now - oldest_due).days))
        if stage is None:  # pragma: no cover — stage_for_aging is total ≥ 0
            continue

        # Idempotency: this rung already fired for the account. The only due
        # work is a mid-rung backoff expiry — defer it to the next rung's
        # gap and contact nobody. The comparison is exact (not >=) on
        # purpose: if the oldest invoice was paid and the account's aging
        # SOFTENED, the remaining balance deserves a statement at the tone
        # its current aging demands, not silence until aging re-passes the
        # old rung.
        latest_log = await session.scalar(
            select(ArContactLog)
            .where(ArContactLog.account_id == account_id)
            .order_by(ArContactLog.created_at.desc())
            .limit(1)
        )
        if latest_log is not None and latest_log.stage_level == stage.level:
            gap = timedelta(hours=next_stage_gap_hours(stage.level))
            for case in joiners:
                case.next_action_at = now + gap
            await session.commit()
            continue

        # Compose the rung's statement — the record of what this single
        # contact said and which invoices it covered.
        statement_cases = [
            {
                "subject_ref": c.subject_ref,
                "due_at": aware_due[c.id],
                "amount_at_risk": c.amount_at_risk,
                "amount_recovered": c.amount_recovered,
                "pay_url": None,  # per-invoice links stay on the case pipeline
            }
            for c in joiners
        ]
        message = compose_stage_message(
            statement_cases,
            tone=stage.tone,
            merchant_name=get_settings().merchant_name or "the merchant",
            # The account statement page: every open invoice on this
            # account in one place, each row deep-linking to its own
            # recovery page. This message consolidates several invoices, so
            # a single-invoice link was always the wrong destination for it.
            # Still degrades to a plain reminder when link minting is
            # unconfigured (no secret, no public base URL) — the compose
            # layer already handles an empty link.
            statement_link=recovery_link.url_for_account(account_id) or "",
            now=now,
        )
        session.add(
            ArContactLog(
                account_id=account_id,
                stage_level=stage.level,
                case_refs=[
                    {"ref": c.subject_ref, "case_id": str(c.id)} for c in joiners
                ],
                channels=list(stage.channels),
                sms_copy=message["sms"],
                email_subject=message["subject"],
                planned_for=now,
                # NULL on purpose: this layer schedules, it does not send.
                # The delivery record is the carrier's retry_attempts row in
                # this same tick; a reconcile pass can stamp sent_at later.
                sent_at=None,
            )
        )

        # The carrier: least-contacted joiner (then oldest) — budget and
        # link minting rotate across the account's invoices over the ladder,
        # so a multi-invoice buyer's every invoice eventually gets its link.
        carrier = min(
            joiners,
            key=lambda c: (c.attempts_used, aware_due[c.id]),
        )
        gap = timedelta(hours=next_stage_gap_hours(stage.level))
        for case in joiners:
            if case is not carrier:
                case.next_action_at = now + gap
        # The carrier stays due: chase_due_cases runs after this sweep in
        # the same tick and delivers the rung's contact through the full
        # per-case pipeline.

        # Stage 3 (urgent) raises the human call task — merchant-side work,
        # never budget spend.
        if "call_task" in stage.channels:
            from src.receivables.tasks import raise_call_task

            await raise_call_task(
                session,
                account_id=account_id,
                account_ref=account.account_ref,
                detail={
                    "stage": stage.tone,
                    "total_outstanding": message["statement"]["total_outstanding"],
                    "oldest_days": message["statement"]["oldest_days"],
                    "case_refs": [c.subject_ref for c in joiners],
                },
            )

        consolidated += 1
        logger.info(
            "AR rung fired: account=%s stage=%d (%s) carrier=%s covering=%d "
            "case(s) — carrier stays due for the per-case pipeline",
            account.account_ref, stage.level, stage.tone,
            carrier.subject_ref, len(joiners),
        )
        await session.commit()

    return consolidated


async def report_due_cases(session: AsyncSession, *, now: datetime | None = None) -> int:
    """
    Surface payment-failure cases whose wait has elapsed. Returns how many.

    The chaser-driven risk types are handled by chase_due_cases above; what
    this sweep still reports is the payment rail — webhook-driven cases whose
    next_action_at (set by the escalation backoff) has passed. Those wait for
    their next webhook rather than for a sweep, and this count keeps that
    waiting VISIBLE in the heartbeat instead of silently accumulating.
    """
    # Filter in SQL, not Python: a mixed-type fetch with a LIMIT could fill
    # the whole window with chaser cases (their sweep yielded on the time
    # budget, leaving them due) and push every payment-failure row out of
    # sight — the heartbeat would then under-report exactly the backlog it
    # exists to surface.
    due = await due_cases(
        session, now=now, risk_type="payment_failure",
        limit=get_settings().scheduler_batch_size,
    )
    if due:
        logger.info(
            "Payment-failure cases waiting on their next webhook: %d",
            len(due),
        )
    return len(due)


async def _stamp_heartbeat(session: AsyncSession, counts: dict[str, int], now: datetime) -> None:
    """
    Dead-man's-switch: stamp the single heartbeat row on every tick.

    A tick that logs and swallows an exception is indistinguishable from a
    scheduler that died days ago — unless something OUTSIDE the loop remembers
    when it last ran. The Operations view reads this row; a last_tick_at older
    than a couple of intervals means nothing is firing deferred retries.

    The first tick after boot can race a second worker (WEB_CONCURRENCY > 1):
    both see "no row", both insert id=1, and the loser's IntegrityError would
    otherwise fail the WHOLE tick's commit — discarding the work all five
    sweeps just did. So the insert runs inside a SAVEPOINT: the loser's
    rollback undoes only its own insert, and it re-reads and stamps the
    winner's row instead. The rest of the tick's work is untouched.
    """
    heartbeat = await session.get(SchedulerHeartbeat, 1)
    if heartbeat is None:
        nested = await session.begin_nested()
        try:
            session.add(SchedulerHeartbeat(id=1, last_tick_at=now, last_tick_counts=counts))
            await nested.commit()
        except IntegrityError:
            await nested.rollback()
            heartbeat = await session.get(SchedulerHeartbeat, 1)
            if heartbeat is None:  # pragma: no cover — only if the table is gone
                return
        else:
            return
    heartbeat.last_tick_at = now
    heartbeat.last_tick_counts = counts
    await session.flush()


async def _reconcile_plans(session: AsyncSession) -> int:
    """Plan verdicts, wrapped so a bad pass cannot kill the tick."""
    try:
        from src.receivables.plans import reconcile_plans

        return await reconcile_plans(session)
    except Exception:
        logger.exception("Plan reconcile failed — continuing")
        await session.rollback()
        return 0


async def _deliver_alerts(session: AsyncSession) -> int:
    """Merchant-alert writeback, wrapped so a bad endpoint cannot kill the tick."""
    try:
        from src.receivables.alerts import deliver_pending_alerts

        return await deliver_pending_alerts(session)
    except Exception:
        logger.exception("Alert delivery failed — continuing")
        await session.rollback()
        return 0


async def tick(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """One full pass. Returns what each sweep did, for logs and for tests."""
    now = now or datetime.now(UTC)
    orchestrator = get_orchestrator()
    # One shared deadline for the tick's expensive sweeps: the fire sweep
    # budgets itself from its own start, and the chase sweep spends whatever
    # is left of the same fraction. Without the shared view, back-to-back
    # sweeps could each take a full budget and run the tick long past the
    # interval — heartbeats go stale and deferred work fires late.
    tick_deadline = time.monotonic() + (
        get_settings().scheduler_interval_seconds * _FIRE_TIME_BUDGET_FRACTION
    )
    counts = {
        "retries_fired": await fire_due_retries(session, now=now),
        "events_reconciled": await reconcile_events(session, now=now),
        "risk_events_reconciled": await reconcile_risk_events(session, now=now),
        "attempts_reconciled": await reconcile_stale_attempts(session, now=now),
        "links_cancelled": await cancel_links_for_closed_cases(
            session, orchestrator, now=now
        ),
        # The other half: an OPEN case's older links, superseded by the newest
        # one. Without this every retry left a live, fully payable link behind
        # and a customer with two of our messages could pay both.
        "superseded_links_cancelled": await cancel_superseded_links(
            session, orchestrator, now=now
        ),
        "promises_expired": await expire_promises(session, now=now),
        # The pre-due half of the promise lifecycle: the reminder fires
        # BEFORE expire_promises could ever see the promise break.
        "promises_reminded": await remind_promises(session, now=now),
        # The receivables plan verdicts: defaulted/completed stamps and their
        # merchant alerts. Runs after expire_promises so a missed instalment's
        # promise is already broken — one clock, one authority.
        "plans_reconciled": await _reconcile_plans(session),
        # The B2B layer runs BEFORE the per-case sweep, and the order is a
        # correctness property, not a preference. chase_due_accounts picks ONE
        # carrier case per buyer account, defers every other joiner, and leaves
        # the carrier due so the per-case sweep below delivers the rung's single
        # contact. Run the other way round (as this dict once did) and all three
        # of its guarantees break at once: every joiner is contacted separately
        # (the buyer gets four messages, which is the exact harm this layer
        # exists to prevent), the Mon–Fri B2B window is skipped because only
        # this sweep enforces it, and the consolidation then finds nothing due —
        # the per-case sweep has already pushed every next_action_at forward —
        # so no ar_contact_log row is ever written and the ladder never fires.
        "accounts_consolidated": await chase_due_accounts(session, now=now),
        # Chase BEFORE report: the report only counts what the chase left
        # behind (payment-failure cases waiting on their next webhook).
        "cases_chased": await chase_due_cases(session, now=now, deadline=tick_deadline),
        "due_cases_reported": await report_due_cases(session, now=now),
        # The writeback drain: queued merchant alerts → HMAC-signed POSTs.
        "alerts_delivered": await _deliver_alerts(session),
    }
    await _stamp_heartbeat(session, counts, now)
    await session.commit()
    return counts


async def run_forever() -> None:
    """Poll loop. Cancelled by the app's lifespan on shutdown."""
    interval = get_settings().scheduler_interval_seconds
    logger.info("Scheduler started (every %ds)", interval)
    while True:
        try:
            async with async_session_factory() as session:
                counts = await tick(session)
            if any(counts.values()):
                logger.info("Scheduler tick: %s", counts)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad tick must not end the loop. A scheduler that dies silently
            # on a transient database blip looks identical to one that is
            # working, right up until someone notices no retry has fired in days.
            logger.exception("Scheduler tick failed — continuing")
        await asyncio.sleep(interval)


def start(loop_task: set[asyncio.Task[None]]) -> asyncio.Task[None] | None:
    """Launch the loop unless disabled. The caller keeps a reference to it."""
    if not get_settings().scheduler_enabled:
        logger.info("Scheduler disabled (SCHEDULER_ENABLED=false)")
        return None
    task = asyncio.create_task(run_forever())
    # Held in a set the caller owns: asyncio keeps only a weak reference to a
    # running task, so a local variable going out of scope can have it garbage
    # collected mid-await.
    loop_task.add(task)
    task.add_done_callback(loop_task.discard)
    return task


async def stop(task: asyncio.Task[None] | None) -> None:
    """Cancel the loop and wait for it to unwind."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    logger.info("Scheduler stopped")
