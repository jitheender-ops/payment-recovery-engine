"""The staged B2B dunning ladder — tone, channel and routing by days-past-due.

Flat ladders are why dunning gets a bad name: the same words at hour 0 and
day 28 either start hostile or end toothless. The research consensus
(Chaser/Upflow/Gaviti's published ladders) is a small set of rungs with
rising firmness, changing WHO is addressed as the invoice ages, and a
consequence stated once at the end.

This module is a pure policy, in the exact discipline of
``src/chasers/policy.py``: frozen dataclasses, one code-reviewed constant per
product promise, no env-var configurability. The numbers it encodes:

* 5 customer-facing rungs (plus one call task), not a wall of messages
* pre-due reminder OFF by default — chasing an invoice that is not yet late
  reads as surveillance and burns the first rung's good will
* the rung gaps WIDEN (3 → 7 → 7 → 7 days): each silence buys trust in the
  "we are serious" signal and stays inside the 30-day consent window with
  4 contacts — the same budget the frozen invoice_overdue policy promises
* business hours only: Mon–Fri 09:30–18:30 IST. A person at a desk is the
  audience, not a phone in a pocket; the existing 23:00–07:00 blackout stays
  as the floor under everything

Stage 0 (pre-due) exists in the structure but is reachable only when the
merchant pushed the invoice with a future ``due_at`` and explicitly opted
into pre-due reminders — both are integration-phase switches; the ladder
validates against ``first_action_hours`` either way so no configuration can
spend budget before the policy allows contact.

Escalation semantics (integration phase wires these into chase_case):

* ``escalation_level`` on the case IS the stage index — the column already
  exists and is already bumped by attach_attempt; the ladder only reads it
* a promise-to-pay pauses the ladder via the EXISTING next_action_at
  mechanism (record_promise) — nothing here re-implements pausing
* a BROKEN promise resumes at the next firmer stage, never back at
  friendly: ``stage_after_break()`` enforces the one-way ratchet
* a call task does NOT consume customer-contact budget — it is merchant-side
  work, and mixing the two would quietly rewrite the policy's promise
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


# ── Contact routing ──────────────────────────────────────────────────────────

# The roles a ladder rung addresses, in escalation order. Integer keys so the
# sort in accounts.active_contacts() is total and stable.
ROLE_PRECEDENCE: dict[str, int] = {
    "ap_clerk": 0,
    "finance_manager": 1,
    "escalation": 2,
}


# ── Business-hours window ───────────────────────────────────────────────────

B2B_OPEN_HOUR = 9   # 09:30 IST
B2B_OPEN_MINUTE = 30
B2B_CLOSE_HOUR = 18  # 18:30 IST
B2B_CLOSE_MINUTE = 30


def is_b2b_contact_time(dt: datetime) -> bool:
    """True inside Mon–Fri 09:30–18:30 IST. Naive input is treated as UTC.

    Weekday 5/6 (Sat/Sun) are the only days excluded — Indian bandhs and
    regional holidays are a merchant's judgement call, not a number this
    policy can honestly freeze.
    """
    local = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    local = local.astimezone(IST)
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return (B2B_OPEN_HOUR * 60 + B2B_OPEN_MINUTE) <= minutes < (
        B2B_CLOSE_HOUR * 60 + B2B_CLOSE_MINUTE
    )


def next_b2b_window(dt: datetime) -> datetime:
    """
    The next moment a B2B contact may fire, at or after ``dt``.

    The sweep's defer pattern (orchestrator.chase_case's blackout deferral):
    never burn a budget slot on a wall you can see — reschedule to the wall's
    edge and keep the attempt. Minute-aligned because a second-precision
    defer guarantees the next tick re-defers indefinitely on a slow clock.
    """
    local = (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(IST)
    # Walk forward minute by minute until inside the window. Bounded: at most
    # ~2.5 days of minutes (a Friday 18:29 rollover walks to Monday 09:30).
    # ponytail: linear scan; a closed-form next-window is the upgrade if
    # this shows in a profile (it will not — it runs once per deferred case).
    candidate = local.replace(second=0, microsecond=0)
    for _ in range(60 * 24 * 3):
        if is_b2b_contact_time(candidate):
            return candidate
        candidate = candidate + timedelta(minutes=1)
    # Unreachable: any three-day span contains a Mon–Fri morning.
    return candidate  # pragma: no cover


# ── The ladder stages ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class LadderStage:
    """One rung: when it fires, how firm it is, who it addresses."""

    # The stage's index — what escalation_level must read for this rung.
    level: int
    # Days past due this rung fires at.
    days_past_due: int
    # The one-word tone the copy carries. Part of the frozen structure: the
    # LLM prompt and fallback templates both branch on it, and a free-text
    # tone drifts into either hostile or toothless without a review.
    tone: str
    # Who the rung addresses, by contact role (ArContact.role). Later rungs
    # add roles — day 14 addresses the finance manager AND the clerk, which
    # is the published-pattern for "the person you emailed stopped
    # answering; we are asking their boss now".
    addresses: tuple[str, ...]
    # The channels this rung uses, in send order. SMS always carries the
    # link; email carries the statement. "call_task" is NOT here — it is
    # merchant-side work, never customer-facing contact.
    channels: tuple[str, ...]
    # True when this rung spends customer-contact budget. Pre-due and call
    # tasks do not; the four dunning rungs do.
    spends_budget: bool = True
    # When this rung is the pre-due reminder.
    pre_due: bool = False

    # No idempotency need: stages are values, not rows.


# The frozen ladder. days_past_due is cumulative from the invoice's due date
# (case.due_at), NOT from case-open — aging runs on the money's own clock.
INVOICE_LADDER: tuple[LadderStage, ...] = (
    LadderStage(
        level=0,
        days_past_due=0,
        tone="courtesy",
        addresses=("ap_clerk",),
        channels=("email",),
        spends_budget=False,
        pre_due=True,
    ),
    LadderStage(
        level=1,
        days_past_due=1,
        tone="friendly",
        addresses=("ap_clerk",),
        channels=("email", "sms"),
    ),
    LadderStage(
        level=2,
        days_past_due=7,
        tone="firm",
        addresses=("ap_clerk", "finance_manager"),
        channels=("email", "sms"),
    ),
    LadderStage(
        level=3,
        days_past_due=14,
        tone="urgent",
        addresses=("finance_manager",),
        channels=("email", "sms", "call_task"),
    ),
    LadderStage(
        level=4,
        days_past_due=21,
        tone="final",
        addresses=("finance_manager", "escalation"),
        channels=("email", "sms"),
    ),
)


def stage_for_level(level: int) -> LadderStage | None:
    """The stage whose index this escalation_level maps to, or None past the end.

    escalation_level 0 means "no contact yet" on a fresh case — the sweep
    must consult the aging clock (stage_for_aging), not this function, when
    deciding the FIRST rung. This function is the mapping AFTER the first
    contact has landed and the level has been bumped.
    """
    # `level < len`, not `<=`: the ladder is indexed 0..len-1, and
    # stage_after_break can legitimately hand back len-1, so the inclusive
    # bound walked one past the end and raised IndexError instead of
    # returning the None this signature promises.
    if 1 <= level < len(INVOICE_LADDER):
        return INVOICE_LADDER[level]
    return None


def stage_for_aging(days_past_due: int) -> LadderStage | None:
    """The rung the aging clock demands, ignoring escalation_level.

    The aging clock is the truth (the invoice is 7 days late regardless of
    how many times we have emailed); the escalation level is the history.
    The sweep uses this to place a case on its correct rung, then the
    ratchet rule below keeps the two from moving backwards.
    """
    if days_past_due <= 0:
        return INVOICE_LADDER[0] if INVOICE_LADDER[0].pre_due else None
    # Skip the pre-due stage: it fires at days_past_due=0 and below, which
    # the guard above already handled. The highest rung whose threshold has
    # been reached wins — aging never moves a case backwards.
    match: LadderStage | None = None
    for stage in INVOICE_LADDER[1:]:
        if days_past_due >= stage.days_past_due:
            match = stage
    return match


def stage_after_break(current_level: int) -> int:
    """
    The level a case resumes at after a broken promise.

    The one-way ratchet: a customer who promised and did not pay resumes at
    the NEXT firmer rung, never back at friendly. Resuming at the old rung
    teaches every buyer that a promise is a free pause button, which is the
    exact behaviour a promise system must not teach.

    Past the top of the ladder the chase is over — the budget is spent and
    the policy's stopping rule fires; this function returns the top level so
    the caller can close as exhausted rather than loop.
    """
    return min(current_level + 1, len(INVOICE_LADDER) - 1)


def next_stage_gap_hours(current_level: int) -> float:
    """
    The quiet hours between finishing this rung and the next being due.

    Read by the sweep when advancing a case's next_action_at: the rungs sit
    at 3 → 7 → 7 → 7 day boundaries, so the gap is derived from the ladder's
    own thresholds, keeping one source of truth. A case that lands BETWEEN
    rungs (a promise pushed next_action_at out and then broke) is rescheduled
    by aging, not by this gap.
    """
    next_level = current_level + 1
    if next_level >= len(INVOICE_LADDER):
        # Past the final rung: the policy's consent window (30 days) is the
        # only remaining boundary; return it so the caller can let expiry
        # close the case on its own clock.
        return float(30 * 24)
    return float(
        (INVOICE_LADDER[next_level].days_past_due
         - INVOICE_LADDER[current_level].days_past_due) * 24
    )
