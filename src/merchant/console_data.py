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
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import or_ as sa_or
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
from src.receivables.accounts import active_contacts
from src.receivables.models import (
    AccountTask,
    ArAccount,
    ArContact,
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
                PromiseToPay.mandate_status,
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
            # Whether this promise collects itself. The distinction is the
            # merchant's forecast: an autopay-backed promise is money arriving
            # on a date, a plain one is a commitment that still has to be
            # remembered by the person who made it.
            "autopay": r["mandate_status"] == "active",
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
                CaseDispute.id,
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
            # The id the console's uphold/reject form posts back. The panel
            # told the merchant to resolve these and offered no control for
            # three releases; a worklist you cannot act on is a to-do list
            # someone else is holding.
            "id": str(r["id"]),
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
    from src.receivables.ladder import (
        INVOICE_LADDER,
        is_b2b_contact_time,
        next_b2b_window,
        next_stage_gap_hours,
        stage_after_break,
    )

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
            # The rest of the rung, which the console has never shown. A
            # merchant could see WHERE their accounts sat and nothing about
            # what happens there — which channels fire, whether it costs a
            # contact, how long until the next rung, and where a broken
            # promise lands them. All of it is enforced; none of it was
            # legible.
            "channels": ", ".join(stage.channels),
            "spends_budget": stage.spends_budget,
            "gap_hours": next_stage_gap_hours(stage.level),
            "after_break": stage_after_break(stage.level),
        }
        for stage in INVOICE_LADDER
        if not stage.pre_due  # stage 0 is opt-in and not reachable by default
    ]

    # The B2B contact window, read from the enforcing function rather than
    # restated in copy — PRODUCT.md's rule. A merchant looking at a quiet
    # ladder at 8pm on Saturday should be told it is quiet on purpose.
    now_ist = datetime.now(UTC)
    window_open = is_b2b_contact_time(now_ist)
    next_window = next_b2b_window(now_ist)

    accounts = int(await session.scalar(select(func.count(ArAccount.id))) or 0)
    open_tasks = int(
        await session.scalar(
            select(func.count(AccountTask.id)).where(AccountTask.status == "open")
        )
        or 0
    )

    # The tasks themselves, not just the count. A bare number told a merchant
    # that work existed and nothing about WHICH work — no account, no reason,
    # no way to close it. `complete_task` has existed the whole time behind an
    # HMAC-signed endpoint a person on a laptop cannot reach.
    task_rows = (
        await session.execute(
            select(
                AccountTask.id,
                AccountTask.kind,
                AccountTask.detail,
                AccountTask.created_at,
                ArAccount.display_name,
                ArAccount.account_ref,
            )
            .join(ArAccount, ArAccount.id == AccountTask.account_id)
            .where(AccountTask.status == "open")
            .order_by(AccountTask.created_at)
            .limit(_LIST_LIMIT)
        )
    ).mappings().all()
    tasks = [
        {
            "id": str(r["id"]),
            "kind": r["kind"],
            "account": r["display_name"] or r["account_ref"],
            "raised": (
                _ist(r["created_at"]).strftime("%d %b") if r["created_at"] else "—"
            ),
            # detail is merchant-supplied JSON; take only the one field the
            # ladder writes and bound it, rather than rendering a blob.
            "why": str((r["detail"] or {}).get("reason") or "")[:120],
        }
        for r in task_rows
    ]

    return {
        "accounts": accounts,
        "stages": stages,
        # Stage 3 raises a human call task. It never spends customer-contact
        # budget — it is merchant-side work, and it sits here undone until
        # somebody picks up a phone.
        "open_call_tasks": open_tasks,
        "tasks": tasks,
        "window_open": window_open,
        "next_window": _ist(next_window).strftime("%a %d %b, %H:%M"),
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


# ── Folded in from the Streamlit dashboard ──────────────────────────────────
# These five sections used to live in a SECOND deployed service. Two consoles
# meant two URLs, two passwords, two cold starts on the free plan, and one of
# them silently broken for months because nobody looked at it. Everything the
# operator console showed is now reachable from the same site, behind the same
# gate, and every query below is an ORM construct for the reason at the top of
# this file.
#
# The one deliberate difference: the operator console selected customer_id.
# This one does not. The PII-free rule is the console's contract, and folding
# a second page in is not a reason to relax it — case_ref (the merchant's own
# invoice/cart reference) identifies a case perfectly well.


async def pipeline_funnel(session: AsyncSession) -> dict[str, Any]:
    """
    Where money leaves the pipeline, as two funnels rather than one.

    A single chart reads as one shrinking population, and this is not one:
    a case can spend three attempts, so "attempts" was TALLER than "payments"
    and the whole thing looked broken. Split by unit, each is monotonic.
    """
    from src.models import PaymentFailure

    failed = await session.scalar(select(func.count()).select_from(PaymentFailure))
    retryable = await session.scalar(
        select(func.count()).select_from(PaymentFailure).where(
            PaymentFailure.is_retryable.is_(True)
        )
    )
    recovered = await session.scalar(
        select(func.count()).select_from(RecoveryCase).where(
            RecoveryCase.state == "recovered"
        )
    )
    decided = await session.scalar(select(func.count()).select_from(RetryAttempt))
    passed = await session.scalar(
        select(func.count()).select_from(RetryAttempt).where(
            RetryAttempt.guardrail_passed.is_(True)
        )
    )
    executed = await session.scalar(
        select(func.count()).select_from(RetryAttempt).where(
            RetryAttempt.result.in_(["success", "failed", "pending"])
        )
    )
    return {
        "cases": [
            {"label": "Failed", "n": failed or 0},
            {"label": "Retryable", "n": retryable or 0},
            {"label": "Recovered", "n": recovered or 0},
        ],
        "attempts": [
            {"label": "Decided", "n": decided or 0},
            {"label": "Guardrail passed", "n": passed or 0},
            {"label": "Executed", "n": executed or 0},
        ],
        "has_data": bool(failed or decided),
    }


# Below this many chased cases a recovery percentage is arithmetic, not a rate.
# Same threshold and same caveat wording as the bank x rail heatmap
# (dashboard/views/bank_breakdown.py) — one thin-sample rule for the console,
# not a second one that happens to disagree.
FAILURE_CLASS_MIN_SAMPLE = 5


async def failure_causes(session: AsyncSession, *, limit: int = 12) -> list[dict[str, Any]]:
    """
    Why the gateway said no, ranked — and what each class does once chased.

    Counts alone answer "what declines most" and never "what is worth
    chasing". Bank x rail has had that second answer since routing_panel;
    failure class only ever had the first. `chased`/`recovered` close it,
    joined the same way the bank x rail heatmap joins (attempt -> failure,
    attempt -> case), so "recovered" means a case this engine actually
    touched, never a customer who retried on their own.
    """
    from src.models import PaymentFailure

    rows = (
        await session.execute(
            select(
                PaymentFailure.failure_class,
                func.count().label("n"),
                func.count().filter(PaymentFailure.is_retryable.is_(True)).label("retryable"),
            )
            .group_by(PaymentFailure.failure_class)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()

    outcome = (
        await session.execute(
            select(
                PaymentFailure.failure_class,
                func.count(func.distinct(RecoveryCase.id)).label("chased"),
                func.count(func.distinct(RecoveryCase.id))
                .filter(RecoveryCase.state == "recovered")
                .label("recovered"),
            )
            .select_from(RetryAttempt)
            .join(PaymentFailure, RetryAttempt.payment_failure_id == PaymentFailure.id)
            .join(RecoveryCase, RetryAttempt.recovery_case_id == RecoveryCase.id)
            .group_by(PaymentFailure.failure_class)
        )
    ).all()
    by_class = {r.failure_class: r for r in outcome}

    causes = []
    for r in rows:
        cls = r.failure_class or "unclassified"
        o = by_class.get(r.failure_class)
        chased = o.chased if o else 0
        recovered = o.recovered if o else 0
        causes.append({
            "cause": cls,
            "n": r.n,
            "retryable": r.retryable,
            "chased": chased,
            "recovered": recovered,
            # None, not 0 — "we have never chased one of these" and "we chased
            # them and none came back" are different facts and must not render
            # as the same 0%.
            "recovery_rate": round(100.0 * recovered / chased, 1) if chased else None,
            "thin": 0 < chased < FAILURE_CLASS_MIN_SAMPLE,
        })
    return causes


async def routing_panel(session: AsyncSession) -> dict[str, Any]:
    """
    Which bank, on which rail — the routing quality the switch_rail action
    depends on. Aggregate only: a bank name is the merchant's counterparty,
    never a customer identifier.
    """
    from src.models import PaymentFailure

    by_bank = (
        await session.execute(
            select(
                PaymentFailure.bank,
                func.count().label("failures"),
                func.count().filter(PaymentFailure.is_retryable.is_(True)).label("retryable"),
            )
            .where(PaymentFailure.bank.is_not(None))
            .group_by(PaymentFailure.bank)
            .order_by(func.count().desc())
            .limit(12)
        )
    ).all()

    by_method = (
        await session.execute(
            select(PaymentFailure.method, func.count().label("failures"))
            .group_by(PaymentFailure.method)
            .order_by(func.count().desc())
        )
    ).all()

    by_rail = (
        await session.execute(
            select(RetryAttempt.target_rail, func.count().label("n"))
            .where(RetryAttempt.target_rail.is_not(None))
            .group_by(RetryAttempt.target_rail)
            .order_by(func.count().desc())
        )
    ).all()

    return {
        "banks": [
            {"bank": r.bank, "failures": r.failures, "retryable": r.retryable}
            for r in by_bank
        ],
        "methods": [{"method": r.method or "unknown", "n": r.failures} for r in by_method],
        "rails": [{"rail": r.target_rail, "n": r.n} for r in by_rail],
        "has_data": bool(by_bank or by_method or by_rail),
    }


def _mask_email(email: str | None) -> str:
    """
    Enough of an address to recognise a colleague, not enough to harvest one.

    PRODUCT.md holds the console to "aggregate and PII-free: never a customer
    email, phone or id", and that rule is right for the money pages. A contact
    DIRECTORY is a different job — its whole purpose is "who do we reach at
    this buyer" — so the rule is kept rather than waived: the merchant sees
    the shape and the domain, which is what identifies a colleague they
    already know, while a screenshot or a shoulder-surfer gets nothing
    sendable. The full address still reaches the sender; it just never reaches
    a page.
    """
    if not email or "@" not in email:
        return "—"
    local, _, domain = email.partition("@")
    head = local[0] if local else ""
    tail = local[-1] if len(local) > 2 else ""
    return f"{head}…{tail}@{domain}"


async def accounts_panel(session: AsyncSession) -> dict[str, Any]:
    """
    The buyer directory. Which organisations owe money, and who we can reach.

    B2B collection runs on the ACCOUNT (one buyer with four overdue invoices
    is one chase, not four), and the console had no account view at all — the
    only per-account surface in the whole product was the customer-facing
    /statement/<token>. A merchant could not answer "which buyers owe us the
    most" or "do we even have a finance manager on file for this one", and the
    second question is the difference between the ladder escalating and the
    ladder running out of people to write to.
    """
    rows = (
        await session.execute(
            select(
                ArAccount.id,
                ArAccount.account_ref,
                ArAccount.display_name,
                func.count(RecoveryCase.id).label("open_cases"),
                func.coalesce(
                    func.sum(RecoveryCase.amount_at_risk - RecoveryCase.amount_recovered),
                    0,
                ).label("outstanding"),
                func.max(RecoveryCase.escalation_level).label("rung"),
            )
            .outerjoin(
                RecoveryCase,
                (RecoveryCase.account_id == ArAccount.id)
                & (RecoveryCase.state == "open"),
            )
            .group_by(ArAccount.id, ArAccount.account_ref, ArAccount.display_name)
            .order_by(func.coalesce(func.sum(
                RecoveryCase.amount_at_risk - RecoveryCase.amount_recovered), 0).desc())
            .limit(_LIST_LIMIT)
        )
    ).mappings().all()

    # Which roles each account actually has covered. A ladder that escalates to
    # a finance manager nobody has recorded escalates to nobody.
    role_rows = (
        await session.execute(
            select(ArContact.account_id, ArContact.role)
            .where(ArContact.active.is_(True))
            .distinct()
        )
    ).all()
    roles: dict[Any, list[str]] = {}
    for account_id, role in role_rows:
        roles.setdefault(account_id, []).append(str(role))

    accounts = [
        {
            "id": str(r["id"]),
            "name": r["display_name"] or r["account_ref"],
            "ref": r["account_ref"],
            "open_cases": int(r["open_cases"] or 0),
            "outstanding": _money(int(r["outstanding"] or 0)),
            "rung": int(r["rung"] or 0),
            "roles": ", ".join(
                sorted(x.replace("_", " ") for x in roles.get(r["id"], []))
            ),
            "no_contacts": not roles.get(r["id"]),
        }
        for r in rows
    ]
    return {"accounts": accounts, "has_data": bool(accounts)}


async def account_detail(
    session: AsyncSession, account_id: uuid.UUID
) -> dict[str, Any] | None:
    """One buyer: who we can reach, what they owe, and what we last sent."""
    account = await session.get(ArAccount, account_id)
    if account is None:
        return None

    contacts = [
        {
            "id": str(c.id),
            "role": c.role.replace("_", " "),
            "name": c.name or "—",
            "email": _mask_email(c.email),
            "phone": "on file" if c.phone else "—",
            "active": c.active,
        }
        for c in (await active_contacts(session, account_id))
    ]

    cases = (
        await session.execute(
            select(
                RecoveryCase.id,
                RecoveryCase.subject_ref,
                RecoveryCase.amount_at_risk,
                RecoveryCase.amount_recovered,
                RecoveryCase.due_at,
                RecoveryCase.escalation_level,
            )
            .where(
                RecoveryCase.account_id == account_id,
                RecoveryCase.state == "open",
            )
            .order_by(RecoveryCase.due_at)
            .limit(_LIST_LIMIT)
        )
    ).mappings().all()

    # What actually went out, per rung. This is the consolidation rule's own
    # evidence: one row per account per rung, not one per invoice.
    log = (
        await session.execute(
            select(
                ArContactLog.stage_level,
                ArContactLog.channels,
                ArContactLog.email_subject,
                ArContactLog.created_at,
            )
            .where(ArContactLog.account_id == account_id)
            .order_by(ArContactLog.created_at.desc())
            .limit(10)
        )
    ).mappings().all()

    return {
        "id": str(account.id),
        "name": account.display_name or account.account_ref,
        "ref": account.account_ref,
        "contacts": contacts,
        "cases": [
            {
                "case_id": str(c["id"]),
                "ref": c["subject_ref"],
                "outstanding": _money(
                    max(0, int(c["amount_at_risk"]) - int(c["amount_recovered"] or 0))
                ),
                "due": _ist(c["due_at"]).strftime("%d %b") if c["due_at"] else "—",
                "rung": int(c["escalation_level"] or 0),
            }
            for c in cases
        ],
        "log": [
            {
                "rung": int(r["stage_level"]),
                "channels": ", ".join(r["channels"] or []),
                "subject": r["email_subject"],
                "when": (
                    _ist(r["created_at"]).strftime("%d %b, %H:%M")
                    if r["created_at"] else "—"
                ),
            }
            for r in log
        ],
    }


async def message_preview(session: AsyncSession) -> dict[str, Any]:
    """
    What the customer actually receives, rendered by the real renderers.

    The engine sends SMS and email on the merchant's behalf and the merchant
    had no way to read a single one of them. `render_fallback` and
    `compose_stage_message` have always existed; nothing showed their output,
    so the exact words going out under someone else's business name were
    visible only by reading Python. An unnamed payment link reads as phishing,
    and so does a message nobody has approved.

    Rendered live through the SAME functions the sender calls, never a copy —
    a preview that could drift from what ships would be worse than none. The
    values are illustrative and labelled as such; the WORDS are exact.
    """
    from datetime import timedelta

    from src.config import get_settings
    from src.messaging.templates import _TEMPLATES, render_fallback
    from src.receivables.ladder import INVOICE_LADDER
    from src.receivables.statement import compose_stage_message

    settings = get_settings()
    merchant = settings.merchant_name or "your business"
    link = (settings.public_base_url or "https://example.in").rstrip("/") + "/recover/…"

    # One sample per failure class the payment rail can send. Sorted so the
    # page is stable between loads rather than dict-ordered.
    nudges = []
    for failure_class in sorted(_TEMPLATES):
        try:
            body = render_fallback(
                failure_class=failure_class,
                amount_display="2,499",
                next_step=f"Pay securely here: {link}",
                customer_name=None,
            )
        except Exception:  # noqa: BLE001 — a broken template must not blank the page
            logger.exception("Nudge preview failed for %s", failure_class)
            continue
        nudges.append(
            {
                "failure_class": failure_class.replace("_", " "),
                "body": body,
                # SMS is billed and truncated per 160-character segment, so the
                # length is a cost and a legibility fact, not trivia.
                "chars": len(body),
                "over": len(body) > 160,
            }
        )

    # One sample per ladder tone, through the real statement composer.
    now = datetime.now(UTC)
    sample_cases = [
        {
            "subject_ref": "INV-2041",
            "due_at": now - timedelta(days=18),
            "amount_at_risk": 4_50_000,
            "amount_recovered": 0,
            "pay_url": link,
        },
        {
            "subject_ref": "INV-2088",
            "due_at": now - timedelta(days=6),
            "amount_at_risk": 1_20_000,
            "amount_recovered": 40_000,
            "pay_url": link,
        },
    ]
    statements = []
    for stage in INVOICE_LADDER:
        try:
            composed = compose_stage_message(
                sample_cases,
                tone=stage.tone,
                merchant_name=merchant,
                statement_link=link,
                now=now,
            )
        except Exception:  # noqa: BLE001 — same reason as above
            logger.exception("Statement preview failed for tone %s", stage.tone)
            continue
        statements.append(
            {
                "level": stage.level,
                "tone": stage.tone,
                "subject": composed["subject"],
                "sms": composed["sms"],
                "chars": len(composed["sms"]),
                "over": len(composed["sms"]) > 160,
                "email_text": composed["email_text"],
            }
        )

    return {
        "merchant": merchant,
        "nudges": nudges,
        "statements": statements,
        "has_data": bool(nudges or statements),
    }


async def operations_panel(session: AsyncSession) -> dict[str, Any]:
    """
    Is the machinery running — the 2am question, not the boardroom one.

    Every figure reads a table one of the scheduler's sweeps works on, so a
    stuck pipeline shows up as a number instead of as silence.
    """
    now = datetime.now(UTC)
    stale_cut = now - timedelta(minutes=15)

    async def _count(*where: Any) -> int:
        return await session.scalar(
            select(func.count()).select_from(RetryAttempt).where(*where)
        ) or 0

    from src.models import RiskEvent, WebhookEvent

    scheduled = await _count(RetryAttempt.result == "scheduled")
    pending = await _count(RetryAttempt.result == "pending")
    stale = await _count(RetryAttempt.result == "pending", RetryAttempt.created_at < stale_cut)
    unprocessed = await session.scalar(
        select(func.count()).select_from(WebhookEvent).where(
            WebhookEvent.processed.is_(False)
        )
    ) or 0
    risk_unprocessed = await session.scalar(
        select(func.count()).select_from(RiskEvent).where(RiskEvent.processed.is_(False))
    ) or 0
    promises_overdue = await session.scalar(
        select(func.count()).select_from(PromiseToPay).where(
            PromiseToPay.status == "pending", PromiseToPay.due_at < now
        )
    ) or 0

    mix = (
        await session.execute(
            select(RetryAttempt.agent_type, RetryAttempt.action_type, func.count().label("n"))
            .group_by(RetryAttempt.agent_type, RetryAttempt.action_type)
            .order_by(func.count().desc())
            .limit(20)
        )
    ).all()

    due = (
        await session.execute(
            select(RetryAttempt.scheduled_at, RetryAttempt.action_type,
                   RetryAttempt.agent_type, RecoveryCase.subject_ref)
            .join(RecoveryCase, RecoveryCase.id == RetryAttempt.recovery_case_id, isouter=True)
            .where(RetryAttempt.result == "scheduled",
                   RetryAttempt.scheduled_at.is_not(None))
            .order_by(RetryAttempt.scheduled_at)
            .limit(12)
        )
    ).all()

    # Is the audit trail actually intact? The product offers a hash-chained,
    # tamper-evident record, and until now the only way to check that claim was
    # a CLI script nobody deployed runs. An "auditable" trail whose verification
    # lives on someone's laptop is an assertion, not evidence.
    #
    # Recomputed from the raw rows on every ops-page load. That is a full scan,
    # and it is the honest cost of the claim — the page is operator-only and
    # loaded by hand, not by a poll.
    # ponytail: full recompute per view; cache by max(case_events.id) if the
    # trail outgrows a page load.
    try:
        from src.audit_chain import AuditChainNotKeyedError, verify_chain

        verification = await verify_chain(session)
        chain = {
            "keyed": True,
            "intact": verification.intact,
            "events_checked": verification.events_checked,
            "detail": verification.detail,
            "first_broken_id": verification.first_broken_id,
        }
    except AuditChainNotKeyedError:
        # Distinct from "broken": nothing is wrong with the rows, the key is
        # simply absent. Production refuses to boot without it
        # (Settings.require_production_integrity), so this is a development
        # state, and saying "unverifiable" beats implying tampering.
        chain = {"keyed": False, "intact": False, "events_checked": 0,
                 "detail": "AUDIT_CHAIN_SECRET is not set", "first_broken_id": None}

    return {
        "chain": chain,
        "scheduled": scheduled,
        "pending": pending,
        "stale": stale,
        "events_unprocessed": unprocessed,
        "risk_unprocessed": risk_unprocessed,
        "promises_overdue": promises_overdue,
        "decision_mix": [
            {"agent": r.agent_type or "—", "action": r.action_type, "n": r.n} for r in mix
        ],
        "next_due": [
            {
                "due": _ist(_aware(r.scheduled_at)).strftime("%d %b, %H:%M"),
                "action": r.action_type,
                "agent": r.agent_type or "—",
                "case_ref": r.subject_ref or "—",
            }
            for r in due
        ],
        "has_data": bool(scheduled or pending or unprocessed or mix),
    }


async def case_list(
    session: AsyncSession, *, state: str = "all", limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    """
    Every case, filterable by state. PII-free: subject_ref, not customer_id.

    The operator console selected customer_id here. Folding this page into the
    merchant console does not get to relax that rule, so it selects the
    merchant's own reference instead — which is what identifies a case to the
    person reading it anyway.

    `offset` paginates. It was `limit=100` with no offset, which silently
    truncated: fine against a few dozen cases and a lie against a few
    thousand, where the page would show the newest hundred and give no hint
    that the rest existed.
    """
    stmt = select(
        RecoveryCase.id,
        RecoveryCase.subject_ref, RecoveryCase.risk_type, RecoveryCase.state,
        RecoveryCase.amount_at_risk, RecoveryCase.amount_recovered,
        RecoveryCase.attempts_used, RecoveryCase.max_attempts,
        RecoveryCase.escalation_level, RecoveryCase.next_action_at,
        RecoveryCase.close_reason, RecoveryCase.opened_at,
    ).order_by(RecoveryCase.opened_at.desc()).limit(limit).offset(offset)
    if state != "all":
        stmt = stmt.where(RecoveryCase.state == state)

    rows = (await session.execute(stmt)).all()
    return [
        {
            # The id is what a case-detail link needs. Not PII — an opaque
            # UUID the engine generated, unlike customer_id which is an email.
            "case_id": str(r.id),
            "case_ref": r.subject_ref,
            "risk_type": r.risk_type,
            "state": r.state,
            "at_risk": _money(r.amount_at_risk),
            "recovered": _money(r.amount_recovered) if r.amount_recovered else "—",
            "attempts": f"{r.attempts_used}/{r.max_attempts}",
            "escalation": r.escalation_level,
            "next_action": _ist(_aware(r.next_action_at)).strftime("%d %b, %H:%M")
            if r.next_action_at else "—",
            "close_reason": r.close_reason or "—",
            "opened": _ist(_aware(r.opened_at)).strftime("%d %b, %H:%M"),
        }
        for r in rows
    ]


async def case_states(session: AsyncSession) -> list[dict[str, Any]]:
    """Case count per state — the filter's own legend."""
    rows = (
        await session.execute(
            select(RecoveryCase.state, func.count().label("n"))
            .group_by(RecoveryCase.state)
            .order_by(func.count().desc())
        )
    ).all()
    return [{"state": r.state, "n": r.n} for r in rows]


async def stopping_rules(session: AsyncSession) -> dict[str, Any]:
    """
    Where the engine has deliberately stopped, and what it is holding.

    Automation refusing to act is this product's most load-bearing claim and
    the console never counted it. `cases.stop_reason()` is the authority for
    a single case; this is the same four branches in aggregate, in the same
    precedence order, so a merchant can see the shape of what the engine will
    not do without opening cases one at a time.

    Deliberately NOT a per-case loop over stop_reason(): that is one ledger
    read per case, and this page now runs against thousands. The branches are
    mirrored here in SQL and the docstring is the contract — if stop_reason()
    grows a fifth branch, this must grow one too.
    """
    from src.cases import _TERMINAL
    from src.models import RetryLedger

    now = datetime.now(UTC)
    buckets: list[dict[str, Any]] = []

    # 1. Terminal, split by how it ended. "recovered" is a stop too — the
    # case closed because the money came back — and showing it beside the
    # refusals is what makes the refusals legible as a choice rather than a
    # failure rate.
    rows = (
        await session.execute(
            select(
                RecoveryCase.state,
                func.count(RecoveryCase.id),
                func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0),
            )
            .where(RecoveryCase.state.in_(tuple(_TERMINAL)))
            .group_by(RecoveryCase.state)
        )
    ).all()
    why_closed = {
        "recovered": "the money came back — nothing left to chase",
        "exhausted": "attempt budget spent",
        "abandoned": "the guardrail refused, or the class is not retryable",
        "expired": "the consent window closed",
        "opted_out": "the customer asked us to stop",
    }
    for state, n, paise in rows:
        buckets.append({
            "kind": state, "label": state.replace("_", " "),
            "why": why_closed.get(state, "closed"),
            "cases": int(n), "amount": _money(int(paise)),
            "acting": False,
        })

    # 2. Open but held: budget spent, or waiting on next_action_at. Both are
    # "we may not act right now" rather than "we are done", so they are
    # counted apart from the terminal set above.
    held_budget = (
        await session.execute(
            select(
                func.count(RecoveryCase.id),
                func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0),
            ).where(
                RecoveryCase.state == "open",
                RecoveryCase.attempts_used >= RecoveryCase.max_attempts,
            )
        )
    ).one()
    if held_budget[0]:
        buckets.append({
            "kind": "budget", "label": "budget spent",
            "why": "open, but every permitted attempt has been used",
            "cases": int(held_budget[0]), "amount": _money(int(held_budget[1])),
            "acting": False,
        })

    waiting = (
        await session.execute(
            select(
                func.count(RecoveryCase.id),
                func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0),
            ).where(
                RecoveryCase.state == "open",
                RecoveryCase.attempts_used < RecoveryCase.max_attempts,
                RecoveryCase.next_action_at.is_not(None),
                RecoveryCase.next_action_at > now,
            )
        )
    ).one()
    if waiting[0]:
        buckets.append({
            "kind": "waiting", "label": "waiting",
            "why": "backoff or a promise-to-pay is holding the next touch",
            "cases": int(waiting[0]), "amount": _money(int(waiting[1])),
            "acting": False,
        })

    # 3. Consent withdrawn at the LEDGER, which outlives any one case: the
    # customer said stop, so every case of theirs is off limits whatever its
    # own state says.
    opted_out_ledgers = int(
        await session.scalar(
            select(func.count(RetryLedger.id)).where(
                RetryLedger.consent_status == "opted_out"
            )
        )
        or 0
    )

    # 4. What is left is what the engine may actually touch.
    actionable = (
        await session.execute(
            select(
                func.count(RecoveryCase.id),
                func.coalesce(
                    func.sum(
                        RecoveryCase.amount_at_risk - RecoveryCase.amount_recovered
                    ),
                    0,
                ),
            ).where(
                RecoveryCase.state == "open",
                RecoveryCase.attempts_used < RecoveryCase.max_attempts,
                sa_or(
                    RecoveryCase.next_action_at.is_(None),
                    RecoveryCase.next_action_at <= now,
                ),
            )
        )
    ).one()

    buckets.sort(key=lambda b: b["cases"], reverse=True)
    return {
        "buckets": buckets,
        "stopped_cases": sum(b["cases"] for b in buckets),
        "opted_out_customers": opted_out_ledgers,
        "actionable_cases": int(actionable[0]),
        "actionable": _money(max(0, int(actionable[1]))),
        "has_data": bool(buckets or actionable[0]),
    }


async def case_detail(session: AsyncSession, case_id: str) -> dict[str, Any] | None:
    """
    One case, as the whole decision chain: signal → classification → agent
    recommendation → guardrail → execution → stopping rule → outcome.

    Every link already existed in the schema and none of it was readable.
    RetryAttempt carries `agent_reasoning`, `agent_confidence`,
    `guardrail_passed` and `guardrail_rejection_reason`; `case_events` is the
    ordered trail; `stop_reason()` says why nothing more will happen. This
    assembles them and invents nothing.

    PII-free like every other console read: keyed on the case's own UUID,
    displaying the merchant's `subject_ref`. `customer_id` is an email and
    never leaves this function. B2B account display names are merchant data
    and are allowed (src/receivables/models.py).
    """
    import uuid as _uuid

    from src.cases import stop_reason
    from src.models import CaseEvent, PaymentFailure, RetryLedger
    from src.receivables.models import ArAccount, CaseDispute

    try:
        cid = _uuid.UUID(case_id)
    except (ValueError, AttributeError):
        return None
    case = await session.get(RecoveryCase, cid)
    if case is None:
        return None

    failure = None
    if case.risk_type == "payment_failure":
        failure = await session.scalar(
            select(PaymentFailure).where(PaymentFailure.payment_id == case.subject_ref)
        )

    attempts = list((await session.execute(
        select(RetryAttempt)
        .where(RetryAttempt.recovery_case_id == case.id)
        .order_by(RetryAttempt.attempt_number, RetryAttempt.created_at)
    )).scalars().all())

    events = list((await session.execute(
        select(CaseEvent)
        .where(CaseEvent.recovery_case_id == case.id)
        .order_by(CaseEvent.created_at, CaseEvent.id)
    )).scalars().all())

    # The ledger is what stop_reason() consults for consent, so read it
    # rather than guessing — an opted-out customer outranks every other
    # reason and would otherwise be reported as merely "waiting".
    ledger = None
    if case.customer_id:
        ledger = await session.scalar(
            select(RetryLedger).where(RetryLedger.customer_id == case.customer_id)
        )

    account = None
    siblings: list[dict[str, Any]] = []
    if case.account_id is not None:
        account = await session.get(ArAccount, case.account_id)
        sib_rows = (await session.execute(
            select(RecoveryCase)
            .where(
                RecoveryCase.account_id == case.account_id,
                RecoveryCase.id != case.id,
            )
            .order_by(RecoveryCase.due_at, RecoveryCase.opened_at)
            .limit(12)
        )).scalars().all()
        siblings = [
            {
                "case_id": str(c.id), "ref": c.subject_ref, "state": c.state,
                "amount": _money(max(0, c.amount_at_risk - c.amount_recovered)),
            }
            for c in sib_rows
        ]

    disputed = bool(await session.scalar(
        select(func.count()).select_from(CaseDispute).where(
            CaseDispute.case_id == case.id, CaseDispute.status == "open"
        )
    ))

    return {
        "case_id": str(case.id),
        "ref": case.subject_ref,
        "risk_type": case.risk_type.replace("_", " "),
        "state": case.state,
        "at_risk": _money(case.amount_at_risk),
        "recovered": _money(case.amount_recovered) if case.amount_recovered else None,
        "outstanding": _money(max(0, case.amount_at_risk - case.amount_recovered)),
        # The distinction the headline depends on: money we earned versus
        # money that arrived anyway. Never collapse them.
        "attributed": case.recovered_via_attempt_id is not None,
        "recovered_ref": case.recovered_ref,
        "opened": _ist(_aware(case.opened_at)).strftime("%d %b %Y, %H:%M"),
        "attempts_used": case.attempts_used,
        "max_attempts": case.max_attempts,
        "close_reason": case.close_reason,
        "disputed": disputed,
        # None means "the engine may act right now" — the honest opposite of
        # a stopping rule, and worth saying rather than leaving blank.
        "stop_reason": stop_reason(case, ledger),
        "diagnosis": {
            "failure_class": failure.failure_class if failure else None,
            "retryable": failure.is_retryable if failure else None,
            "error_reason": failure.error_reason if failure else None,
            "error_code": failure.error_code if failure else None,
            "error_source": failure.error_source if failure else None,
            "error_step": failure.error_step if failure else None,
            "method": failure.method if failure else None,
            "bank": (failure.bank or failure.card_issuer) if failure else None,
        } if failure else None,
        "attempts": [
            {
                "n": a.attempt_number,
                "action": a.action_type,
                "rail": a.target_rail,
                "agent": a.agent_type,
                "reason": a.agent_reasoning,
                "confidence": (
                    round(a.agent_confidence * 100) if a.agent_confidence else None
                ),
                "guardrail_passed": a.guardrail_passed,
                "rejection": a.guardrail_rejection_reason,
                "result": a.result,
                "executed": (
                    _ist(_aware(a.executed_at)).strftime("%d %b, %H:%M")
                    if a.executed_at else None
                ),
            }
            for a in attempts
        ],
        "events": [
            {
                "type": e.event_type.replace("_", " "),
                "actor": e.actor,
                "at": _ist(_aware(e.created_at)).strftime("%d %b, %H:%M:%S"),
                "detail": e.detail or {},
                # A stamped hash means this row is inside the verified chain
                # (scripts/audit_chain.py --verify). Shown because "auditable"
                # should be checkable, not asserted.
                "sealed": bool(e.event_hash),
            }
            for e in events
        ],
        "account": {
            "name": account.display_name or account.account_ref,
            "siblings": siblings,
        } if account else None,
    }
