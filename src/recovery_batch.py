"""
Batch recovery — bounded execution across a cohort, and the money it earned.

"We recovered a payment" is an anecdote. "Across 2,014 eligible cases the
agent recovered ₹7.42L, and refused 467 the guardrail would not permit" is a
measurement, and it is the claim this engine exists to support.

Nothing here is a second recovery path. Cohort selection is a query; the
decision is `XGBoostBaseline` and `GuardrailGate`, the same objects the
orchestrator uses; execution goes through `RetryExecutor`, which in demo
mode is the local fake. A batch that shortcut any of that would be
measuring something other than the product.

THE PREVIEW IS THE POINT. `plan()` runs classification, the agent and the
guardrail over the cohort WITHOUT executing, so a merchant sees eligible /
blocked / approved and the reasons before anything moves money. Execution
then re-validates rather than trusting the preview — a preview is a
forecast, and acting on a forecast is how a bounded system stops being one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.actions import FailureContext, RetryAction
from src.cases import stop_reason
from src.formatting import IST
from src.formatting import money as _money
from src.guardrail.gate import GuardrailGate
from src.models import PaymentFailure, RecoveryCase, RetryAttempt, RetryLedger

logger = logging.getLogger(__name__)

# A batch is bounded like everything else here. Without a cap this is an
# unbounded write loop behind an HTTP request, which is the shape of every
# runaway job that has ever taken a payments system down.
MAX_COHORT = 500


@dataclass
class Candidate:
    """One case's verdict, before anything is executed."""

    case_id: uuid.UUID
    ref: str
    amount: int
    failure_class: str | None
    action: str | None = None
    rail: str | None = None
    confidence: float | None = None
    reason: str | None = None
    eligible: bool = False
    blocked_by: list[str] = field(default_factory=list)


@dataclass
class BatchPlan:
    """What the engine would do, and what it refuses to do."""

    candidates: list[Candidate]
    cohort_label: str

    @property
    def approved(self) -> list[Candidate]:
        return [c for c in self.candidates if c.eligible]

    @property
    def blocked(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.eligible]

    def summary(self) -> dict[str, Any]:
        approved, blocked = self.approved, self.blocked
        reasons: dict[str, int] = {}
        for c in blocked:
            for r in c.blocked_by:
                # Group on the rule, not the formatted detail: "blackout hour
                # 2" and "blackout hour 3" are one rule firing twice.
                reasons[r.split(":")[0]] = reasons.get(r.split(":")[0], 0) + 1
        return {
            "cohort": self.cohort_label,
            "eligible": len(self.candidates),
            "approved": len(approved),
            "blocked": len(blocked),
            "approved_value": _money(sum(c.amount for c in approved)),
            "blocked_value": _money(sum(c.amount for c in blocked)),
            "block_reasons": sorted(
                [{"rule": k, "cases": v} for k, v in reasons.items()],
                key=lambda r: int(r["cases"]), reverse=True,
            ),
        }


async def _context_for(
    session: AsyncSession, case: RecoveryCase, now: datetime
) -> FailureContext | None:
    """The 5-tuple the agent and guardrail both read, or None if unusable."""
    failure = await session.scalar(
        select(PaymentFailure).where(PaymentFailure.payment_id == case.subject_ref)
    )
    if failure is None:
        return None
    local = now.astimezone(IST)
    return FailureContext(
        payment_id=case.subject_ref,
        order_id=failure.order_id,
        failure_class=failure.failure_class,
        error_code=failure.error_code,
        error_description=failure.error_description,
        error_source=failure.error_source,
        error_reason=failure.error_reason,
        amount=max(0, case.amount_at_risk - case.amount_recovered),
        method=failure.method,
        bank=failure.bank,
        customer_id=case.customer_id,
        failed_at=failure.failed_at,
        current_time=now,
        # IST, because the blackout rule reads this as an IST hour.
        hour_of_day=local.hour,
        day_of_week=local.weekday(),
    )


