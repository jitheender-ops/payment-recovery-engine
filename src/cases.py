"""
Recovery case lifecycle — open, bound, attribute, close.

A case is one unit of revenue at risk, whatever leaked it. This module owns the
three questions the case table exists to answer:

    may we act again?    -> stop_reason()
    did the money land?  -> attribute_capture()
    how much did we get? -> batch_summary()
    what did we do, and why? -> log_event() / case_events

A promise to pay is the fourth: it is the only customer response that should
make the workflow go *quieter* rather than louder, so it lives here next to the
stopping rules rather than in the messaging layer that collected it.

Attribution is the load-bearing part. Recovery goes out as a Razorpay Payment
Link, so the customer paying it produces a payment id we have never seen. Before
this module, `payment.captured` tried to resolve it with
`RetryAttempt.payment_id == <captured id>` — a comparison between the new id and
the original failed one, which can essentially never match. Pending attempts
were never resolved and not one rupee was attributable to the engine.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.models import (
    CaseEvent,
    PaymentFailure,
    PromiseToPay,
    RecoveryCase,
    RetryAttempt,
    RetryLedger,
)

logger = logging.getLogger(__name__)

# How the money went missing. `subject_ref` is the id in that source's own
# namespace — a payment id, a cart id, a subscription id, an invoice id, a
# mandate token.
RiskType = Literal[
    "payment_failure",
    "checkout_abandonment",
    "subscription_failure",
    "invoice_overdue",
    "mandate_failure",
]

# Terminal unless "open". Every non-open state is a stopping rule that fired, and
# the reason it fired is in `close_reason`.
CaseState = Literal[
    "open",       # still chasing
    "recovered",  # money collected, in full
    "exhausted",  # attempt budget spent
    "abandoned",  # agent/guardrail said stop (hard decline, ceiling, blocklist)
    "expired",    # too old to be worth chasing
    "opted_out",  # customer withdrew consent — compliance stop, never reopens
]

ConsentStatus = Literal["granted", "opted_out"]

# How the customer was reached. Per-channel because contact-frequency limits are
# per-channel: two WhatsApp messages and a call is three contacts, not one.
Channel = Literal["whatsapp", "sms", "email", "voice", "payment_link"]

# A promise is "broken" only once its date has passed with no money — not the
# moment we doubt it. expire_promises() is what makes that transition, on the
# clock, so nothing else has to guess.
PromiseStatus = Literal["pending", "kept", "broken", "cancelled"]

# How a promise ends, and the audit event each ending writes. Typed as a mapping
# rather than built with an f-string so "pending" — which is a state, not a
# resolution — cannot be passed to resolve_promises() at all.
PromiseResolution = Literal["kept", "broken", "cancelled"]
_PROMISE_EVENT: dict[PromiseResolution, CaseEventType] = {
    "kept": "promise_kept",
    "broken": "promise_broken",
    "cancelled": "promise_cancelled",
}

# The audit vocabulary. Closed set so the trail stays queryable: a free-text
# event type turns "how often do we escalate" into a grep.
CaseEventType = Literal[
    "opened",
    "contacted",
    "escalated",
    "promise_made",
    "promise_kept",
    "promise_broken",
    "promise_cancelled",
    "attributed",
    "closed",
    "opted_out",
    "deferred",
    "stopped",
    "reconciled",
]

_TERMINAL: frozenset[str] = frozenset(
    {"recovered", "exhausted", "abandoned", "expired", "opted_out"}
)


async def open_case(
    session: AsyncSession,
    *,
    risk_type: RiskType,
    subject_ref: str,
    amount_at_risk: int,
    customer_id: str | None = None,
    batch_id: str | None = None,
    currency: str = "INR",
    max_attempts: int | None = None,
    due_at: datetime | None = None,
    next_action_at: datetime | None = None,
) -> RecoveryCase:
    """
    Get or create the case for this subject. Idempotent.

    Re-ingesting the same failure must land on the existing case: a second case
    for one payment would double both the attempt budget and the recovered
    amount, which is the one thing the headline number cannot survive.
    """
    existing = await find_case(session, risk_type=risk_type, subject_ref=subject_ref)
    if existing is not None:
        return existing

    case = RecoveryCase(
        risk_type=risk_type,
        subject_ref=subject_ref,
        amount_at_risk=amount_at_risk,
        currency=currency,
        customer_id=customer_id,
        batch_id=batch_id,
        due_at=due_at,
        # Webhook-driven risk types leave this NULL: a payment.failed event is
        # itself the trigger, so nothing needs to go looking for the case. The
        # ones with no inbound event — an overdue invoice, a cold cart — must
        # set it, or due_cases() will never surface them and the case sits open
        # forever without anyone chasing it.
        next_action_at=next_action_at,
        max_attempts=max_attempts or get_settings().max_retries_per_payment,
        state="open",
    )
    session.add(case)
    try:
        await session.flush()
    except IntegrityError:
        # Lost the race on uq_recovery_case_subject. The winner's row is the
        # case; ours never existed. Rolling back is safe here because callers
        # open the case before writing anything else about it.
        await session.rollback()
        won = await find_case(session, risk_type=risk_type, subject_ref=subject_ref)
        if won is None:  # pragma: no cover — only if the constraint is missing
            raise
        logger.info("Case race lost, reusing existing: %s/%s", risk_type, subject_ref)
        return won
    log_event(
        session,
        case,
        "opened",
        risk_type=risk_type,
        subject_ref=subject_ref,
        amount_at_risk=amount_at_risk,
        max_attempts=case.max_attempts,
    )
    return case


async def find_case(
    session: AsyncSession, *, risk_type: str, subject_ref: str
) -> RecoveryCase | None:
    """Look up a case by its natural key."""
    result = await session.execute(
        select(RecoveryCase).where(
            RecoveryCase.risk_type == risk_type,
            RecoveryCase.subject_ref == subject_ref,
        )
    )
    return result.scalar_one_or_none()


def stop_reason(case: RecoveryCase, ledger: RetryLedger | None = None) -> str | None:
    """
    Why we must not act on this case again, or None if we may.

    Bounded workflow in one place. Reads `attempts_used` off the case rather than
    counting retry_attempts rows: a budget you recompute from a mutable table is
    a budget that can quietly start again, and a case closed early by opt-out or
    expiry has a row count that no longer means "attempts allowed".
    """
    if case.state in _TERMINAL:
        return f"case already {case.state}: {case.close_reason or 'no reason recorded'}"
    if case.attempts_used >= case.max_attempts:
        return f"attempt budget spent ({case.attempts_used}/{case.max_attempts})"
    if ledger is not None and ledger.consent_status == "opted_out":
        return "customer opted out of contact"
    # Weakest reason, so it is checked last: this one expires on its own, and
    # reporting it ahead of a spent budget would describe a dead case as merely
    # waiting. Set by the escalation backoff and by a pending promise to pay.
    if case.next_action_at is not None and datetime.now(UTC) < case.next_action_at:
        return f"next action not due until {case.next_action_at.isoformat()}"
    return None


def attach_attempt(
    case: RecoveryCase,
    attempt: RetryAttempt,
    *,
    external_ref: str | None = None,
    channel: str | None = None,
    language: str | None = None,
) -> None:
    """
    Bind an attempt to its case and spend one unit of the budget.

    In-memory only — the caller owns the commit, because the orchestrator writes
    the attempt ahead of executing it and that ordering is a money-safety
    property, not a detail this function may reorder.
    """
    attempt.recovery_case_id = case.id
    if external_ref is not None:
        attempt.external_ref = external_ref
    if channel is not None:
        attempt.channel = channel
    if language is not None:
        attempt.language = language
    case.attempts_used += 1
    # Escalation counts customer-facing contact, not API retries. A silent
    # rail switch does not escalate anything from the customer's side.
    if attempt.action_type == "nudge_customer":
        case.escalation_level += 1
        # And it buys the customer quiet until the next rung is due. Widening
        # per rung rather than a flat gap, because the compliance complaint is
        # never about the first message — it is about the fourth one arriving as
        # fast as the first. `escalation_level` existed before this and decided
        # nothing; this is the line that makes it a bound rather than a counter.
        case.next_action_at = datetime.now(UTC) + timedelta(
            hours=get_settings().escalation_backoff_hours * case.escalation_level
        )


def close_case(case: RecoveryCase, state: CaseState, reason: str) -> None:
    """Move a case to a terminal state. Idempotent — first close wins."""
    if case.state in _TERMINAL:
        return
    case.state = state
    case.close_reason = reason
    case.closed_at = datetime.now(UTC)


async def attribute_capture(
    session: AsyncSession,
    *,
    amount: int,
    recovered_ref: str,
    link_id: str | None = None,
    idempotency_key: str | None = None,
    order_ref: str | None = None,
) -> RecoveryCase | None:
    """
    Credit a captured payment to the case that earned it.

    Resolution order, most specific first:
      1. `link_id`        — the Payment Link we sent. Direct hit on external_ref.
      2. `idempotency_key`— from the link's `notes`, which Razorpay copies onto
                            the payment. The executor has always written this
                            breadcrumb; nothing ever read it back until now.
      3. `order_ref`      — the order id, which survives across payment attempts
                            where payment ids do not. This is the customer paying
                            on their own: the money counts, but the engine does
                            not get the credit, so `amount_recovered` rises while
                            `recovered_via_attempt_id` stays NULL. Closing the
                            case still matters — otherwise it keeps spending
                            attempt budget chasing money already in the bank.

    Returns the case credited, or None if the capture is unrelated to any case we
    opened — the common path, since most payments never fail.
    """
    attempt: RetryAttempt | None = None
    if link_id:
        attempt = await _find_attempt(session, RetryAttempt.external_ref == link_id)
    if attempt is None and idempotency_key:
        attempt = await _find_attempt(
            session, RetryAttempt.idempotency_key == idempotency_key
        )

    case: RecoveryCase | None = None
    if attempt is not None and attempt.recovery_case_id is not None:
        case = await session.get(RecoveryCase, attempt.recovery_case_id)
    elif order_ref:
        case = await _find_case_by_order(session, order_ref)
        attempt = None  # self-recovery: no attempt of ours earned this

    if case is None:
        logger.info("Capture %s matched no recovery case", recovered_ref)
        return None

    case.amount_recovered += amount
    case.recovered_ref = recovered_ref
    case.recovered_at = datetime.now(UTC)
    if attempt is not None:
        case.recovered_via_attempt_id = attempt.id

    log_event(
        session,
        case,
        "attributed",
        amount=amount,
        recovered_ref=recovered_ref,
        attributed_to_attempt=str(attempt.id) if attempt is not None else None,
        matched_on=(
            "link_id" if link_id else "idempotency_key" if idempotency_key else "order_ref"
        ),
    )

    # Money arriving is what keeps a promise, whichever channel collected it —
    # the customer said they would pay and they did. Resolving here rather than
    # in the messaging layer means a self-recovery counts too, which is right:
    # the promise was kept, we just did not have to chase it.
    await resolve_promises(session, case, "kept", ref=recovered_ref)

    if case.amount_recovered >= case.amount_at_risk:
        close_case(
            case,
            "recovered",
            f"captured {case.amount_recovered} paise via {recovered_ref}"
            + (f" (attempt {attempt.attempt_number})" if attempt else " (self-recovery)"),
        )
        log_event(session, case, "closed", state="recovered", reason=case.close_reason)

    # Now that the case is resolved, the outstanding attempts against it are
    # moot. This is what the old `payment_id == <captured id>` update was trying
    # to do; resolving by case is why it now actually matches something.
    await session.execute(
        update(RetryAttempt)
        .where(
            RetryAttempt.recovery_case_id == case.id,
            RetryAttempt.result == "pending",
        )
        .values(result="superseded", executed_at=datetime.now(UTC))
    )

    logger.info(
        "Attributed %s paise to case %s (%s), attributed=%s",
        amount,
        case.id,
        case.state,
        attempt is not None,
    )
    return case


async def record_opt_out(session: AsyncSession, customer_id: str) -> int:
    """
    Withdraw consent and stop every open case for this customer.

    Distinct from `blocked_until`, which is a cooldown that expires on its own.
    An opt-out closes the work rather than deferring it: skipping one nudge and
    leaving the case open means the next scheduler tick contacts them again.
    Returns the number of cases closed.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        select(RetryLedger).where(RetryLedger.customer_id == customer_id)
    )
    ledger = result.scalar_one_or_none()
    if ledger is None:
        ledger = RetryLedger(customer_id=customer_id)
        session.add(ledger)
    ledger.consent_status = "opted_out"
    ledger.opted_out_at = now

    open_cases = await session.execute(
        select(RecoveryCase).where(
            RecoveryCase.customer_id == customer_id,
            RecoveryCase.state == "open",
        )
    )
    closed = 0
    for case in open_cases.scalars():
        close_case(case, "opted_out", "customer withdrew consent")
        # A pending promise on a case we will never chase again is not broken —
        # nobody is going to ask for the money. Marking it "broken" would libel
        # the customer in the one table a dispute would be settled from.
        await resolve_promises(session, case, "cancelled", ref=None)
        log_event(session, case, "opted_out", closed_state="opted_out", actor="customer")
        closed += 1
    logger.info("Opt-out recorded for %s, closed %d open cases", customer_id, closed)
    return closed


