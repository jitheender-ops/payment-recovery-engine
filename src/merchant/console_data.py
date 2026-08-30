"""
The live console's read layer — every query behind /console/live, in one place.

Split out of routes.py because these are the numbers a merchant makes
decisions on, and they deserve to be testable without standing up HTTP, a
session cookie and a template. routes.py keeps the auth, the gating and the
rendering; this module answers "what is true right now".

Three rules hold every function here:

AGGREGATE AND PII-FREE. Counts, totals, and the merchant's OWN references
(invoice numbers, cart ids). Never a customer email, phone, or id — that is
the console's contract (PRODUCT.md), and it is enforced by not selecting the
columns rather than by remembering to filter them later.

ORM CONSTRUCTS, NOT RAW SQL STRINGS. The console shipped a
`WHERE delivered = 0` against a Boolean column: SQLite accepted the integer
compare, Postgres refused it outright, and the alerts feed was permanently
empty in production while every test passed. `delivered.is_(False)` renders
correctly on both. Portability here is not neatness — it is the difference
between a panel that works and one that silently shows nothing.

OUTSTANDING, NOT AT-RISK. `amount_at_risk` never shrinks; a part-paid case
still shows its original figure. Anything a merchant reads as "owed" is
at_risk minus recovered — the same rule cases.outstanding_paise() enforces on
the money paths.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.formatting import ist as _ist
from src.formatting import money as _money
from src.models import (
    CaseEvent,
    PromiseToPay,
    RecoveryCase,
    RetryAttempt,
    VoiceCallQueue,
)
from src.receivables.models import (
    AccountTask,
    ArAccount,
    ArContactLog,
    CaseDispute,
    PaymentPlan,
    PlanInstalment,
)

logger = logging.getLogger(__name__)

# How many rows each "what needs a human" list shows. Small on purpose: these
# are worklists, not exports. A merchant with forty open disputes has a
# problem no table length solves.
_LIST_LIMIT = 8


async def promise_panel(session: AsyncSession) -> dict[str, Any]:
    """
    The promise-to-pay tracker, as the merchant reads it.

    `kept_rate` is over RESOLVED promises only — kept / (kept + broken).
    Counting pending ones as failures would make every fresh promise look like
    a broken one, and the rate would climb on its own as time passed rather
    than as customers paid. None when nothing has resolved: an honest "no
    signal yet", never a 0% that reads as "nobody pays us".

    `kept_late` is the honesty split the kept_late_days column exists for — a
    promise paid five days into the grace window is kept, and calling it
    on-time would flatter the number the forecast depends on.
    """
    row = (
        await session.execute(
            select(
                func.count(PromiseToPay.id),
                func.count(PromiseToPay.id).filter(PromiseToPay.status == "pending"),
                func.count(PromiseToPay.id).filter(PromiseToPay.status == "kept"),
                func.count(PromiseToPay.id).filter(PromiseToPay.status == "broken"),
                func.count(PromiseToPay.id).filter(
                    PromiseToPay.status == "kept", PromiseToPay.kept_late_days > 0
                ),
                func.coalesce(
                    func.sum(PromiseToPay.amount_promised).filter(
                        PromiseToPay.status == "pending"
                    ),
                    0,
                ),
            )
        )
    ).one()
    total, pending, kept, broken, kept_late, pending_paise = (int(v) for v in row)
    resolved = kept + broken

    # Promises coming due, with the case they belong to. The merchant's own
    # reference only — a promise is a commitment about an invoice, and the
    # invoice number is what they can act on.
    upcoming_rows = (
        await session.execute(
            select(
                PromiseToPay.due_at,
                PromiseToPay.amount_promised,
                PromiseToPay.channel,
                PromiseToPay.reminded_at,
                RecoveryCase.subject_ref,
                RecoveryCase.risk_type,
            )
            .join(RecoveryCase, RecoveryCase.id == PromiseToPay.recovery_case_id)
            .where(PromiseToPay.status == "pending")
            .order_by(PromiseToPay.due_at)
            .limit(_LIST_LIMIT)
        )
    ).mappings().all()

    upcoming = [
        {
            "due": _ist(r["due_at"]).strftime("%d %b") if r["due_at"] else "—",
            "amount": _money(int(r["amount_promised"])),
            "channel": r["channel"] or "—",
            "subject_ref": r["subject_ref"],
            "risk_type": r["risk_type"],
            # The 48h pre-due reminder is one-shot; showing whether it has
            # fired is how a merchant knows the promise is being worked.
            "reminded": r["reminded_at"] is not None,
        }
        for r in upcoming_rows
    ]

    return {
        "total": total,
        "pending": pending,
        "kept": kept,
        "broken": broken,
        "kept_late": kept_late,
        "kept_on_time": kept - kept_late,
        "kept_rate": round(kept / resolved * 100, 1) if resolved else None,
        "pending_amount": _money(pending_paise),
        "upcoming": upcoming,
    }


async def plan_panel(session: AsyncSession) -> dict[str, Any]:
    """
    Payment plans, and how far through their instalments they are.

    A plan is a GROUP of promises (src/receivables/plans.py), so progress is
    derived from the instalment promises rather than stored twice — the same
    reason plan_progress() computes its verdicts instead of trusting a column.
    """
    counts = (
        await session.execute(
            select(
                func.count(PaymentPlan.id),
                func.count(PaymentPlan.id).filter(PaymentPlan.status == "active"),
                func.count(PaymentPlan.id).filter(PaymentPlan.status == "completed"),
                func.count(PaymentPlan.id).filter(PaymentPlan.status == "defaulted"),
            )
        )
    ).one()
    total, active, completed, defaulted = (int(v) for v in counts)

    plan_rows = (
        await session.execute(
            select(
                PaymentPlan.id,
                PaymentPlan.status,
                PaymentPlan.principal_paise,
                PaymentPlan.settlement_paise,
                RecoveryCase.subject_ref,
                func.count(PlanInstalment.id).label("instalments"),
                func.count(PromiseToPay.id)
                .filter(PromiseToPay.status == "kept")
                .label("paid"),
            )
            .join(RecoveryCase, RecoveryCase.id == PaymentPlan.case_id)
            .outerjoin(PlanInstalment, PlanInstalment.plan_id == PaymentPlan.id)
            .outerjoin(PromiseToPay, PromiseToPay.id == PlanInstalment.promise_id)
            .where(PaymentPlan.status == "active")
            .group_by(
                PaymentPlan.id,
                PaymentPlan.status,
                PaymentPlan.principal_paise,
                PaymentPlan.settlement_paise,
                RecoveryCase.subject_ref,
            )
            .order_by(PaymentPlan.created_at.desc())
            .limit(_LIST_LIMIT)
        )
    ).mappings().all()

    plans = [
        {
            "subject_ref": r["subject_ref"],
            "status": r["status"],
            # The settlement, when the merchant approved a reduced payoff —
            # that is the figure the plan actually completes against, and
            # showing the principal instead would overstate what is coming.
            "target": _money(int(r["settlement_paise"] or r["principal_paise"])),
            "settled": r["settlement_paise"] is not None,
            "instalments": int(r["instalments"]),
            "paid": int(r["paid"]),
        }
        for r in plan_rows
    ]

    return {
        "total": total,
        "active": active,
        "completed": completed,
        "defaulted": defaulted,
        "plans": plans,
    }


async def dispute_panel(session: AsyncSession) -> dict[str, Any]:
    """
    Open disputes — the one panel that is a WORKLIST, not a metric.

    An open dispute freezes the chase (orchestrator.chase_case) and stays
    frozen until a human decides. Nothing in the engine resolves one, by
    design: "this invoice is wrong" is a commercial judgement. So an open
    dispute is money that has stopped moving and will not restart on its own,
    which is exactly the thing a dashboard exists to put in front of someone.
    """
    counts = (
        await session.execute(
            select(
                func.count(CaseDispute.id).filter(CaseDispute.status == "open"),
                func.count(CaseDispute.id).filter(CaseDispute.status == "upheld"),
                func.count(CaseDispute.id).filter(CaseDispute.status == "rejected"),
            )
        )
    ).one()
    open_count, upheld, rejected = (int(v) for v in counts)

    rows = (
        await session.execute(
            select(
                CaseDispute.reason,
                CaseDispute.opened_at,
                RecoveryCase.subject_ref,
                RecoveryCase.risk_type,
                RecoveryCase.amount_at_risk,
                RecoveryCase.amount_recovered,
            )
            .join(RecoveryCase, RecoveryCase.id == CaseDispute.case_id)
            .where(CaseDispute.status == "open")
            .order_by(CaseDispute.opened_at)
            .limit(_LIST_LIMIT)
        )
    ).mappings().all()

    disputes = [
        {
            "subject_ref": r["subject_ref"],
            "risk_type": r["risk_type"],
            "outstanding": _money(
                max(0, int(r["amount_at_risk"]) - int(r["amount_recovered"] or 0))
            ),
            # The customer's own words, trimmed. open_dispute already bounds
            # this at write time; the trim is for the row height.
            "reason": (r["reason"] or "")[:120],
            "opened": _ist(r["opened_at"]).strftime("%d %b") if r["opened_at"] else "—",
            "days_open": (
                (datetime.now(UTC) - _aware(r["opened_at"])).days
                if r["opened_at"]
                else 0
            ),
        }
        for r in rows
    ]

    return {
        "open": open_count,
        "upheld": upheld,
        "rejected": rejected,
        "disputes": disputes,
    }


async def voice_panel(session: AsyncSession) -> dict[str, Any]:
    """
    The voice chaser's queue depth, by state.

    The engine never dials — it queues work items a telephony leg claims
    (models.VoiceCallQueue). So the number that matters operationally is
    whether anything is CLAIMING them: a growing `queued` count with nothing
    moving to `done` means the call leg is down, and no other surface would
    say so.
    """
    counts = (
        await session.execute(
            select(
                func.count(VoiceCallQueue.id).filter(VoiceCallQueue.state == "queued"),
                func.count(VoiceCallQueue.id).filter(VoiceCallQueue.state == "claimed"),
                func.count(VoiceCallQueue.id).filter(VoiceCallQueue.state == "done"),
                func.count(VoiceCallQueue.id).filter(VoiceCallQueue.state == "failed"),
                func.count(VoiceCallQueue.id).filter(
                    VoiceCallQueue.state == "opted_out"
                ),
            )
        )
    ).one()
    queued, claimed, done, failed, opted_out = (int(v) for v in counts)

    oldest = await session.scalar(
        select(func.min(VoiceCallQueue.created_at)).where(
            VoiceCallQueue.state == "queued"
        )
    )

    return {
        "queued": queued,
        "claimed": claimed,
        "done": done,
        "failed": failed,
        "opted_out": opted_out,
        "total": queued + claimed + done + failed + opted_out,
        # A queue with an old head and no movement is the signal; the age is
        # what makes "3 queued" either fine or an outage.
        "oldest_queued": (
            _ist(oldest).strftime("%d %b, %H:%M") if oldest is not None else None
        ),
    }


async def ladder_panel(session: AsyncSession) -> dict[str, Any]:
    """
    Where each B2B account sits on the dunning ladder.

    `ar_contact_log` records one row per rung fired per account, so the LATEST
    row's stage_level is the account's current rung. Counting rows per stage
    across all history would answer a different question ("how many rungs have
    we ever fired") and read as if every account were at every stage at once.
    """
    from src.receivables.ladder import INVOICE_LADDER

    # The newest log row per account — the account's current rung.
    newest = (
        select(
            ArContactLog.account_id.label("account_id"),
            func.max(ArContactLog.created_at).label("newest_at"),
        )
        .group_by(ArContactLog.account_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(ArContactLog.stage_level, func.count(ArContactLog.id))
            .join(
                newest,
                (newest.c.account_id == ArContactLog.account_id)
                & (newest.c.newest_at == ArContactLog.created_at),
            )
            .group_by(ArContactLog.stage_level)
        )
    ).all()
    by_stage = {int(level): int(count) for level, count in rows}

    stages = [
        {
            "level": stage.level,
            "tone": stage.tone,
            "days": stage.days_past_due,
            "accounts": by_stage.get(stage.level, 0),
            "addresses": ", ".join(a.replace("_", " ") for a in stage.addresses),
        }
        for stage in INVOICE_LADDER
        if not stage.pre_due  # stage 0 is opt-in and not reachable by default
    ]

    accounts = int(await session.scalar(select(func.count(ArAccount.id))) or 0)
    open_tasks = int(
        await session.scalar(
            select(func.count(AccountTask.id)).where(AccountTask.status == "open")
        )
        or 0
    )

    return {
        "accounts": accounts,
        "stages": stages,
        # Stage 3 raises a human call task. It never spends customer-contact
        # budget — it is merchant-side work, and it sits here undone until
        # somebody picks up a phone.
        "open_call_tasks": open_tasks,
    }


async def activity_feed(session: AsyncSession, *, limit: int = 12) -> list[dict[str, Any]]:
    """
    The case audit trail, newest first — what the engine has been doing.

    case_events is append-only and already the answer to "why did this
    customer get contacted four times"; this is that trail rendered. The
    detail JSON is deliberately NOT shown: it carries free-form values from
    several writers, and the console's PII-free contract is easier to keep by
    showing the event type and the merchant's reference than by auditing every
    key that might ever land in a JSONB blob.
    """
    rows = (
        await session.execute(
            select(
                CaseEvent.event_type,
                CaseEvent.actor,
                CaseEvent.created_at,
                RecoveryCase.subject_ref,
                RecoveryCase.risk_type,
            )
            .join(RecoveryCase, RecoveryCase.id == CaseEvent.recovery_case_id)
            .order_by(CaseEvent.id.desc())
            .limit(limit)
        )
    ).mappings().all()

    return [
        {
            "event": r["event_type"],
            "actor": r["actor"],
            "subject_ref": r["subject_ref"],
            "risk_type": r["risk_type"],
            "when": (
                _ist(r["created_at"]).strftime("%d %b, %H:%M")
                if r["created_at"] is not None
                else ""
            ),
        }
        for r in rows
    ]


async def engine_health(session: AsyncSession) -> dict[str, Any]:
    """
    Is the engine actually running — the question every other number assumes.

    A console full of confident totals is worthless if the scheduler died
    three days ago: the numbers would simply stop changing, and nothing on the
    page would say why. The heartbeat row is the dead-man's switch
    (models.SchedulerHeartbeat); `stale` is the honest verdict rather than a
    raw timestamp the reader has to interpret.

    Stale threshold is generous — several times the default 60s tick — so a
    slow sweep or a cold Modal container never cries wolf.
    """
    from src.config import get_settings
    from src.models import SchedulerHeartbeat

    heartbeat = await session.get(SchedulerHeartbeat, 1)
    interval = get_settings().scheduler_interval_seconds
    stale_after = timedelta(seconds=max(300, interval * 5))

    if heartbeat is None or heartbeat.last_tick_at is None:
        return {"ticking": False, "last_tick": None, "stale": True, "counts": None}

    last = _aware(heartbeat.last_tick_at)
    age = datetime.now(UTC) - last
    return {
        "ticking": True,
        "last_tick": _ist(last).strftime("%d %b, %H:%M"),
        "age_seconds": int(age.total_seconds()),
        "stale": age > stale_after,
        "counts": heartbeat.last_tick_counts,
    }


async def outstanding_total(session: AsyncSession) -> dict[str, Any]:
    """
    What is genuinely still owed across OPEN cases, and how it splits.

    Not `SUM(amount_at_risk)`: that is what was owed when each case opened and
    never shrinks, so a book with heavy part-payment reads as if nothing had
    been collected. The console's headline "at risk" figure has to be the
    balance, or a merchant chasing it is chasing money they already have.
    """
    row = (
        await session.execute(
            select(
                func.count(RecoveryCase.id),
                func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0),
                func.coalesce(func.sum(RecoveryCase.amount_recovered), 0),
            ).where(RecoveryCase.state == "open")
        )
    ).one()
    open_cases, at_risk, part_paid = (int(v) for v in row)

    # Cases carrying a part payment: the reason the two figures differ, and a
    # number that makes the gap legible instead of looking like a rounding bug.
    part_paid_cases = int(
        await session.scalar(
            select(func.count(RecoveryCase.id)).where(
                RecoveryCase.state == "open",
                RecoveryCase.amount_recovered > 0,
            )
        )
        or 0
    )

    return {
        "open_cases": open_cases,
        "outstanding": _money(max(0, at_risk - part_paid)),
        "outstanding_paise": max(0, at_risk - part_paid),
        "part_paid": _money(part_paid),
        "part_paid_cases": part_paid_cases,
    }


async def in_flight(session: AsyncSession) -> dict[str, Any]:
    """
    Work the engine is mid-way through: live Razorpay calls and parked retries.

    `pending` is a write-ahead row whose outcome has not landed — a link may
    exist on Razorpay's side. `scheduled` is an agent decision to WAIT that the
    scheduler has not fired yet. They mean different things operationally and
    the old console summed them into one tile.
    """
    row = (
        await session.execute(
            select(
                func.count(RetryAttempt.id).filter(RetryAttempt.result == "pending"),
                func.count(RetryAttempt.id).filter(RetryAttempt.result == "scheduled"),
                func.count(RetryAttempt.id).filter(RetryAttempt.result == "success"),
            )
        )
    ).one()
    pending, scheduled, succeeded = (int(v) for v in row)
    return {"pending": pending, "scheduled": scheduled, "succeeded": succeeded}


def attention_items(
    *,
    disputes: dict[str, Any],
    voice: dict[str, Any],
    plans: dict[str, Any],
    exceptions: list[dict[str, Any]],
    health: dict[str, Any],
) -> list[dict[str, str]]:
    """
    The worklist: everything automation has deliberately stopped short of.

    Assembled from panels already fetched rather than re-querying — this is an
    editorial pass over known facts, not a new read.

    The engine is built to refuse: a dispute freezes a case until a person
    decides, a spent budget closes one, a queued call needs a telephony leg to
    claim it. Each of those is money that has stopped moving and will NOT
    restart on its own. On the old console they sat in the same visual weight
    as a vanity total, several screens down. Ranked here by what a merchant
    loses by ignoring it for a day.

    Returns [] when there is nothing — and the template says so out loud,
    because "nothing needs you" is a real answer and a blank space is not.
    """
    items: list[dict[str, str]] = []

    # First, because it invalidates everything else on the page: if the
    # scheduler is not ticking, every number below is a frozen snapshot and
    # no sweep is firing.
    if health.get("stale"):
        items.append(
            {
                "kind": "engine",
                "title": "The engine has stopped ticking",
                "detail": (
                    f"Last sweep {health['last_tick']}. Deferred retries, "
                    "promise expiry and the chase sweeps are not running."
                    if health.get("last_tick")
                    else "No sweep has run yet. Check the scheduler is enabled."
                ),
            }
        )

    if disputes["open"]:
        oldest = max((d["days_open"] for d in disputes["disputes"]), default=0)
        items.append(
            {
                "kind": "dispute",
                "title": (
                    f"{disputes['open']} invoice"
                    f"{'s' if disputes['open'] != 1 else ''} disputed"
                ),
                "detail": (
                    f"Chasing is frozen until you decide. Oldest waiting "
                    f"{oldest} day{'s' if oldest != 1 else ''}."
                ),
            }
        )

    # A queue with a head and nothing claiming it means the telephony leg is
    # down. No other surface would ever say so.
    if voice["queued"] and not voice["claimed"]:
        items.append(
            {
                "kind": "voice",
                "title": (
                    f"{voice['queued']} call"
                    f"{'s' if voice['queued'] != 1 else ''} waiting to be placed"
                ),
                "detail": (
                    f"Oldest queued {voice['oldest_queued']}. Nothing has "
                    "claimed them — check the call leg is running."
                    if voice["oldest_queued"]
                    else "Nothing has claimed them."
                ),
            }
        )

    if plans["defaulted"]:
        items.append(
            {
                "kind": "plan",
                "title": (
                    f"{plans['defaulted']} payment plan"
                    f"{'s' if plans['defaulted'] != 1 else ''} defaulted"
                ),
                "detail": "An instalment was missed. The chase resumed a rung firmer.",
            }
        )

    if exceptions:
        items.append(
            {
                "kind": "exhausted",
                "title": (
                    f"{len(exceptions)} case"
                    f"{'s' if len(exceptions) != 1 else ''} out of attempts"
                ),
                "detail": "The engine spent its budget without recovering these.",
            }
        )

    return items


def _aware(ts: datetime) -> datetime:
    """UTC-aware before meeting datetime.now(UTC) — SQLite returns naive."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
