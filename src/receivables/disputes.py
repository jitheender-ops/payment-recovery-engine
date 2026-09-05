"""Disputes — the customer answer that quiets the chase and alerts the merchant.

A dispute is not a complaint to soothe, it is a collections stop: chasing an
invoice the customer says is wrong is how a commercial relationship ends up
in a legal escalation. Every ladders' dispute flows (Chaser, Upflow, Gaviti
all ship one) agree on the shape:

    open dispute → freeze all automated contact → human resolves →
    upheld (case closes, nothing recovered) | rejected (chase resumes)

Freezing here is a flag the consolidation query reads — NOT a case-state
change. A disputed case keeps its place on the ladder and its budget; it is
simply excluded from contacting until the human answers. That keeps the
recovery case's state machine (open → recovered/exhausted/…) exactly as the
case layer defines it — a dispute is an overlay, not a state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.receivables.alerts import raise_alert
from src.receivables.models import CaseDispute

if TYPE_CHECKING:
    from src.models import RecoveryCase

logger = logging.getLogger(__name__)

# The customer's words, bounded. Long enough for a real explanation, short
# enough that a paste-bomb cannot turn the alert detail into a wall.
MAX_DISPUTE_REASON_CHARS = 2000


async def open_dispute(
    session: AsyncSession,
    case: RecoveryCase,
    *,
    reason: str,
    now: datetime | None = None,
) -> CaseDispute | None:
    """
    Record a customer dispute on a case. Returns None when refused.

    Refused when:
      * the case is terminal (nothing left to dispute — the chase is over)
      * a dispute is already open (double-tap on the page button; the first
        click is the record)
      * the reason is empty after trimming (a dispute with no words is a
        stray click, not a dispute)

    The caller owns the commit — same contract as cases.record_promise.
    """
    now = now or datetime.now(UTC)
    reason = reason.strip()
    if not reason:
        return None

    existing = await session.scalar(
        select(CaseDispute).where(
            CaseDispute.case_id == case.id, CaseDispute.status == "open"
        )
    )
    if existing is not None:
        return existing

    from src.cases import _TERMINAL  # the case layer's own terminal set

    if case.state in _TERMINAL:
        return None

    dispute = CaseDispute(
        case_id=case.id,
        reason=reason[:MAX_DISPUTE_REASON_CHARS],
        status="open",
        opened_at=now,
    )
    session.add(dispute)
    await session.flush()

    # Both, and for different readers. The alert is the merchant's worklist —
    # deliverable, dismissable, gone once handled. The case event is the audit
    # chain, which is where "why did chasing stop on this invoice" is answered
    # months later. Only the RESOLUTION was on the chain, so the trail recorded
    # the verdict on a dispute it never recorded anyone raising.
    from src.cases import log_event

    log_event(
        session, case, "dispute_opened", actor="customer",
        reason=dispute.reason, dispute_id=str(dispute.id),
    )
    await raise_alert(
        session,
        event_type="dispute_opened",
        case_ref=case.subject_ref,
        detail={
            "reason": dispute.reason,
            "amount_at_risk": case.amount_at_risk,
            "case_state": case.state,
        },
    )
    logger.info(
        "Dispute opened: case=%s ref=%s", case.id, case.subject_ref
    )
    return dispute


async def resolve_dispute(
    session: AsyncSession,
    dispute: CaseDispute,
    *,
    outcome: str,
    note: str | None = None,
    now: datetime | None = None,
) -> CaseDispute:
    """
    A human's verdict. ``outcome`` is "upheld" or "rejected".

    upheld   → the invoice was wrong: the case closes abandoned (the money
               is not recoverable through chasing; writing it off is the
               merchant's bookkeeping, the case record is the evidence).
    rejected → the invoice stands: the chase resumes at its next scheduled
               rung — next_action_at is untouched, so the ladder continues
               on the aging clock it was already on. No "punish the
               disputer" fast-forward; that behaviour invites disputes as a
               delay tactic, which is the opposite of what the freeze is for.

    Idempotent: the first resolution wins, a second call on a resolved
    dispute returns it unchanged.
    """
    now = now or datetime.now(UTC)
    if dispute.status != "open":
        return dispute

    dispute.status = outcome
    dispute.resolution_note = note
    dispute.resolved_at = now

    from src.models import RecoveryCase

    case = await session.get(RecoveryCase, dispute.case_id)
    if case is not None:
        if outcome == "upheld":
            from src.cases import close_case, log_event

            close_case(
                case,
                "abandoned",
                f"dispute upheld: {dispute.reason[:200]}",
            )
            log_event(
                session, case, "closed", actor="merchant",
                state="abandoned", reason=case.close_reason,
                dispute_id=str(dispute.id),
            )
        await raise_alert(
            session,
            event_type=(
                "dispute_upheld" if outcome == "upheld" else "dispute_rejected"
            ),
            case_ref=case.subject_ref if case else None,
            detail={"dispute_id": str(dispute.id), "note": note},
        )
    return dispute