async def batch_summary(
    session: AsyncSession, batch_id: str | None = None
) -> dict[str, float | int]:
    """
    Measured money recovered across a batch, in paise.

    `recovered_paise` is everything that came back; `attributed_paise` is the
    subset a link we sent brought back. Quote the second one as the engine's
    result — the first includes customers who would have paid anyway, and a
    headline that cannot separate them is taking credit for the control group.
    """
    where = [RecoveryCase.batch_id == batch_id] if batch_id else []
    result = await session.execute(
        select(
            func.count(RecoveryCase.id),
            func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0),
            func.coalesce(func.sum(RecoveryCase.amount_recovered), 0),
            func.coalesce(func.sum(RecoveryCase.attempts_used), 0),
        ).where(*where)
    )
    cases, at_risk, recovered, attempts = result.one()

    attributed = await session.execute(
        select(func.coalesce(func.sum(RecoveryCase.amount_recovered), 0)).where(
            RecoveryCase.recovered_via_attempt_id.is_not(None), *where
        )
    )
    attributed_paise = int(attributed.scalar_one())

    return {
        "cases": int(cases),
        "at_risk_paise": int(at_risk),
        "recovered_paise": int(recovered),
        "attributed_paise": attributed_paise,
        "attempts_used": int(attempts),
        "recovery_rate_pct": round(recovered / at_risk * 100, 2) if at_risk else 0.0,
        "attributed_rate_pct": (
            round(attributed_paise / at_risk * 100, 2) if at_risk else 0.0
        ),
    }