async def plan(
    session: AsyncSession,
    *,
    failure_class: str | None = None,
    limit: int = 200,
    now: datetime | None = None,
) -> BatchPlan:
    """
    Score a cohort without touching it.

    Only open cases with budget left are considered — `stop_reason()` is
    consulted first, so a case the engine has already stopped never enters
    the batch at all. That ordering matters: a stopping rule that could be
    overridden by asking again is not a stopping rule.
    """
    now = now or datetime.now(UTC)
    limit = min(limit, MAX_COHORT)
    agent = _agent()
    gate = GuardrailGate()

    stmt = (
        select(RecoveryCase)
        .where(
            RecoveryCase.state == "open",
            RecoveryCase.risk_type == "payment_failure",
            RecoveryCase.attempts_used < RecoveryCase.max_attempts,
        )
        .order_by(RecoveryCase.amount_at_risk.desc())
        .limit(limit)
    )
    if failure_class:
        stmt = stmt.join(
            PaymentFailure, PaymentFailure.payment_id == RecoveryCase.subject_ref
        ).where(PaymentFailure.failure_class == failure_class)

    cases = list((await session.execute(stmt)).scalars().all())
    candidates: list[Candidate] = []

    for case in cases:
        ledger = None
        if case.customer_id:
            ledger = await session.scalar(
                select(RetryLedger).where(RetryLedger.customer_id == case.customer_id)
            )
        held = stop_reason(case, ledger)
        context = await _context_for(session, case, now)
        cand = Candidate(
            case_id=case.id, ref=case.subject_ref,
            amount=max(0, case.amount_at_risk - case.amount_recovered),
            failure_class=context.failure_class if context else None,
        )
        if held is not None:
            cand.blocked_by = [f"Stopping rule: {held}"]
            candidates.append(cand)
            continue
        if context is None:
            cand.blocked_by = ["No gateway failure on record for this case"]
            candidates.append(cand)
            continue

        action: RetryAction = agent.predict(context)
        cand.action, cand.rail = action.action, action.rail
        cand.confidence, cand.reason = action.confidence, action.reason
        verdict = gate.validate(
            action, context, f"batch_{case.id.hex}_{case.attempts_used}",
            current_attempts=case.attempts_used,
        )
        if not verdict.passed:
            cand.blocked_by = list(verdict.rejection_reasons)
        elif action.action == "abandon":
            # The guardrail passes an abandon — of course it does, abandon is
            # the safe action. But "approved" here means the batch will act on
            # it, and execute() then spends an attempt, bumps attempts_used and
            # counts the no-op as accepted (execute_retry returns success with
            # "No action taken"), so a cohort of hard declines would report
            # itself 100% accepted while doing nothing and exhausting every
            # case's budget. Nothing to chase is a refusal, with the policy's
            # own reason attached.
            cand.blocked_by = [f"Nothing to chase: {action.reason}"]
        else:
            cand.eligible = True
        candidates.append(cand)

    label = failure_class or "every open payment-rail case with budget left"
    return BatchPlan(candidates=candidates, cohort_label=label)


def _agent() -> Any:
    from src.agent.xgboost_baseline import XGBoostBaseline

    return XGBoostBaseline()


