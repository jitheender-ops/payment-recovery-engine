"""
Background worker — the thing that makes deferred decisions actually happen.

Eight sweeps, one tick:

    fire_due_retries()            an agent said "retry in 4 hours"; four hours have passed
    reconcile_events()            a webhook was stored but its background task never ran
    reconcile_risk_events()       a merchant risk event was stored but its task never ran
    reconcile_stale_attempts()    a write-ahead attempt was committed but its outcome never landed
    cancel_links_for_closed_cases()  links of finished cases die with them
    cancel_superseded_links()        an open case keeps only its newest link
    expire_promises()             a promise-to-pay came due with no money (src/cases.py)
    chase_due_cases()             a chaser-driven case whose wait elapsed (cart, subscription,
                                  invoice, mandate — the risk types with no inbound webhook)
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
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.actions import RetryAction
from src.cases import due_cases, expire_promises, log_event
from src.chasers.policy import RISK_POLICIES
from src.config import get_settings
from src.database import async_session_factory
from src.ingestion.router import EVENT_RECONCILE_MAX_ATTEMPTS, attribute_captured_payload
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
        action="switch_rail" if attempt.target_rail else "retry_now",
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

    # The ladder's next rung: same floor as chase_case so the due-case sweep
    # does not re-chase immediately, and a spent budget closes the case.
    if case.state == "open":
        if case.attempts_used >= case.max_attempts:
            from src.cases import close_case

            close_case(
                case, "exhausted",
                f"attempt budget spent ({case.attempts_used}/{case.max_attempts})",
            )
            log_event(
                session, case, "closed", actor="scheduler",
                state="exhausted", reason=case.close_reason,
            )
        else:
            next_at = case.next_action_at
            if next_at is None or _aware(next_at) <= now:
                case.next_action_at = now + timedelta(hours=policy.re_chase_hours)

    await session.commit()
    logger.info(
        "Scheduled case retry fired: case=%s key=%s result=%s",
        case.id, attempt.idempotency_key, attempt.result,
    )
    return True


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

    The age threshold is what keeps this from racing the in-flight task that is
    still legitimately running.

    Transient failures no longer consume the event. The old sweep claimed the
    row (processed=True) and gave up on the first exception — a database blip
    permanently skipped a real payment failure. Now a failing event counts one
    processing_attempt and is RE-ARMED (processed=False) until it has failed
    EVENT_RECONCILE_MAX_ATTEMPTS times; only then does it rest with the error
    recorded. A deterministically-broken payload still stops eating the batch.
    """
    now = now or datetime.now(UTC)
    settings = get_settings()
    cutoff = now - timedelta(seconds=settings.event_reconcile_after_seconds)

    stale = await session.execute(
        select(WebhookEvent)
        .where(
            WebhookEvent.processed.is_(False),
            WebhookEvent.event_type.in_(["payment.failed", "payment.captured"]),
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
            if event.event_type == "payment.failed":
                await process_payment_failure(event, session)
            else:
                await attribute_captured_payload(session, event.payload)
            await session.commit()
            recovered += 1
            logger.info("Reconciled dropped event: %s", event.razorpay_event_id)
        except Exception:
            logger.exception("Reconcile failed for %s", event.razorpay_event_id)
            await session.rollback()
            attempts = (event.processing_attempts or 0) + 1
            if attempts < EVENT_RECONCILE_MAX_ATTEMPTS:
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
                    EVENT_RECONCILE_MAX_ATTEMPTS,
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
    settings = get_settings()
    cutoff = now - timedelta(seconds=settings.event_reconcile_after_seconds)

    stale = await session.execute(
        select(RiskEvent)
        .where(
            RiskEvent.processed.is_(False),
            RiskEvent.received_at <= cutoff,
        )
        .order_by(RiskEvent.received_at)
        .limit(settings.scheduler_batch_size)
    )

    recovered = 0
    for event in stale.scalars().all():
        # Claim it: whoever flips processed owns the event. With more than one
        # worker, two schedulers can select the same stale event in the same
        # second; the idempotency key makes duplicate processing safe, but it
        # is still wasted work.
        claimed = await session.execute(
            update(RiskEvent)
            .where(RiskEvent.id == event.id, RiskEvent.processed.is_(False))
            .values(processed=True)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            continue
        await session.commit()

        try:
            await process_risk_event(event, session)
            await session.commit()
            recovered += 1
            logger.info("Reconciled dropped risk event: %s", event.event_id)
        except Exception:
            logger.exception("Reconcile failed for risk event %s", event.event_id)
            await session.rollback()
            attempts = (event.processing_attempts or 0) + 1
            if attempts < EVENT_RECONCILE_MAX_ATTEMPTS:
                await session.execute(
                    update(RiskEvent)
                    .where(RiskEvent.id == event.id)
                    .values(
                        processed=False,
                        processing_attempts=attempts,
                        processing_error=f"Reconcile attempt {attempts} failed; re-armed",
                    )
                )
                logger.warning(
                    "Reconcile failed for risk event %s (attempt %d/%d) — re-armed",
                    event.event_id, attempts, EVENT_RECONCILE_MAX_ATTEMPTS,
                )
            else:
                await session.execute(
                    update(RiskEvent)
                    .where(RiskEvent.id == event.id)
                    .values(
                        processed=True,
                        processing_attempts=attempts,
                        processing_error="Reconciliation failed after retry cap",
                    )
                )
                logger.error(
                    "Reconcile permanently failed for risk event %s after %d attempts",
                    event.event_id, attempts,
                )
            await session.commit()
    return recovered


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
        # Chase BEFORE report: the report only counts what the chase left
        # behind (payment-failure cases waiting on their next webhook).
        "cases_chased": await chase_due_cases(session, now=now, deadline=tick_deadline),
        "due_cases_reported": await report_due_cases(session, now=now),
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
