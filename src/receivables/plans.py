"""Payment plans — instalments over one case, built on the promise machinery.

The lazy-but-correct move: a plan is a GROUP of promises, not a new state
machine. Each instalment is created as a PromiseToPay via the existing
``record_promise`` — which means:

  * the pause (next_action_at pushed to the instalment's date) is already
    enforced by stop_reason() — zero new code
  * the audit event (promise_made per instalment) is already written
  * a missed instalment is already BROKEN by the existing expire_promises
    sweep, on the clock, with its own audit event — the sweep this module
    only needs to watch to detect a default

What a plan adds over raw promises is the grouping and the verdicts a
merchant asks for: "is this buyer on a plan", "did the plan complete", "did
it default". Those are plan-level facts, derived from instalment states,
never stored twice.

This module is standalone-safe: it works on any RecoveryCase rows the caller
provides. Integration wires plan_requested alerts and the default detector
into the promise-expiry sweep.

Settlements: a plan may carry a settlement amount below the principal — the
merchant's approved reduced payoff. The delta is visible on the plan row for
honest reporting; it never rewrites case.amount_at_risk (the money
truthfully owed), and a settled plan completes only when the settlement
amount is fully paid.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.receivables.alerts import raise_alert
from src.receivables.models import PaymentPlan, PlanInstalment

if TYPE_CHECKING:
    from src.models import RecoveryCase

logger = logging.getLogger(__name__)

# Plan shape bounds — frozen like the chaser policies, and for the same
# reason: a plan a typo can turn into "365 daily instalments" is a product
# bug, not a configuration option. 90 days is the longest horizon an
# invoice-chasing consent window (30d) can honestly grow to accommodate a
# plan; anything longer is a restructured commercial agreement, not a
# recovery plan.
MAX_INSTALMENTS = 6
MAX_PLAN_HORIZON_DAYS = 90


def validate_plan_shape(
    amounts_paise: list[int], due_dates: list[datetime], *, principal_paise: int
) -> str | None:
    """None when the shape is legal, else a human-readable refusal reason.

    Pure function so the recovery page, the API and tests share one law:
      * instalments 2..MAX (one instalment is not a plan, it is a promise —
        record_promise already exists for that)
      * every amount positive, every date timezone-aware
      * amounts sum to the principal (or the settlement when one is set —
        checked by the caller, which knows the settlement figure)
      * dates strictly increasing, within the horizon
    """
    n = len(amounts_paise)
    if not (2 <= n <= MAX_INSTALMENTS):
        return f"a plan needs 2 to {MAX_INSTALMENTS} instalments"
    if len(due_dates) != n:
        return "each instalment needs a due date"
    if any((not isinstance(a, int)) or isinstance(a, bool) or a <= 0 for a in amounts_paise):
        return "every instalment amount must be a positive integer (paise)"
    if any(d.tzinfo is None for d in due_dates):
        return "instalment dates must be timezone-aware"
    if sum(amounts_paise) != principal_paise:
        return "instalments must sum to the outstanding amount"
    if any(b <= a for a, b in zip(due_dates, due_dates[1:], strict=False)):
        return "instalment dates must be strictly increasing"
    if (due_dates[-1] - due_dates[0]).days > MAX_PLAN_HORIZON_DAYS:
        return f"the plan must fit inside {MAX_PLAN_HORIZON_DAYS} days"
    return None


async def create_plan(
    session: AsyncSession,
    case: RecoveryCase,
    *,
    amounts_paise: list[int],
    due_dates: list[datetime],
    settlement_paise: int | None = None,
    now: datetime | None = None,
) -> PaymentPlan | None:
    """
    Create a plan on a case. None when refused.

    Refusals (each returns None, the caller surfaces the reason):
      * case terminal or already carries an active plan
      * shape invalid (validate_plan_shape)
      * a settlement below the smallest instalment makes no sense

    Each instalment's promise is recorded with the instalment's seq in
    source_ref, so plan_instalments.promise_id → promises_to_pay is the join
    and "which promise is this instalment" is one query, forever.
    """
    now = now or datetime.now(UTC)

    from src.cases import _TERMINAL

    if case.state in _TERMINAL:
        return None

    active = await session.scalar(
        select(PaymentPlan).where(
            PaymentPlan.case_id == case.id, PaymentPlan.status == "active"
        )
    )
    if active is not None:
        return None

    # The plan covers what is STILL OWED, not what was owed when the case
    # opened. `amount_at_risk` never shrinks, so on a part-paid invoice the
    # old target asked a customer to schedule money they had already sent —
    # and then refused the plan for not summing to it. Same rule the recovery
    # page's /promise route and the minted link already follow.
    from src.cases import outstanding_paise

    outstanding = outstanding_paise(case)
    if outstanding <= 0:
        # Nothing left to schedule. The stopping rules should have closed this
        # case already; refusing here says why instead of building an empty plan.
        return None

    target = settlement_paise if settlement_paise is not None else outstanding
    if settlement_paise is not None and settlement_paise <= 0:
        return None
    if any(a > target for a in amounts_paise):
        return None
    problem = validate_plan_shape(amounts_paise, due_dates, principal_paise=target)
    if problem is not None:
        logger.info("Plan refused on %s: %s", case.subject_ref, problem)
        return None

    plan = PaymentPlan(
        case_id=case.id,
        # What this plan is responsible for collecting — the balance at the
        # moment it was agreed. NOT amount_at_risk: a plan that says it will
        # collect money the customer already paid cannot be reconciled against
        # reality later.
        principal_paise=outstanding,
        settlement_paise=settlement_paise,
        status="active",
    )
    session.add(plan)
    await session.flush()

    from src.cases import record_promise

    for seq, (amount, due) in enumerate(zip(amounts_paise, due_dates, strict=True), start=1):
        promise = await record_promise(
            session,
            case,
            amount=amount,
            due_at=due,
            channel="payment_plan",
            source_ref=f"plan:{plan.id}:{seq}",
        )
        if promise is None:  # pragma: no cover — plan instalments are cap-exempt
            raise RuntimeError(f"plan instalment {seq} refused on {case.subject_ref}")
        session.add(
            PlanInstalment(
                plan_id=plan.id,
                seq=seq,
                amount_paise=amount,
                due_at=due,
                promise_id=promise.id,
            )
        )

    await raise_alert(
        session,
        event_type="plan_requested",
        case_ref=case.subject_ref,
        detail={
            "instalments": len(amounts_paise),
            "total": sum(amounts_paise),
            "settlement": settlement_paise,
        },
    )
    logger.info(
        "Plan created: case=%s ref=%s instalments=%d total=%d",
        case.id, case.subject_ref, len(amounts_paise), sum(amounts_paise),
    )
    return plan


async def instalments_for_plan(
    session: AsyncSession, plan: PaymentPlan
) -> list[Row[tuple[PlanInstalment, Any]]]:
    """
    The plan's instalments with their promise rows, in seq order.

    One query: the promise join is the point of the table — instalment state
    IS promise state, so the caller reads status off the joined row without
    a second copy anywhere.
    """
    from src.models import PromiseToPay

    result = await session.execute(
        select(PlanInstalment, PromiseToPay)
        .join(PromiseToPay, PlanInstalment.promise_id == PromiseToPay.id)
        .where(PlanInstalment.plan_id == plan.id)
        .order_by(PlanInstalment.seq)
    )
    return list(result.all())


async def plan_progress(
    session: AsyncSession, plan: PaymentPlan
) -> dict[str, object]:
    """
    Where the plan stands: counts by derived instalment state and the two
    plan-level verdicts (completed / defaulted) as booleans, not stored state.

    completed  — every instalment's promise is kept, and the case's recovered
                 amount has reached the settlement (or principal).
    defaulted  — any instalment's promise is broken. The ratchet in the
                 ladder module resumes the chase at the next firmer rung;
                 this verdict is the merchant-facing fact of the default.
    """
    from src.models import PromiseToPay, RecoveryCase

    rows = (
        await session.execute(
            select(PlanInstalment, PromiseToPay)
            .join(PromiseToPay, PlanInstalment.promise_id == PromiseToPay.id)
            .where(PlanInstalment.plan_id == plan.id)
            .order_by(PlanInstalment.seq)
        )
    ).all()

    kept = sum(1 for _, p in rows if p.status == "kept")
    broken = sum(1 for _, p in rows if p.status == "broken")
    pending = sum(1 for _, p in rows if p.status == "pending")
    target = plan.settlement_paise or plan.principal_paise

    case = await session.get(RecoveryCase, plan.case_id)
    recovered = case.amount_recovered if case is not None else 0

    return {
        "total": len(rows),
        "kept": kept,
        "broken": broken,
        "pending": pending,
        "completed": kept == len(rows) and recovered >= target,
        "defaulted": broken > 0,
    }


async def missed_instalments(
    session: AsyncSession, *, now: datetime | None = None
) -> list[Row[tuple[PlanInstalment, Any]]]:
    """
    Instalments whose promise the expiry sweep has broken — the default feed.

    The reconcile pass (below) consumes this; standalone it is the query a
    merchant-facing "plans in trouble" view renders from.
    """
    from src.models import PromiseToPay

    rows = (
        await session.execute(
            select(PlanInstalment, PromiseToPay)
            .join(PromiseToPay, PlanInstalment.promise_id == PromiseToPay.id)
            .where(PromiseToPay.status == "broken")
            .order_by(PlanInstalment.due_at)
        )
    ).all()
    return list(rows)


async def reconcile_plans(session: AsyncSession, *, limit: int = 100) -> int:
    """
    Move plans to their verdicts and alert the merchant. Returns how many moved.

    Two transitions, both derived (never stored until proven):

      defaulted  — any instalment's promise broke. The expiry sweep already
                   re-armed the case and the ratchet already resumed the
                   chase at the next firmer rung; this pass only stamps the
                   plan, cancels the instalments that will never be paid,
                   and raises the merchant alerts (one per missed instalment,
                   plus the plan-level default).
      completed — the case recovered (full capture or external payment) and
                   every instalment promise resolved. Pending promises on a
                   recovered case are resolved kept by attribution; this pass
                   stamps the plan and raises plan_completed.

    Runs after expire_promises in the tick, so a missed instalment's promise
    is already broken by the time it looks — one clock, one authority.
    """

    from src.models import PromiseToPay, RecoveryCase
    from src.receivables.alerts import raise_alert

    moved = 0

    # ── Defaults: active plans with any broken instalment promise ──────
    # Ordered, because it is limited. An unordered LIMIT lets the database
    # return any 100 rows it likes, so past 100 active plans some could go
    # unreconciled indefinitely while others were checked every tick — a
    # defaulted plan that never gets its verdict is money nobody is told
    # about. Oldest first: the plan that has been waiting longest is the one
    # whose verdict is most overdue.
    active_plans = (
        await session.execute(
            select(PaymentPlan)
            .where(PaymentPlan.status == "active")
            .order_by(PaymentPlan.created_at)
            .limit(limit)
        )
    ).scalars().all()

    for plan in active_plans:
        rows = (
            await session.execute(
                select(PlanInstalment, PromiseToPay)
                .join(PromiseToPay, PlanInstalment.promise_id == PromiseToPay.id)
                .where(PlanInstalment.plan_id == plan.id)
                .order_by(PlanInstalment.seq)
            )
        ).all()
        broken = [(inst, promise) for inst, promise in rows if promise.status == "broken"]
        case = await session.get(RecoveryCase, plan.case_id)
        case_ref = case.subject_ref if case is not None else None

        if broken:
            plan.status = "defaulted"
            # Future instalments will never be paid; their promises rest as
            # pending forever unless cancelled. resolve_promises (one call,
            # the shared machinery) writes the per-promise audit events.
            from src.cases import resolve_promises

            if case is not None and case.state == "open":
                await resolve_promises(session, case, "cancelled", ref=str(plan.id))
            for inst, promise in broken:
                await raise_alert(
                    session,
                    event_type="plan_instalment_missed",
                    case_ref=case_ref,
                    detail={
                        "plan_id": str(plan.id),
                        "seq": inst.seq,
                        "amount_paise": inst.amount_paise,
                        "due_at": inst.due_at.isoformat(),
                    },
                )
            await raise_alert(
                session,
                event_type="plan_defaulted",
                case_ref=case_ref,
                detail={"plan_id": str(plan.id), "missed": len(broken)},
            )
            logger.info(
                "Plan defaulted: plan=%s case=%s missed=%d",
                plan.id, plan.case_id, len(broken),
            )
            moved += 1
            continue

        # ── Completion: recovered case + every promise resolved ────────
        if case is not None and case.state == "recovered":
            all_resolved = all(promise.status in ("kept", "broken") for _, promise in rows)
            target = plan.settlement_paise or plan.principal_paise
            if all_resolved and case.amount_recovered >= target:
                plan.status = "completed"
                plan.completed_at = datetime.now(UTC)
                await raise_alert(
                    session,
                    event_type="plan_completed",
                    case_ref=case_ref,
                    detail={"plan_id": str(plan.id), "total_paise": target},
                )
                logger.info("Plan completed: plan=%s case=%s", plan.id, plan.case_id)
                moved += 1

    if moved:
        await session.commit()
    return moved