async def execute(
    session: AsyncSession,
    batch: BatchPlan,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Run the approved half, and report what the money actually did.

    Re-validates each case against the guardrail before acting rather than
    trusting `plan()`: the preview may be minutes old, and an attempt budget
    or a blackout boundary can have moved under it. A bounded system that
    acts on a stale approval is not bounded.
    """
    from src.executor.retry_executor import RetryExecutor

    now = now or datetime.now(UTC)
    executor = RetryExecutor()
    gate = GuardrailGate()
    agent = _agent()

    attempted = accepted = rejected = 0
    stale = 0

    # Money is measured, never inferred from the executor's return value.
    # `execute_retry` succeeding means a Payment Link was MINTED — the
    # customer has not paid anything yet. Counting that as recovery would
    # report a 100% recovery rate for a batch where nobody had paid, which
    # is the exact overclaim `recovered_via_attempt_id` exists to prevent
    # everywhere else in this codebase.
    #
    # So recovery is attributed to THIS RUN'S attempts specifically, by
    # collecting their ids and joining back through
    # recovered_via_attempt_id. Not a before/after snapshot of the cohort:
    # captures arrive asynchronously, so a snapshot would miss money that
    # lands a minute later and would credit this run for a payment an
    # earlier one earned.
    run_attempt_ids: list[uuid.UUID] = []

    for cand in batch.approved:
        case = await session.get(RecoveryCase, cand.case_id)
        if case is None or case.state != "open":
            stale += 1
            continue
        context = await _context_for(session, case, now)
        if context is None:
            stale += 1
            continue

        action = agent.predict(context)
        idem = f"batch_{case.id.hex}_{case.attempts_used}"
        verdict = gate.validate(
            action, context, idem, current_attempts=case.attempts_used
        )
        if not verdict.passed or action.action == "abandon":
            # Approved in the preview, refused now. Counted, not silently
            # skipped — this is the guardrail doing its job late, and hiding
            # it would make the batch look cleaner than it was. An abandon
            # lands here too: the re-prediction can differ from the preview's,
            # and an abandon must never reach the executor and spend an
            # attempt on a no-op.
            stale += 1
            continue

        failure = await session.scalar(
            select(PaymentFailure).where(
                PaymentFailure.payment_id == case.subject_ref
            )
        )
        if failure is None:
            # _context_for already found one, so this is a race rather than
            # a shape error — count it as stale rather than crashing a run
            # that has already moved money for earlier cases.
            stale += 1
            continue

        result: dict[str, Any]
        try:
            result = await executor.execute_retry(
                failure, action.action, action.rail, idem
            )
        except Exception:
            logger.exception("Batch attempt failed for case %s", case.id)
            result = {"success": False, "error": "executor raised"}

        attempted += 1
        # "accepted" = the gateway took the request and a link exists. NOT
        # "the customer paid" — that arrives later, on the capture webhook.
        ok = bool(result.get("success"))
        attempt = RetryAttempt(
            payment_id=case.subject_ref, idempotency_key=idem,
            attempt_number=case.attempts_used + 1, recovery_case_id=case.id,
            action_type=action.action, target_rail=action.rail,
            agent_type="xgboost", agent_reasoning=action.reason,
            agent_confidence=action.confidence, guardrail_passed=True,
            result="success" if ok else "failed", executed_at=now,
        )
        session.add(attempt)
        await session.flush()
        run_attempt_ids.append(attempt.id)
        case.attempts_used += 1
        if ok:
            accepted += 1
        else:
            rejected += 1
        if case.attempts_used >= case.max_attempts and case.state == "open":
            case.state = "exhausted"
            case.close_reason = (
                f"attempt budget spent ({case.max_attempts}/{case.max_attempts})"
            )
            case.closed_at = now
    await session.commit()

    won = (
        await session.execute(
            select(
                func.count(RecoveryCase.id),
                func.coalesce(func.sum(RecoveryCase.amount_recovered), 0),
            ).where(RecoveryCase.recovered_via_attempt_id.in_(run_attempt_ids))
        )
    ).one() if run_attempt_ids else (0, 0)
    recovered_cases, recovered_paise = int(won[0]), int(won[1])

    return {
        "attempted": attempted,
        "accepted": accepted,
        "rejected": rejected,
        "stale": stale,
        # Measured from case state, so it can only move when a capture
        # webhook has landed AND been attributed.
        "recovered": _money(recovered_paise),
        "recovered_paise": recovered_paise,
        "recovered_cases": recovered_cases,
        "at_risk_touched": _money(
            sum(c.amount for c in batch.approved[:attempted])
        ),
        "accept_rate": (
            round(100.0 * accepted / attempted, 1) if attempted else 0.0
        ),
        "ran_at": now.astimezone(IST).strftime("%d %b %Y, %H:%M IST"),
    }


async def cohorts(session: AsyncSession) -> list[dict[str, Any]]:
    """Failure classes with open, still-actionable cases — what is worth running."""
    rows = (
        await session.execute(
            select(
                PaymentFailure.failure_class,
                func.count(RecoveryCase.id),
                func.coalesce(
                    func.sum(RecoveryCase.amount_at_risk - RecoveryCase.amount_recovered),
                    0,
                ),
            )
            .join(RecoveryCase, RecoveryCase.subject_ref == PaymentFailure.payment_id)
            .where(
                RecoveryCase.state == "open",
                RecoveryCase.attempts_used < RecoveryCase.max_attempts,
            )
            .group_by(PaymentFailure.failure_class)
            .order_by(func.count(RecoveryCase.id).desc())
        )
    ).all()
    return [
        {"failure_class": r[0], "cases": int(r[1]), "value": _money(int(r[2]))}
        for r in rows
    ]
