"""AR aging — the receivables analytics, computed honestly from cases.

The two numbers every AR platform leads with are DSO and an aging table.
Both are trivially gameable, so this module computes them with the
codebase's honesty discipline:

* ``aging_buckets`` reads OPEN invoice cases only, aged on due_at — a case
  the engine already closed is not outstanding, no matter what its state
  string is.
* ``avg_days_to_pay`` is computed over cases the ENGINE closed as recovered
  AND cases closed by external payment — both are real collection outcomes.
  It is named days-to-pay, NOT "DSO": true DSO needs the merchant's total
  credit sales from their ERP, which this engine does not have, and a
  number that silently pretends otherwise is a lie on a finance surface.
* Promise effectiveness (kept vs broken) is the segment input and a core
  dunning KPI both; one query answers both consumers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import PromiseToPay, RecoveryCase

# The buckets, in order, as (label, lo_days, hi_days). hi_days None = open.
BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("not_due", -10_000, 0),
    ("1_7", 1, 7),
    ("8_14", 8, 14),
    ("15_30", 15, 30),
    ("30_plus", 31, None),
)


async def aging_buckets(
    session: AsyncSession, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """
    Open invoice cases bucketed by days past due, with amounts.

    invoice_overdue cases only: the other risk types have no due date the
    aging ladder runs on (their due_at semantics differ), and a mixed aging
    table would answer a question the merchant never asked.
    """
    now = now or datetime.now(UTC)

    rows = (
        await session.execute(
            select(RecoveryCase.due_at, RecoveryCase.amount_at_risk,
                   RecoveryCase.amount_recovered)
            .where(
                RecoveryCase.risk_type == "invoice_overdue",
                RecoveryCase.state == "open",
            )
        )
    ).all()

    buckets: list[dict[str, Any]] = [
        {"label": label, "count": 0, "outstanding_paise": 0}
        for label, _, _ in BUCKETS
    ]
    counts = [0 for _ in BUCKETS]
    outstanding_paise = [0 for _ in BUCKETS]
    for due_at, at_risk, recovered in rows:
        if due_at is None:
            continue
        due = due_at if due_at.tzinfo else due_at.replace(tzinfo=UTC)
        days_past = (now - due).days
        outstanding = int(at_risk) - int(recovered or 0)
        for i, (_, lo, hi) in enumerate(BUCKETS):
            if days_past >= lo and (hi is None or days_past <= hi):
                counts[i] += 1
                outstanding_paise[i] += max(0, outstanding)
                break
    for i, bucket in enumerate(buckets):
        bucket["count"] = counts[i]
        bucket["outstanding_paise"] = outstanding_paise[i]
    return buckets


async def avg_days_to_pay(session: AsyncSession) -> float | None:
    """
    Mean days from due to recovered, over recovered invoice cases.

    None when no cases qualify — an honest "not enough history", never 0.
    Cases recovered externally (recovered_via_attempt_id NULL but state
    recovered) count too: the money arrived and the case closed; the engine
    simply did not earn it, which is a different fact from timing.

    Computed in Python from two columns, like aging_buckets above, rather
    than in SQL. The SQL version was doing `avg(... max(recovered_at) ...)` —
    a NESTED AGGREGATE, which Postgres and SQLite both reject outright
    ("aggregate function calls cannot be nested" / "misuse of aggregate
    function max()"). The `max()` served no purpose: there is one recovered_at
    per row. It never returned a number on any dialect; the console's
    try/except simply hid that, so the panel showed no average and nobody
    could tell the difference between "no history" and "broken query".

    Row-bounded by recovered invoice cases, and the sibling function already
    reads its rows the same way — one approach in one module beats a clever
    aggregate that does not run.
    """
    rows = (
        await session.execute(
            select(RecoveryCase.due_at, RecoveryCase.recovered_at).where(
                RecoveryCase.risk_type == "invoice_overdue",
                RecoveryCase.state == "recovered",
                RecoveryCase.due_at.isnot(None),
                RecoveryCase.recovered_at.isnot(None),
            )
        )
    ).all()

    spans: list[float] = []
    for due_at, recovered_at in rows:
        if due_at is None or recovered_at is None:  # pragma: no cover — filtered above
            continue
        due = due_at if due_at.tzinfo else due_at.replace(tzinfo=UTC)
        paid = recovered_at if recovered_at.tzinfo else recovered_at.replace(tzinfo=UTC)
        spans.append((paid - due).total_seconds() / 86_400.0)

    if not spans:
        return None
    return round(sum(spans) / len(spans), 1)


async def promise_effectiveness(session: AsyncSession) -> dict[str, float | None]:
    """
    Kept rate over RESOLVED promises on invoice cases: kept / (kept+broken).

    Cancelled promises are excluded from both numerator and denominator —
    a promise the ENGINE cancelled (case closed, opt-out) says nothing
    about payer behaviour. None when no resolved promises exist.

    Two fixes live here, both of which the console's try/except was hiding:

    `count().filter()`, not `sum(1 * (status = 'kept'))`. The old form
    multiplies an integer by a boolean, which SQLite happily evaluates as 0/1
    and Postgres refuses outright ("operator does not exist: integer *
    boolean") — the same dialect trap as the `delivered = 0` compare in the
    console's alerts query, and the same silent-empty outcome.

    And the JOIN this docstring always claimed. The query counted EVERY
    promise in the table, so a voice promise on an abandoned cart moved a
    number labelled "invoice promise effectiveness".
    """
    result = await session.execute(
        select(
            func.count(PromiseToPay.id).filter(PromiseToPay.status == "kept"),
            func.count(PromiseToPay.id).filter(PromiseToPay.status == "broken"),
        )
        .select_from(PromiseToPay)
        .join(RecoveryCase, RecoveryCase.id == PromiseToPay.recovery_case_id)
        .where(RecoveryCase.risk_type == "invoice_overdue")
    )
    k, b = result.one()
    k = int(k or 0)
    b = int(b or 0)
    resolved = k + b
    return {
        "kept": float(k),
        "broken": float(b),
        "kept_rate": (k / resolved) if resolved else None,
    }