# ── Audit trail ──────────────────────────────────────────────────────────────


def log_event(
    session: AsyncSession,
    case: RecoveryCase,
    event_type: CaseEventType,
    *,
    actor: str = "system",
    **detail: Any,
) -> None:
    """
    Append one row to the case's audit trail.

    In-memory like `attach_attempt` — `session.add` issues no I/O, so the caller
    still owns the commit and the audit row lands in the same transaction as the
    change it describes. That matters more than it looks: an audit written in a
    separate transaction can survive a rollback of the thing it claims happened.

    The case must already have an id, which means flushed. Every caller here is
    working with a case that came out of the database or was just flushed by
    `open_case`, so this holds — but it is why `open_case` logs after its flush
    rather than before.
    """
    session.add(
        CaseEvent(
            recovery_case_id=case.id,
            event_type=event_type,
            actor=actor,
            detail=detail or None,
        )
    )


# ── Promises to pay ──────────────────────────────────────────────────────────


async def record_promise(
    session: AsyncSession,
    case: RecoveryCase,
    *,
    amount: int,
    due_at: datetime,
    channel: str | None = None,
    language: str | None = None,
    source_ref: str | None = None,
    notes: str | None = None,
) -> PromiseToPay:
    """
    Log a commitment to pay by a date, and go quiet until then.

    The only customer response that should make the workflow quieter. Chasing
    someone who answered and named a date is both the fastest way to lose the
    money and the thing a complaint is actually about.

    `next_action_at` takes the LATER of the promise date and whatever the
    escalation ladder had already scheduled. The promise is permission to wait,
    not permission to contact sooner — a customer who says "tomorrow" an hour
    after a nudge does not reset the contact-frequency floor.
    """
    promise = PromiseToPay(
        recovery_case_id=case.id,
        customer_id=case.customer_id,
        amount_promised=amount,
        due_at=due_at,
        channel=channel,
        language=language,
        source_ref=source_ref,
        notes=notes,
        status="pending",
    )
    session.add(promise)
    await session.flush()

    case.next_action_at = (
        due_at if case.next_action_at is None else max(case.next_action_at, due_at)
    )
    log_event(
        session,
        case,
        "promise_made",
        actor="customer",
        promise_id=str(promise.id),
        amount=amount,
        due_at=due_at.isoformat(),
        channel=channel,
    )
    logger.info(
        "Promise recorded: case=%s amount=%s due=%s channel=%s",
        case.id, amount, due_at.isoformat(), channel,
    )
    return promise


