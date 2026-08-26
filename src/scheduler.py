"""
Background worker — the thing that makes deferred decisions actually happen.

Five sweeps, one tick:

    fire_due_retries()            an agent said "retry in 4 hours"; four hours have passed
    reconcile_events()            a webhook was stored but its background task never ran
    reconcile_stale_attempts()    a write-ahead attempt was committed but its outcome never landed
    cancel_links_for_closed_cases()  links of finished cases die with them
    expire_promises()             a promise-to-pay came due with no money (src/cases.py)

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
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.actions import RetryAction
from src.cases import expire_promises, log_event
from src.config import get_settings
from src.database import async_session_factory
from src.models import (
    PaymentFailure,
    RecoveryCase,
    RetryAttempt,
    SchedulerHeartbeat,
    WebhookEvent,
)
from src.orchestrator import (
    PaymentRecoveryOrchestrator,
    get_orchestrator,
    process_payment_failure,
)

logger = logging.getLogger(__name__)

# How many times reconcile_events will re-arm an event that keeps raising
# before it gives up and leaves the error recorded. A transient database blip
# must not permanently skip a real payment failure; a payload that raises on
# every tick must also not eat the whole sweep batch forever. Three is the
# compromise: enough to survive a bad minute, few enough to starve nothing.
_EVENT_RECONCILE_MAX_ATTEMPTS = 3

# Fraction of one tick interval fire_due_retries may spend before yielding
# back to the loop. Each fire can hold a Razorpay call for up to the executor
# timeout, so an unbounded sweep of 50 due rows could run ~8 minutes against a
# 60s interval — exactly when Razorpay is slow, i.e. when retries matter most.
# The budget caps the overrun; whatever does not fit waits for the next tick
# (the rows stay "scheduled" and the indexed query re-finds them).
_FIRE_TIME_BUDGET_FRACTION = 0.8


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
        # Nothing but the payment rail schedules retries today. A non-payment
        # case reaching here means a future risk type started using retry_at
        # without a fire path — refuse rather than guess.
        await _mark(session, attempt, "skipped", "no payment_failure to retry")
        return False

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
        # Fires as a plain retry: the wait it asked for is over, and re-sending
        # retry_at would park the row right back where it came from.
        action="switch_rail" if attempt.target_rail else "retry_now",
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


async def _mark(
    session: AsyncSession, attempt: RetryAttempt, result: str, reason: str
) -> None:
    attempt.result = result
    attempt.executed_at = datetime.now(UTC)
    attempt.result_details = {"scheduler": reason}
    session.add(attempt)


async def reconcile_events(session: AsyncSession, *, now: datetime | None = None) -> int:
    """
    Re-run webhook events whose background task never finished. Returns how many.

    FastAPI's BackgroundTasks run in the request's process after the response is
    sent. Restart, crash, or deploy in that window and the task is simply gone —
    the event is durably stored (the router commits before returning 200, on
    purpose) and `processed` stays False forever. Razorpay will not re-send it:
    we already said 200. Without this sweep every such payment silently gets no
    recovery attempt at all, and nothing in the system reports a gap.

    The age threshold is what keeps this from racing the in-flight task that is
    still legitimately running.

    Transient failures no longer consume the event. The old sweep claimed the
    row (processed=True) and gave up on the first exception — a database blip
    permanently skipped a real payment failure. Now a failing event counts one
    processing_attempt and is RE-ARMED (processed=False) until it has failed
    _EVENT_RECONCILE_MAX_ATTEMPTS times; only then does it rest with the error
    recorded. A deterministically-broken payload still stops eating the batch.
    """
    now = now or datetime.now(UTC)
    settings = get_settings()
    cutoff = now - timedelta(seconds=settings.event_reconcile_after_seconds)

    stale = await session.execute(
        select(WebhookEvent)
        .where(
            WebhookEvent.processed.is_(False),
            WebhookEvent.event_type == "payment.failed",
            WebhookEvent.received_at <= cutoff,
        )
        .order_by(WebhookEvent.received_at)
        .limit(settings.scheduler_batch_size)
    )

    recovered = 0
    for event in stale.scalars().all():
        # Claim it the same way fire_due_retries claims an attempt. With
        # WEB_CONCURRENCY > 1 every uvicorn worker runs its own scheduler, so
        # two of them can select the same stale event in the same second.
        # Duplicate processing is not a double charge — the idempotency key is
        # UNIQUE and the orchestrator now exits cleanly when it loses that race
        # — but it is wasted work and two contradictory log lines about the
        # same payment. Whoever flips processed owns the event.
        claimed = await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event.id, WebhookEvent.processed.is_(False))
            .values(processed=True)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            logger.debug("Event %s claimed by another worker", event.razorpay_event_id)
            continue
        await session.commit()

        try:
            await process_payment_failure(event, session)
            await session.commit()
            recovered += 1
            logger.info("Reconciled dropped event: %s", event.razorpay_event_id)
        except Exception:
            logger.exception("Reconcile failed for %s", event.razorpay_event_id)
            await session.rollback()
            attempts = (event.processing_attempts or 0) + 1
            if attempts < _EVENT_RECONCILE_MAX_ATTEMPTS:
                # Re-arm: a transient failure gets another try on a later tick.
                await session.execute(
                    update(WebhookEvent)
                    .where(WebhookEvent.id == event.id)
                    .values(
                        processed=False,
                        processing_attempts=attempts,
                        processing_error=f"Reconcile attempt {attempts} failed; re-armed",
                    )
                )
                logger.warning(
                    "Reconcile failed for %s (attempt %d/%d) — re-armed for retry",
                    event.razorpay_event_id,
                    attempts,
                    _EVENT_RECONCILE_MAX_ATTEMPTS,
                )
            else:
                # Give up with the error recorded — visible in the Operations
                # view instead of looping forever.
                await session.execute(
                    update(WebhookEvent)
                    .where(WebhookEvent.id == event.id)
                    .values(
                        processed=True,
                        processing_attempts=attempts,
                        processing_error="Reconciliation failed after retry cap",
                    )
                )
                logger.error(
                    "Reconcile permanently failed for %s after %d attempts",
                    event.razorpay_event_id,
                    attempts,
                )
            await session.commit()
    return recovered


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
            RecoveryCase.state.in_(["recovered", "exhausted", "abandoned", "opted_out"]),
            RetryAttempt.result.notin_(["cancelled"]),
        )
        .order_by(RetryAttempt.created_at)
        .limit(limit)
    )

    cancelled = 0
    for attempt in result.scalars().all():
        details = dict(attempt.result_details) if isinstance(attempt.result_details, dict) else {}
        if details.get("link_cancelled_at") is not None:
            continue
        link_id = attempt.external_ref
        if link_id is None:  # pragma: no cover — filtered by the query above
            continue
        ok = await orchestrator._executor.cancel_payment_link(link_id)
        if not ok:
            continue
        details["link_cancelled_at"] = now.isoformat()
        attempt.result_details = details
        session.add(attempt)
        cancelled += 1

    if cancelled:
        await session.commit()
        logger.info("Cancelled %d payment link(s) on closed cases", cancelled)
    return cancelled


async def _stamp_heartbeat(session: AsyncSession, counts: dict[str, int], now: datetime) -> None:
    """
    Dead-man's-switch: stamp the single heartbeat row on every tick.

    A tick that logs and swallows an exception is indistinguishable from a
    scheduler that died days ago — unless something OUTSIDE the loop remembers
    when it last ran. The Operations view reads this row; a last_tick_at older
    than a couple of intervals means nothing is firing deferred retries.
    """
    heartbeat = await session.get(SchedulerHeartbeat, 1)
    if heartbeat is None:
        heartbeat = SchedulerHeartbeat(id=1, last_tick_at=now, last_tick_counts=counts)
        session.add(heartbeat)
    else:
        heartbeat.last_tick_at = now
        heartbeat.last_tick_counts = counts
    await session.flush()


async def tick(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """One full pass. Returns what each sweep did, for logs and for tests."""
    now = now or datetime.now(UTC)
    orchestrator = get_orchestrator()
    counts = {
        "retries_fired": await fire_due_retries(session, now=now),
        "events_reconciled": await reconcile_events(session, now=now),
        "attempts_reconciled": await reconcile_stale_attempts(session, now=now),
        "links_cancelled": await cancel_links_for_closed_cases(
            session, orchestrator, now=now
        ),
        "promises_expired": await expire_promises(session, now=now),
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
