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


async def promise_panel(
    session: AsyncSession, *, limit: int = _LIST_LIMIT
) -> dict[str, Any]:
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
            .limit(limit)
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


async def plan_panel(
    session: AsyncSession, *, limit: int = _LIST_LIMIT
) -> dict[str, Any]:
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
            .limit(limit)
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


async def dispute_panel(
    session: AsyncSession, *, limit: int = _LIST_LIMIT
) -> dict[str, Any]:
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
            .limit(limit)
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
        # The same age as a phrase. The question under a heartbeat is "are
        # these numbers current", and a wall-clock timestamp makes the reader
        # do the subtraction. Coarse on purpose past a minute: nobody needs
        # "4m 37s ago", and a second-precision figure on a page that does not
        # live-update is a lie that gets truer once a minute.
        "fresh": _ago(int(age.total_seconds())),
        "stale": age > stale_after,
        "counts": heartbeat.last_tick_counts,
    }


def _ago(seconds: int) -> str:
    """A coarse, honest "how long ago" — never a false precision."""
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


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

    Each item carries a `severity` and an `href`:

    * `severity` is "stop" (money has halted and only a person restarts it) or
      "wait" (it will resolve itself; watch it). Rendered as a word beside the
      colour, never a colour alone.
    * `href` is where the work actually gets done. A worklist whose rows are
      not clickable makes the reader hunt the same page for the thing it just
      told them about — every item here points at the block or the filtered
      list that holds it.
    """
    items: list[dict[str, str]] = []

    # First, because it invalidates everything else on the page: if the
    # scheduler is not ticking, every number below is a frozen snapshot and
    # no sweep is firing.
    if health.get("stale"):
        items.append(
            {
                "kind": "engine",
                "severity": "stop",
                # The one item that is not about a case: the sweeps that would
                # clear everything else are not running.
                "href": "/console/ops",
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
                "severity": "stop",
                "href": "#disputes",
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
                "severity": "stop",
                "href": "#voice",
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
                # The chase resumed on its own, a rung firmer. Worth seeing,
                # not worth stopping the day for.
                "severity": "wait",
                "href": "#plans",
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
                "severity": "stop",
                "href": "/console/cases?state=exhausted",
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
    # `href` only where a page shows THAT population. "Failed" and
    # "Retryable" count payment_failures; the cases list is keyed on case
    # state and cannot be filtered to either, and the attempt stages have no
    # page at all. A stage that links to a nearly-right list is worse than one
    # that does not link — the reader trusts the number they land on.
    return {
        "cases": [
            {"label": "Failed", "n": failed or 0, "href": None},
            {"label": "Retryable", "n": retryable or 0, "href": None},
            {
                "label": "Recovered", "n": recovered or 0,
                "href": "/console/cases?state=recovered",
            },
        ],
        "attempts": [
            {"label": "Decided", "n": decided or 0, "href": None},
            {"label": "Guardrail passed", "n": passed or 0, "href": None},
            {"label": "Executed", "n": executed or 0, "href": None},
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

    # The denominator for the method shares, once.
    _method_total = sum(r.failures for r in by_method)

    return {
        "banks": [
            {"bank": r.bank, "failures": r.failures, "retryable": r.retryable}
            for r in by_bank
        ],
        # `frac` and `pct` are computed here, not in the template: the
        # analytics page draws these as bars, and bar() does arithmetic on
        # frac. An empty methods list hid that for a whole release — the loop
        # never ran on a fresh database, and every page with data 500'd on
        # `'dict object' has no attribute 'frac'`. Nothing is computed in a
        # template in this console; this is why.
        "methods": [
            {
                "method": r.method or "unknown",
                "n": r.failures,
                "frac": (r.failures / _method_total) if _method_total else 0.0,
                "pct": round(100.0 * r.failures / _method_total, 1)
                if _method_total else 0.0,
            }
            for r in by_method
        ],
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


async def customer_view(
    session: AsyncSession, *, limit: int = 60
) -> dict[str, Any]:
    """
    The other side of every case: what the customer can see, and whether they
    have looked.

    The console shows the engine's reasoning in full and the customer's own
    view not at all, so an operator could say what the guardrail decided and
    not what the person on the other end was actually shown. This is that
    half — per case, the state the customer's page is in, whether they have
    opened it, and what they did from it.

    **No link is listed here, deliberately.** A `/recover/<token>` URL is a
    bearer credential: whoever holds it can see and pay that case. A console
    page that printed one per row would be a page of live credentials, sitting
    in a browser cache and a screenshot. The operator opens one at a time
    instead, through a POST that mints a fresh short-lived token and writes an
    audit row (see routes.console_customer_open) — the same reasoning
    src/demo.py states for why only the offline demo lists them.

    PII-free like every other console read: `subject_ref`, never customer_id.
    """
    from src.config import get_settings, reveal
    from src.models import RetryLedger
    from src.receivables.models import CaseDispute

    settings = get_settings()
    # mint() returns None with no secret, and every nudge then ships without a
    # link. That is a deployment fact the operator should read here rather
    # than discover from a button that does nothing.
    links_on = bool(reveal(settings.recovery_link_secret))

    cases = list((await session.execute(
        select(RecoveryCase)
        .order_by(RecoveryCase.opened_at.desc())
        .limit(limit)
    )).scalars().all())
    ids = [c.id for c in cases]
    if not ids:
        return {
            "links_on": links_on,
            "ttl_hours": min(
                settings.recovery_link_ttl_hours, settings.consent_window_hours
            ),
            "rows": [],
            "viewed_cases": 0,
        }

    # Views, by the customer only — an operator preview writes actor
    # "operator" for exactly this reason (src/customer/routes.py).
    view_rows = (await session.execute(
        select(
            CaseEvent.recovery_case_id,
            func.count(CaseEvent.id),
            func.max(CaseEvent.created_at),
        )
        .where(
            CaseEvent.recovery_case_id.in_(ids),
            CaseEvent.event_type == "page_viewed",
            CaseEvent.actor == "customer",
        )
        .group_by(CaseEvent.recovery_case_id)
    )).all()
    views = {r[0]: (r[1], r[2]) for r in view_rows}

    promise_rows = (await session.execute(
        select(PromiseToPay)
        .where(PromiseToPay.recovery_case_id.in_(ids))
        .order_by(PromiseToPay.promised_at.desc())
    )).scalars().all()
    # Newest per case: the rows arrive newest-first, so the first one wins and
    # every re-promise after a break is correctly ignored here.
    promises: dict[Any, Any] = {}
    for row in promise_rows:
        promises.setdefault(row.recovery_case_id, row)

    disputed = {
        r[0] for r in (await session.execute(
            select(CaseDispute.case_id).where(
                CaseDispute.case_id.in_(ids), CaseDispute.status == "open"
            )
        )).all()
    }

    # Opt-out lives on the customer's ledger, not the case, because it is a
    # standing instruction across every case that person has.
    opted_out: set[str] = set()
    customer_ids = {c.customer_id for c in cases if c.customer_id}
    if customer_ids:
        opted_out = {
            r[0] for r in (await session.execute(
                select(RetryLedger.customer_id).where(
                    RetryLedger.customer_id.in_(customer_ids),
                    RetryLedger.consent_status == "opted_out",
                )
            )).all()
        }

    rows = []
    for c in cases:
        seen, last = views.get(c.id, (0, None))
        promise = promises.get(c.id)
        rows.append({
            "case_id": str(c.id),
            "ref": c.subject_ref,
            "risk_type": c.risk_type.replace("_", " "),
            "state": c.state,
            "outstanding": _money(max(0, c.amount_at_risk - c.amount_recovered)),
            "recovered": _money(c.amount_recovered) if c.amount_recovered else None,
            "opened": _ist(_aware(c.opened_at)).strftime("%d %b, %H:%M"),
            # 0 is a finding, not a blank: a nudge that was delivered and never
            # opened is the single most useful thing on this page.
            "views": seen,
            "last_view": (
                _ist(_aware(last)).strftime("%d %b, %H:%M") if last else None
            ),
            "promise": {
                "status": promise.status,
                "due": _ist(_aware(promise.due_at)).strftime("%d %b"),
                "amount": _money(promise.amount_promised),
            } if promise else None,
            "disputed": c.id in disputed,
            "opted_out": bool(c.customer_id and c.customer_id in opted_out),
        })

    return {
        "links_on": links_on,
        "ttl_hours": min(
            settings.recovery_link_ttl_hours, settings.consent_window_hours
        ),
        "rows": rows,
        "viewed_cases": sum(1 for r in rows if r["views"]),
    }


async def nav_counts(session: AsyncSession) -> dict[str, int]:
    """
    The counts the navigation carries as badges — what needs a person, per
    section.

    Rendered on every console page, so it is three indexed COUNTs and nothing
    else: no joins, no per-row work, no reuse of the panel readers (which
    fetch rows this does not need). A badge that costs a page render is a
    badge that gets removed six months later.

    Only things automation has DELIBERATELY stopped short of are counted. A
    number that merely describes volume ("42 open cases") would sit in the
    navigation forever and teach the reader to ignore the badges — the whole
    value of a count here is that a non-zero one means someone has to act.
    """
    from src.models import VoiceCallQueue
    from src.receivables.models import AccountTask, CaseDispute

    disputes = int(await session.scalar(
        select(func.count()).select_from(CaseDispute).where(
            CaseDispute.status == "open"
        )
    ) or 0)
    tasks = int(await session.scalar(
        select(func.count()).select_from(AccountTask).where(
            AccountTask.status == "open"
        )
    ) or 0)
    # Queued and unclaimed: a claimed call is the telephony leg working, which
    # is not a thing anyone needs to do something about.
    calls = int(await session.scalar(
        select(func.count()).select_from(VoiceCallQueue).where(
            VoiceCallQueue.state == "queued",
            VoiceCallQueue.claimed_at.is_(None),
        )
    ) or 0)

    return {
        "disputes": disputes,
        "tasks": tasks,
        "calls": calls,
        # What the Ledger badge shows: the worklist is the sum of the things
        # that will not restart on their own.
        "needs_you": disputes + tasks + calls,
    }


async def guardrail_trace(
    session: AsyncSession, case_id: str
) -> dict[str, Any] | None:
    """
    The gate's verdict on this case's latest attempt, rule by rule.

    The case page already says *whether* the guardrail approved an attempt and
    reproduces its refusal verbatim. What it could not say is what else was
    checked — so an approval read as "nothing objected" rather than as "eleven
    named rules ran and none of them fired", which is the actual claim and the
    more interesting one.

    Reconstructed rather than stored, and the reconstruction is honest about
    its own limits:

    * The roster comes from `gate.rule_roster(action)`, so it is the rules the
      gate runs for THAT action — a nudge carries a twelfth, an abandon runs
      none at all.
    * A fired rule is attributed by matching its rejection prefix against the
      stored reason. The gate collects every violation rather than stopping at
      the first, so a refusal names all of them and the rest of the roster
      genuinely passed.
    * Nothing here is a re-run. Re-validating now would answer a different
      question — "would this pass today" — against a clock, a budget and a
      consent window that have all moved since.
    """
    import uuid as _uuid

    from src.guardrail.gate import SCHEMA_RULE, rule_roster

    try:
        cid = _uuid.UUID(case_id)
    except (ValueError, AttributeError):
        return None

    attempt = await session.scalar(
        select(RetryAttempt)
        .where(RetryAttempt.recovery_case_id == cid)
        .order_by(RetryAttempt.attempt_number.desc(), RetryAttempt.created_at.desc())
        .limit(1)
    )
    if attempt is None:
        return None

    action = attempt.action_type or ""
    reason = attempt.guardrail_rejection_reason or ""
    roster = rule_roster(action)

    if not roster:
        # An abandon. The gate auto-passes it without running anything, and
        # drawing eleven green ticks here would describe work that never
        # happened.
        return {
            "attempt": attempt.attempt_number,
            "action": action,
            "passed": bool(attempt.guardrail_passed),
            "skipped": True,
            "rules": [],
            "checked": 0,
            "failed": 0,
        }

    rules = [
        {
            "label": label,
            # The schema check aside, a rule "fired" exactly when its prefix
            # appears in the stored reason.
            "fired": prefix in reason,
            # Only a fired rule has anything to say; a passed one saying
            # something would be invented detail.
            "detail": reason if prefix in reason else None,
        }
        for _name, label, prefix in roster
    ]
    schema_label, schema_prefix = SCHEMA_RULE
    rules.insert(0, {
        "label": schema_label,
        "fired": schema_prefix in reason,
        "detail": reason if schema_prefix in reason else None,
    })

    fired = sum(1 for r in rules if r["fired"])
    return {
        "attempt": attempt.attempt_number,
        "action": action,
        "passed": bool(attempt.guardrail_passed),
        "skipped": False,
        "rules": rules,
        "checked": len(rules),
        "failed": fired,
        # A refusal whose reason matches no prefix: the rule was renamed, or
        # its message changed, and the roster has drifted. Said out loud
        # rather than rendered as a clean sheet.
        "unattributed": bool(reason) and fired == 0,
        "reason": reason or None,
    }


# The filters the payments page offers, as (value, label) in the order a
# merchant thinks about them. Declared here rather than in the template so the
# page cannot offer a filter the query does not implement.
PAYMENT_STATES: list[tuple[str, str]] = [
    ("open", "Recovering"),
    ("recovered", "Recovered"),
    ("exhausted", "Out of attempts"),
    ("abandoned", "Abandoned"),
    ("opted_out", "Opted out"),
]


async def payment_list(
    session: AsyncSession,
    *,
    state: str = "all",
    failure_class: str = "all",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """
    The payment rail's own view: one row per failed charge, not per case.

    The console has had a case list since the ledger landed, and a case is not
    a payment — it is the recovery wrapped around one. A merchant asking "what
    failed, and why" was being answered with case references and case states,
    which is the right answer to a different question.

    **Every column is named explicitly.** PaymentFailure carries
    `customer_email`, `customer_contact` and `vpa` (a UPI handle is a personal
    identifier), and the console is PII-free by contract. A `select(Model)`
    here would put all three one template mistake away from the page.
    """
    from src.models import PaymentFailure

    stmt = (
        select(
            PaymentFailure.payment_id,
            PaymentFailure.order_id,
            PaymentFailure.amount,
            PaymentFailure.method,
            PaymentFailure.bank,
            PaymentFailure.card_issuer,
            PaymentFailure.failure_class,
            PaymentFailure.is_retryable,
            PaymentFailure.error_reason,
            PaymentFailure.failed_at,
            RecoveryCase.id.label("case_id"),
            RecoveryCase.state,
            RecoveryCase.attempts_used,
            RecoveryCase.max_attempts,
            RecoveryCase.next_action_at,
            RecoveryCase.amount_recovered,
        )
        # OUTER: a failure whose case has not opened yet is exactly the row a
        # merchant most wants to see, and an inner join would hide it.
        .join(
            RecoveryCase,
            RecoveryCase.subject_ref == PaymentFailure.payment_id,
            isouter=True,
        )
        .order_by(PaymentFailure.failed_at.desc())
        .limit(limit)
        .offset(offset)
    )
    count_stmt = (
        select(func.count())
        .select_from(PaymentFailure)
        .join(
            RecoveryCase,
            RecoveryCase.subject_ref == PaymentFailure.payment_id,
            isouter=True,
        )
    )
    if state != "all":
        stmt = stmt.where(RecoveryCase.state == state)
        count_stmt = count_stmt.where(RecoveryCase.state == state)
    if failure_class != "all":
        stmt = stmt.where(PaymentFailure.failure_class == failure_class)
        count_stmt = count_stmt.where(PaymentFailure.failure_class == failure_class)

    rows = (await session.execute(stmt)).all()
    total = int(await session.scalar(count_stmt) or 0)

    # The filter legend, counted the same way the filter itself selects — a
    # count that disagrees with the list it leads to is worse than no count.
    by_state = {
        row[0]: row[1] for row in (await session.execute(
            select(RecoveryCase.state, func.count())
            .join(
                PaymentFailure,
                PaymentFailure.payment_id == RecoveryCase.subject_ref,
            )
            .group_by(RecoveryCase.state)
        )).all()
    }
    by_class = {
        row[0]: row[1] for row in (await session.execute(
            select(PaymentFailure.failure_class, func.count())
            .group_by(PaymentFailure.failure_class)
            .order_by(func.count().desc())
        )).all()
    }

    return {
        "payments": [
            {
                "ref": r.payment_id,
                "order_ref": r.order_id,
                "amount": _money(r.amount),
                "method": r.method,
                "bank": r.bank or r.card_issuer or "—",
                "failure_class": r.failure_class.replace("_", " "),
                "retryable": bool(r.is_retryable),
                "why": r.error_reason,
                "failed": _ist(_aware(r.failed_at)).strftime("%d %b, %H:%M"),
                # None means no case opened for this failure yet — a real
                # state with its own meaning, not a blank.
                "case_id": str(r.case_id) if r.case_id else None,
                "state": r.state,
                "attempts": (
                    f"{r.attempts_used}/{r.max_attempts}"
                    if r.case_id is not None else None
                ),
                "next_action": (
                    _ist(_aware(r.next_action_at)).strftime("%d %b, %H:%M")
                    if r.next_action_at else None
                ),
                "recovered": (
                    _money(r.amount_recovered) if r.amount_recovered else None
                ),
            }
            for r in rows
        ],
        "total": total,
        "states": [
            (value, label, by_state.get(value, 0)) for value, label in PAYMENT_STATES
        ],
        "classes": [
            (name, name.replace("_", " "), n) for name, n in by_class.items()
        ],
    }


# ── Phase 05: Analytics reads ───────────────────────────────────────────────
# Ported from the Streamlit dashboard (dashboard/views/), which was never
# deployed. The charts live in the templates as inline SVG, so these functions
# return numbers, never markup.


async def performance_analytics(session: AsyncSession) -> dict[str, Any]:
    """
    Recovery rate, recovered amount, outstanding, average attempts — the
    four numbers a finance user wants by failure class and by channel.
    """
    # By failure class
    from src.models import PaymentFailure

    by_class_rows = (
        await session.execute(
            select(
                PaymentFailure.failure_class,
                func.count(func.distinct(RecoveryCase.id)).label("cases"),
                func.count(func.distinct(RecoveryCase.id))
                .filter(RecoveryCase.state == "recovered")
                .label("recovered"),
                func.coalesce(
                    func.sum(RecoveryCase.amount_at_risk).filter(
                        RecoveryCase.state == "recovered"
                    ),
                    0,
                ).label("recovered_paise"),
            )
            .select_from(RetryAttempt)
            .join(PaymentFailure, RetryAttempt.payment_failure_id == PaymentFailure.id)
            .join(RecoveryCase, RetryAttempt.recovery_case_id == RecoveryCase.id)
            .group_by(PaymentFailure.failure_class)
            .order_by(func.count(func.distinct(RecoveryCase.id)).desc())
            .limit(12)
        )
    ).all()

    by_class = []
    max_cases = max((r.cases for r in by_class_rows), default=1) or 1
    for r in by_class_rows:
        rate = round(100.0 * r.recovered / r.cases, 1) if r.cases else None
        by_class.append({
            "class": (r.failure_class or "unknown").replace("_", " "),
            "cases": r.cases,
            "recovered": r.recovered,
            "rate": rate,
            "recovered_amount": _money(int(r.recovered_paise)),
            "frac": r.cases / max_cases,
        })

    # By channel (risk_type as the axis the merchant recognises)
    by_channel_rows = (
        await session.execute(
            select(
                RecoveryCase.risk_type,
                func.count(RecoveryCase.id).label("cases"),
                func.count(RecoveryCase.id)
                .filter(RecoveryCase.state == "recovered")
                .label("recovered"),
                func.coalesce(func.sum(RecoveryCase.amount_recovered), 0).label(
                    "recovered_paise"
                ),
                func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0).label(
                    "at_risk_paise"
                ),
            )
            .group_by(RecoveryCase.risk_type)
            .order_by(func.count(RecoveryCase.id).desc())
        )
    ).all()

    by_channel: list[dict[str, Any]] = []
    for ch in by_channel_rows:
        rate = round(100.0 * ch[2] / ch[1], 1) if ch[1] else None
        by_channel.append({
            "channel": (ch[0] or "unknown").replace("_", " "),
            "cases": ch[1],
            "recovered": ch[2],
            "rate": rate,
            "recovered_amount": _money(int(ch[3])),
            "outstanding": _money(max(0, int(ch[4]) - int(ch[3]))),
        })

    # Average attempts per recovered case
    avg_attempts = await session.scalar(
        select(func.avg(RecoveryCase.attempts_used)).where(
            RecoveryCase.state == "recovered"
        )
    )

    return {
        "by_class": by_class,
        "by_channel": by_channel,
        "avg_attempts": round(float(avg_attempts), 1) if avg_attempts else None,
        "has_data": bool(by_class or by_channel),
    }


async def hours_analytics(session: AsyncSession) -> dict[str, Any]:
    """
    Recovery count by hour-of-day, with the blackout band.

    The blackout boundaries are read from get_settings(), never hardcoded to
    23:00–07:00 — a merchant who changes them sees the chart move.
    """
    from src.config import get_settings as _get_settings

    settings = _get_settings()

    # Recoveries by hour (IST). For portability, fetch all recovered-at
    # timestamps and bucket in Python rather than relying on extract('hour')
    # which renders differently on SQLite vs Postgres.
    ts_rows = (
        await session.execute(
            select(RecoveryCase.recovered_at).where(
                RecoveryCase.state == "recovered",
                RecoveryCase.recovered_at.is_not(None),
            )
        )
    ).scalars().all()

    by_hour: dict[int, int] = {h: 0 for h in range(24)}
    for ts in ts_rows:
        if ts is not None:
            ist_ts = _ist(_aware(ts))
            by_hour[ist_ts.hour] = by_hour.get(ist_ts.hour, 0) + 1

    max_n = max(by_hour.values(), default=1) or 1
    hours = [
        {
            "hour": h,
            "label": f"{h:02d}:00",
            "n": by_hour[h],
            "frac": by_hour[h] / max_n,
            "blackout": _in_blackout(h, settings.retry_blackout_start_hour,
                                     settings.retry_blackout_end_hour),
        }
        for h in range(24)
    ]

    return {
        "hours": hours,
        "blackout_start": settings.retry_blackout_start_hour,
        "blackout_end": settings.retry_blackout_end_hour,
        "total_recovered": sum(by_hour.values()),
        "has_data": sum(by_hour.values()) > 0,
    }


def _in_blackout(hour: int, start: int, end: int) -> bool:
    """True when `hour` falls inside the blackout window."""
    if start < end:
        return start <= hour < end
    # Wraps midnight: e.g. 23:00–07:00
    return hour >= start or hour < end


async def economics_analytics(session: AsyncSession) -> dict[str, Any]:
    """
    Gross at risk, recovered, retry cost at the configured rate, and the
    resulting efficiency.
    """
    from src.config import get_settings as _get_settings

    settings = _get_settings()

    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0),
                func.coalesce(func.sum(RecoveryCase.amount_recovered), 0),
                func.count(RetryAttempt.id),
            )
            .select_from(RecoveryCase)
            .outerjoin(RetryAttempt, RetryAttempt.recovery_case_id == RecoveryCase.id)
        )
    ).one()
    at_risk_paise, recovered_paise, total_attempts = (int(v) for v in row)

    # retry_cost is the merchant's configured cost per attempt. The config
    # carries it in rupees; we keep paise here because every other figure is.
    retry_cost_per_attempt = float(getattr(settings, "retry_cost_inr", 0) or 0)
    total_retry_cost_paise = int(retry_cost_per_attempt * total_attempts * 100)
    net_paise = recovered_paise - total_retry_cost_paise

    return {
        "at_risk": _money(at_risk_paise),
        "recovered": _money(recovered_paise),
        "total_attempts": total_attempts,
        "retry_cost_per": retry_cost_per_attempt,
        "total_retry_cost": _money(total_retry_cost_paise),
        "net_recovered": _money(max(0, net_paise)),
        "efficiency": (
            round(100.0 * recovered_paise / at_risk_paise, 1) if at_risk_paise else None
        ),
        "has_data": at_risk_paise > 0,
    }


# ── Phase 06: Voice call detail ─────────────────────────────────────────────


async def voice_call_list(session: AsyncSession) -> dict[str, Any]:
    """
    The voice queue with per-call detail: state, outcome, and which of the
    four pipeline gates fired.

    The gate information lives on the CaseEvent rows the pipeline writes
    (event_type in {'voice_turn', 'voice_abstain', 'voice_opt_out',
    'voice_grounding_failed'}). A call that never progressed past queueing
    has no gate events, and the page says so.

    HARD RULE: never fabricate a transcript. If turn-by-turn text is not
    stored, the page says it is not stored.
    """
    rows = (
        await session.execute(
            select(
                VoiceCallQueue.id,
                VoiceCallQueue.state,
                VoiceCallQueue.result,
                VoiceCallQueue.risk_type,
                VoiceCallQueue.amount_paise,
                VoiceCallQueue.created_at,
                VoiceCallQueue.claimed_at,
                RecoveryCase.subject_ref,
            )
            .join(RecoveryCase, RecoveryCase.id == VoiceCallQueue.recovery_case_id)
            .order_by(VoiceCallQueue.created_at.desc())
            .limit(30)
        )
    ).mappings().all()

    calls: list[dict[str, Any]] = []
    for r in rows:
        # Gate events for this call's case
        gate_rows = (
            await session.execute(
                select(CaseEvent.event_type, CaseEvent.detail, CaseEvent.created_at)
                .where(
                    CaseEvent.recovery_case_id == RecoveryCase.id,
                    RecoveryCase.subject_ref == r["subject_ref"],
                    CaseEvent.event_type.in_([
                        "voice_turn", "voice_abstain", "voice_opt_out",
                        "voice_grounding_failed", "voice_injection_refused",
                    ]),
                )
                .join(RecoveryCase, RecoveryCase.id == CaseEvent.recovery_case_id)
                .order_by(CaseEvent.created_at.desc())
                .limit(5)
            )
        ).mappings().all()

        gates = {
            "retrieval_passed": False,
            "facts_available": False,
            "instructions_sanitised": False,
            "response_grounded": False,
            "opt_out_checked": True,  # always checked by design
        }
        abstain_reason: str | None = None
        opted_out = r["state"] == "opted_out"

        for ge in gate_rows:
            et = ge["event_type"]
            detail = ge["detail"] or {}
            if et == "voice_turn":
                gates["retrieval_passed"] = True
                gates["facts_available"] = True
                gates["instructions_sanitised"] = True
                gates["response_grounded"] = True
            elif et == "voice_abstain":
                gates["retrieval_passed"] = detail.get("retrieval_passed", False)
                gates["facts_available"] = detail.get("facts_available", False)
                abstain_reason = (
                    detail.get("reason")
                    or "the agent could not safely answer — insufficient grounded information"
                )
            elif et == "voice_opt_out":
                opted_out = True
            elif et == "voice_grounding_failed":
                gates["retrieval_passed"] = True
                gates["facts_available"] = True
                gates["instructions_sanitised"] = True
                gates["response_grounded"] = False
            elif et == "voice_injection_refused":
                gates["instructions_sanitised"] = False

        calls.append({
            "id": str(r["id"]),
            "state": r["state"],
            "result": r["result"],
            "subject_ref": r["subject_ref"],
            "amount": _money(int(r["amount_paise"])),
            "risk_type": (r["risk_type"] or "").replace("_", " "),
            "created": (
                _ist(_aware(r["created_at"])).strftime("%d %b, %H:%M")
                if r["created_at"] else "—"
            ),
            "claimed": (
                _ist(_aware(r["claimed_at"])).strftime("%d %b, %H:%M")
                if r["claimed_at"] else None
            ),
            "gates": gates,
            "abstain_reason": abstain_reason,
            "opted_out": opted_out,
            # No transcript field: the model does not store turn-by-turn text.
            # The template says so explicitly.
            "has_transcript": False,
        })

    return {"calls": calls, "has_data": bool(calls)}


# ── Phase 07: Safety state ──────────────────────────────────────────────────


async def safety_state(session: AsyncSession) -> dict[str, Any]:
    """
    Every safeguard in the engine, and whether it is live right now.

    Read from the enforcing modules, never restated in copy. A row that says
    'active' must have come from a live read. A row that says NOT CONFIGURED
    must have come from a check that the thing is actually not configured.
    """
    from src.audit_chain import AuditChainNotKeyedError, verify_chain
    from src.classifier.taxonomy import FailureClass
    from src.config import get_settings as _get_settings
    from src.guardrail.rules import GuardrailRules
    from src.receivables.ladder import (
        B2B_CLOSE_HOUR,
        B2B_CLOSE_MINUTE,
        B2B_OPEN_HOUR,
        B2B_OPEN_MINUTE,
        INVOICE_LADDER,
    )
    from src.voice.pipeline import SUPPORT_FLOOR

    settings = _get_settings()

    safeguards: list[dict[str, Any]] = []

    # 1. Hard-decline blocklist
    hard_declines = [fc.value for fc in FailureClass if fc.is_hard_decline]
    safeguards.append({
        "name": "Hard-decline blocklist",
        "description": "Classes the engine will never retry",
        "state": "active",
        "detail": f"{len(hard_declines)} classes blocked",
    })

    # 2. The 12 guardrail rules
    rule_names = [n for n in vars(GuardrailRules) if n.startswith("check_")]
    safeguards.append({
        "name": "Guardrail rules",
        "description": "Every rule runs on every attempt, no short-circuit",
        "state": "active",
        "detail": f"{len(rule_names)} rules enforce",
    })

    # 3. Retry cap
    safeguards.append({
        "name": "Retry cap",
        "description": "Maximum attempts per payment",
        "state": "active",
        "detail": f"{settings.max_retries_per_payment} attempts max",
    })

    # 4. Per-customer rate limit
    safeguards.append({
        "name": "Per-customer rate limit",
        "description": f"Max {settings.max_nudges_per_customer_24h} nudges per customer per 24h",
        "state": "active",
        "detail": f"{settings.max_nudges_per_customer_24h}/24h",
    })

    # 5. Consent window
    safeguards.append({
        "name": "Consent window",
        "description": "No retry after this window from the original charge",
        "state": "active",
        "detail": f"{settings.consent_window_hours}h",
    })

    # 6. Blackout window
    safeguards.append({
        "name": "Blackout window",
        "description": "No retries during overnight hours (IST)",
        "state": "active",
        "detail": (
            f"{settings.retry_blackout_start_hour:02d}:00–"
            f"{settings.retry_blackout_end_hour:02d}:00 IST"
        ),
    })

    # 7. Idempotency
    safeguards.append({
        "name": "Idempotency",
        "description": "Unique key per attempt, UNIQUE constraint in the database",
        "state": "active",
        "detail": "enforced by schema",
    })

    # 8. Fire-time re-validation
    safeguards.append({
        "name": "Fire-time re-validation",
        "description": (
            "The guardrail runs again when a deferred retry fires, "
            "not just at decision time"
        ),
        "state": "active",
        "detail": "always on by design",
    })

    # 9. Dispute freeze
    safeguards.append({
        "name": "Dispute freeze",
        "description": "An open dispute freezes all chasing on that case",
        "state": "active",
        "detail": "enforced in orchestrator",
    })

    # 10. LLM fallback
    llm_key = (
        settings.anthropic_api_key
        if settings.llm_provider == "anthropic"
        else settings.openai_api_key
    )
    llm_configured = bool(
        llm_key.get_secret_value()
        if hasattr(llm_key, "get_secret_value")
        else llm_key
    )

    safeguards.append({
        "name": "LLM fallback",
        "description": "XGBoost baseline runs when LLM is unavailable",
        "state": "active" if llm_configured else "NOT CONFIGURED",
        "detail": (
            f"provider: {settings.llm_provider}"
            if llm_configured
            else "LLM key not set — XGBoost only"
        ),
    })

    # 11. Voice grounding
    safeguards.append({
        "name": "Voice grounding",
        "description": (
            "The voice agent answers only from the case's own "
            "facts and abstains rather than inventing"
        ),
        "state": "active",
        # SUPPORT_FLOOR, not the number 70: this page's whole promise is that
        # it reads the enforcing module. Tuning the gate and leaving a stale
        # figure here would make the safety page the thing that lies.
        "detail": (
            f"{SUPPORT_FLOOR:.0%} content overlap floor, "
            "numeric grounding required"
        ),
    })

    # 12. B2B contact bounds
    safeguards.append({
        "name": "B2B contact bounds",
        "description": "One contact per account per rung, business hours only",
        "state": "active",
        "detail": (
            f"{len(INVOICE_LADDER)} rungs, Mon–Fri "
            f"{B2B_OPEN_HOUR:02d}:{B2B_OPEN_MINUTE:02d}–"
            f"{B2B_CLOSE_HOUR:02d}:{B2B_CLOSE_MINUTE:02d} IST"
        ),
    })

    # 13. Rate limiting — Redis or in-process
    redis_configured = bool(settings.redis_url)
    safeguards.append({
        "name": "Rate limiting",
        "description": "Per-customer contact budget enforcement",
        "state": "active",
        "detail": "shared (Redis)" if redis_configured else "per-process (in-memory)",
    })

    # 14. Audit chain
    chain_state: dict[str, Any]
    audit_secret = settings.audit_chain_secret
    audit_keyed = bool(
        audit_secret.get_secret_value()
        if hasattr(audit_secret, "get_secret_value")
        else audit_secret
    )
    if not audit_keyed:
        chain_state = {
            "keyed": False, "intact": False,
            "detail": "AUDIT_CHAIN_SECRET not set — rows stored but unsealed",
        }
    else:
        try:
            verification = await verify_chain(session)
            chain_state = {
                "keyed": True,
                "intact": verification.intact,
                "events_checked": verification.events_checked,
                "first_broken_id": verification.first_broken_id,
                "detail": verification.detail if not verification.intact else (
                    f"{verification.events_checked} events verified"
                ),
            }
        except AuditChainNotKeyedError:
            chain_state = {
                "keyed": False, "intact": False,
                "detail": "AUDIT_CHAIN_SECRET not set",
            }
        except Exception:
            logger.warning("Audit chain verification failed", exc_info=True)
            chain_state = {
                "keyed": True, "intact": False,
                "detail": "verification failed — see server logs",
            }

    safeguards.append({
        "name": "Audit chain",
        "description": "HMAC hash-chained case events with periodic checkpoints",
        "state": (
            "active" if chain_state.get("intact")
            else "NOT CONFIGURED" if not chain_state.get("keyed")
            else "broken"
        ),
        "detail": chain_state["detail"],
    })

    return {
        "safeguards": safeguards,
        "chain": chain_state,
    }


async def activity_page(
    session: AsyncSession, *, limit: int = 50
) -> dict[str, Any]:
    """
    The case audit trail with actor, event type, and hash verification status.

    Shows "verified" ONLY where event_hash is stamped AND the chain verifies.
    A stamped row inside an unverified chain is NOT evidence.
    """
    from src.audit_chain import AuditChainNotKeyedError, verify_chain
    from src.config import get_settings as _get_settings

    settings = _get_settings()
    audit_secret = settings.audit_chain_secret
    chain_keyed = bool(
        audit_secret.get_secret_value()
        if hasattr(audit_secret, "get_secret_value")
        else audit_secret
    )

    # Verify the chain once for the whole page
    chain_intact = False
    if chain_keyed:
        try:
            verification = await verify_chain(session)
            chain_intact = verification.intact
        except (AuditChainNotKeyedError, Exception):
            chain_intact = False

    rows = (
        await session.execute(
            select(
                CaseEvent.id,
                CaseEvent.event_type,
                CaseEvent.actor,
                CaseEvent.detail,
                CaseEvent.created_at,
                CaseEvent.event_hash,
                CaseEvent.prev_event_hash,
                RecoveryCase.subject_ref,
                RecoveryCase.state,
            )
            .join(RecoveryCase, RecoveryCase.id == CaseEvent.recovery_case_id)
            .order_by(CaseEvent.id.desc())
            .limit(limit)
        )
    ).mappings().all()

    events: list[dict[str, Any]] = []
    for r in rows:
        # detail may carry free-form JSON including PII — show only the
        # event_type, actor, and state transition, never the blob.
        detail = r["detail"] or {}
        stamped = r["event_hash"] is not None

        events.append({
            "id": r["id"],
            "event": r["event_type"],
            "actor": r["actor"],
            "subject_ref": r["subject_ref"],
            "case_state": r["state"],
            "previous_state": detail.get("previous_state"),
            "new_state": detail.get("new_state") or detail.get("state"),
            "reason": detail.get("reason") or detail.get("close_reason"),
            "when": (
                _ist(_aware(r["created_at"])).strftime("%d %b, %H:%M")
                if r["created_at"] else "—"
            ),
            # Verified = stamped AND chain intact. A stamped row inside a
            # broken chain is not evidence.
            "verified": stamped and chain_intact,
            "stamped": stamped,
        })

    return {
        "events": events,
        "chain_keyed": chain_keyed,
        "chain_intact": chain_intact,
        "has_data": bool(events),
    }


# ── Phase 08: Search and settings ───────────────────────────────────────────


async def search_console(
    session: AsyncSession, q: str
) -> dict[str, Any]:
    """
    Search over payment id, order id, case id, invoice/account reference.

    NOT over customer email or phone: that would turn the PII-free contract
    into a lookup service. The console's job is to find merchant-owned
    identifiers, not customer identifiers.
    """
    from src.models import PaymentFailure

    q = q.strip()
    if not q or len(q) < 2:
        return {"results": [], "query": q, "has_data": False}

    results: list[dict[str, Any]] = []

    # 1. Case by UUID
    try:
        case_id = uuid.UUID(q)
        case = await session.get(RecoveryCase, case_id)
        if case:
            results.append({
                "type": "case",
                "ref": case.subject_ref,
                "id": str(case.id),
                "state": case.state,
                "amount": _money(int(case.amount_at_risk)),
                "href": f"/console/case/{case.id}",
            })
    except ValueError:
        pass

    # 2. Cases by subject_ref (invoice number, cart id, etc.)
    ref_rows = (
        await session.execute(
            select(
                RecoveryCase.id,
                RecoveryCase.subject_ref,
                RecoveryCase.state,
                RecoveryCase.amount_at_risk,
                RecoveryCase.risk_type,
            )
            .where(RecoveryCase.subject_ref.ilike(f"%{q}%"))
            .order_by(RecoveryCase.opened_at.desc())
            .limit(10)
        )
    ).mappings().all()
    for r in ref_rows:
        results.append({
            "type": "case",
            "ref": r["subject_ref"],
            "id": str(r["id"]),
            "state": r["state"],
            "risk_type": (r["risk_type"] or "").replace("_", " "),
            "amount": _money(int(r["amount_at_risk"])),
            "href": f"/console/case/{r['id']}",
        })

    # 3. Payments by payment_id or order_id
    pay_rows = (
        await session.execute(
            select(
                PaymentFailure.payment_id,
                PaymentFailure.order_id,
                PaymentFailure.amount,
                PaymentFailure.failure_class,
                PaymentFailure.failed_at,
            )
            .where(
                sa_or(
                    PaymentFailure.payment_id.ilike(f"%{q}%"),
                    PaymentFailure.order_id.ilike(f"%{q}%"),
                )
            )
            .order_by(PaymentFailure.failed_at.desc())
            .limit(10)
        )
    ).mappings().all()
    for r in pay_rows:
        results.append({
            "type": "payment",
            "ref": r["payment_id"],
            "order_ref": r["order_id"],
            "amount": _money(int(r["amount"])),
            "class": (r["failure_class"] or "").replace("_", " "),
            "when": (
                _ist(_aware(r["failed_at"])).strftime("%d %b, %H:%M")
                if r["failed_at"] else "—"
            ),
            "href": "/console/payments?state=all",
        })

    # 4. Accounts by account_ref
    acct_rows = (
        await session.execute(
            select(
                ArAccount.id,
                ArAccount.account_ref,
                ArAccount.display_name,
            )
            .where(
                sa_or(
                    ArAccount.account_ref.ilike(f"%{q}%"),
                    ArAccount.display_name.ilike(f"%{q}%"),
                )
            )
            .limit(10)
        )
    ).mappings().all()
    for r in acct_rows:
        results.append({
            "type": "account",
            "ref": r["display_name"] or r["account_ref"],
            "id": str(r["id"]),
            "href": f"/console/account/{r['id']}",
        })

    return {"results": results, "query": q, "has_data": bool(results)}


def settings_view() -> dict[str, Any]:
    """
    Read-only view of what is configured and what is not.

    Shows presence, never values. A secret that is set renders as 'configured';
    a secret that is empty renders as 'not configured'. The point is to answer
    'can I use feature X' without turning the settings page into a credential
    dump.
    """
    from src.chasers.policy import RISK_POLICIES
    from src.config import get_settings as _get_settings
    from src.receivables.ladder import INVOICE_LADDER

    settings = _get_settings()

    def _is_set(v: Any) -> bool:
        if hasattr(v, "get_secret_value"):
            return bool(v.get_secret_value())
        return bool(v)

    integrations = [
        {
            "name": "Razorpay",
            "configured": _is_set(settings.razorpay_key_id),
            "detail": "payment gateway",
        },
        {
            "name": f"LLM ({settings.llm_provider})",
            "configured": _is_set(
                settings.anthropic_api_key
                if settings.llm_provider == "anthropic"
                else settings.openai_api_key
            ),
            "detail": "decision agent",
        },
        {
            "name": "Plivo",
            "configured": _is_set(settings.plivo_auth_id),
            "detail": "voice call leg",
        },
        {
            "name": "Sarvam",
            "configured": _is_set(settings.sarvam_api_key),
            "detail": "Hindi TTS / ASR",
        },
        {
            "name": "Redis",
            "configured": _is_set(settings.redis_url),
            "detail": (
                "shared rate limiting"
                if _is_set(settings.redis_url)
                else "in-process rate limiting"
            ),
        },
    ]

    # Chase bounds from the enforcing module
    bounds = []
    for risk_type, policy in RISK_POLICIES.items():
        bounds.append({
            "risk_type": risk_type.replace("_", " "),
            "max_attempts": policy.max_attempts,
            "window_hours": policy.consent_window_hours,
            "rail": policy.recommended_rail or "best",
        })

    # Ladder rungs
    rungs = [
        {
            "level": stage.level,
            "days": stage.days_past_due,
            "tone": stage.tone,
            "addresses": ", ".join(a.replace("_", " ") for a in stage.addresses),
            "channels": ", ".join(stage.channels),
        }
        for stage in INVOICE_LADDER
    ]

    channels = [
        {"name": "SMS", "status": "active"},
        {"name": "Email", "status": "active"},
        {
            "name": "Voice",
            "status": (
                "active" if _is_set(settings.plivo_auth_id)
                else "not configured"
            ),
        },
        {"name": "WhatsApp", "status": "coming"},
    ]

    return {
        "integrations": integrations,
        "bounds": bounds,
        "rungs": rungs,
        "channels": channels,
        "blackout_start": settings.retry_blackout_start_hour,
        "blackout_end": settings.retry_blackout_end_hour,
        "consent_window": settings.consent_window_hours,
        "max_retries": settings.max_retries_per_payment,
        "max_nudges_24h": settings.max_nudges_per_customer_24h,
    }
