"""External payments — the NEFT/cheque/cash reality the webhook never sees.

The engine hears about Razorpay captures; Indian B2B money mostly moves by
NEFT/RTGS/IMPS and cheque. Without a closure path for those, every
externally-paid invoice keeps being chased until the window expires — the
engine messages a customer who already paid, which is the single most
damaging thing a dunning system can do to a commercial relationship.

The discipline is honesty, not optimism:

* The money COUNTS (the case closes, amount_recovered rises) but is never
  CLAIMED as engine-attributed: recovered_via_attempt_id stays NULL — the
  exact semantics attribute_capture already gives self-recovery via
  order_ref. The headline "recovered by the engine" stays truthful.
* Idempotent per (case, ref): the merchant's re-POST of the same bank ref
  must not double-count the rupees. First write wins.
* Refusal, not guessing: a ref on a terminal case is refused outright — an
  external payment arriving after close is an overpayment-shaped anomaly
  the merchant must look at, not something this code resolves silently.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.receivables.alerts import raise_alert

logger = logging.getLogger(__name__)


async def record_external_payment(
    session: AsyncSession,
    *,
    case_id: str,
    amount_paise: int,
    paid_ref: str,
    paid_at: datetime | None = None,
    method: str = "neft",
    note: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """
    Close a case on money that arrived outside the payment rail.

    Returns one of: "recorded", "already_recorded", "refused_terminal",
    "refused_amount", "refused_no_case" — a closed vocabulary the merchant
    API can map to status codes without parsing English.

    The caller owns the commit. Cases are looked up by UUID string to keep
    this module standalone (no RecoveryCase import at module import time —
    the integration phase's alembic migration has not linked the schemas
    yet, and the accounts module documents why queries key on stable ids).
    """
    import uuid as _uuid

    from src.cases import _TERMINAL, close_case, log_event
    from src.models import RecoveryCase

    now = now or datetime.now(UTC)
    try:
        case_uuid = _uuid.UUID(str(case_id))
    except ValueError:
        return "refused_no_case"

    if (
        isinstance(amount_paise, bool)
        or not isinstance(amount_paise, int)
        or amount_paise <= 0
    ):
        return "refused_amount"

    case = await session.get(RecoveryCase, case_uuid)
    if case is None:
        return "refused_no_case"

    # Idempotency: the same bank ref on the same case is one payment. The
    # case row is the natural dedup key — a closed case cannot take a second
    # external payment, and an open one carries the ref only once.
    if case.recovered_ref == paid_ref:
        return "already_recorded"

    if case.state in _TERMINAL:
        return "refused_terminal"

    case.amount_recovered += amount_paise
    # NULL on purpose: the money counts, the engine does not claim it.
    case.recovered_via_attempt_id = None
    case.recovered_ref = paid_ref
    case.recovered_at = paid_at or now

    close_case(case, "recovered", f"external payment via {method}: {paid_ref}")
    log_event(
        session,
        case,
        "reconciled",
        actor="merchant",
        amount=amount_paise,
        method=method,
        paid_ref=paid_ref,
        note=note,
    )
    await raise_alert(
        session,
        event_type="external_payment_recorded",
        case_ref=case.subject_ref,
        detail={
            "amount": amount_paise,
            "method": method,
            "paid_ref": paid_ref,
        },
    )
    logger.info(
        "External payment recorded: case=%s ref=%s amount=%d method=%s",
        case.id, case.subject_ref, amount_paise, method,
    )
    return "recorded"