async def resolve_promises(
    session: AsyncSession,
    case: RecoveryCase,
    status: PromiseResolution,
    *,
    ref: str | None = None,
) -> int:
    """
    Close out every pending promise on a case. Returns how many moved.

    Selected before updating rather than issuing a bulk UPDATE, because each
    promise gets its own audit row — "the promise was kept" is a per-promise
    fact, and a dispute is about one of them, not the set.
    """
    result = await session.execute(
        select(PromiseToPay).where(
            PromiseToPay.recovery_case_id == case.id,
            PromiseToPay.status == "pending",
        )
    )
    now = datetime.now(UTC)
    moved = 0
    for promise in result.scalars():
        promise.status = status
        promise.resolved_at = now
        promise.resolved_ref = ref
        log_event(
            session,
            case,
            _PROMISE_EVENT[status],
            promise_id=str(promise.id),
            amount=promise.amount_promised,
            due_at=promise.due_at.isoformat(),
            resolved_ref=ref,
        )
        moved += 1
    return moved


async def expire_promises(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 500
) -> int:
    """
    Mark promises broken once their date has passed with no money, and hand the
    case back to the chaser. Returns how many broke.

    A promise is broken on the clock, never on suspicion — this function is the
    only thing that makes that transition, so nothing upstream has to guess. It
    It also pulls `next_action_at` back to now, which is the half that matters
    operationally: without it the case stays deferred to the promise date and
    the silence the promise bought becomes permanent. Note "now" and not NULL —
    NULL is reserved for webhook-driven cases and `due_cases()` skips it, so
    clearing the column would release the case into a queue nothing reads.

    Cases already closed are skipped. Their promise still resolves — the ledger
    should not carry a pending promise forever — but a recovered or opted-out
    case must not be made chaseable again.
    """
    now = now or datetime.now(UTC)
    result = await session.execute(
        select(PromiseToPay)
        .where(PromiseToPay.status == "pending", PromiseToPay.due_at <= now)
        .order_by(PromiseToPay.due_at)
        .limit(limit)
    )
    broken = 0
    for promise in result.scalars():
        promise.status = "broken"
        promise.resolved_at = now
        case = await session.get(RecoveryCase, promise.recovery_case_id)
        if case is None:  # pragma: no cover — orphan promise, nothing to release
            continue
        if case.state == "open":
            case.next_action_at = now
        log_event(
            session,
            case,
            "promise_broken",
            promise_id=str(promise.id),
            amount=promise.amount_promised,
            due_at=promise.due_at.isoformat(),
            case_state=case.state,
        )
        broken += 1
    if broken:
        logger.info("Promises expired: %d broken as of %s", broken, now.isoformat())
    return broken


# ── Finding work ─────────────────────────────────────────────────────────────


async def due_cases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    risk_type: str | None = None,
    limit: int = 100,
) -> list[RecoveryCase]:
    """
    Open cases whose wait has elapsed, oldest first.

    The entry point for every risk type that has no inbound webhook. A card
    decline announces itself; an overdue invoice, a cold cart and a subscription
    that quietly stopped charging do not, so something has to go looking, and
    `next_action_at` is what it looks at.

    Deliberately excludes cases with `next_action_at IS NULL`. Those are
    webhook-driven — the event is their trigger — and sweeping them would run
    the pipeline a second time on every payment failure ever recorded.
    """
    now = now or datetime.now(UTC)
    conditions = [
        RecoveryCase.state == "open",
        RecoveryCase.next_action_at.is_not(None),
        RecoveryCase.next_action_at <= now,
    ]
    if risk_type is not None:
        conditions.append(RecoveryCase.risk_type == risk_type)
    result = await session.execute(
        select(RecoveryCase)
        .where(*conditions)
        .order_by(RecoveryCase.next_action_at)
        .limit(limit)
    )
    return list(result.scalars())


async def _find_attempt(session: AsyncSession, condition: object) -> RetryAttempt | None:
    """Most recent attempt matching a condition — newest wins on re-sends."""
    result = await session.execute(
        select(RetryAttempt)
        .where(condition)  # type: ignore[arg-type]
        .order_by(RetryAttempt.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _find_case_by_order(session: AsyncSession, order_ref: str) -> RecoveryCase | None:
    """
    The open case for an order, reached through the failure that opened it.

    Cases are keyed by payment id, but a customer's own successful retry carries a
    payment id we have never seen. The order id is the only value common to the
    failure and the recovery, which is why this hop through payment_failures
    exists. Restricted to open cases so a second capture on the same order cannot
    credit a case twice.
    """
    result = await session.execute(
        select(RecoveryCase)
        .join(PaymentFailure, PaymentFailure.payment_id == RecoveryCase.subject_ref)
        .where(PaymentFailure.order_id == order_ref, RecoveryCase.state == "open")
        .order_by(RecoveryCase.opened_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
