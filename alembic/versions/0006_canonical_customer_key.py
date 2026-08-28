"""Canonicalise customer identity across both ingestion rails.

Revision ID: 0006_canonical_customer_key
Revises: 0005_risk_events
Create Date: 2026-08-28

Everything that bounds outreach hangs off `customer_id`: the per-customer
retry and nudge tallies in retry_ledger (UNIQUE on that column) and the
opt-out, which closes cases by exact match. Two rails derived it two
different ways and neither normalised, so one person routinely held two keys:

  * the payment rail keyed on `email or contact` off the Razorpay webhook,
    while the risk rail preferred the merchant's own opaque customer_id
  * "A@B.com" and "a@b.com" are one inbox and were two keys
  * "+91 98765 43210" and "+919876543210" are one phone and were two

Doubling the contact limits is the visible half. The half that matters is the
opt-out: a customer who pressed "stop" on their recovery page kept being
chased under the sibling identity.

src.cases.customer_key() is now the single derivation ("email:…" / "phone:…" /
"id:…"). This revision rewrites the rows already in the table onto it. The
rewrite is the whole point — leaving them behind would strand every existing
ledger and every open case under a key the code no longer produces, which
would silently RESET the tallies and re-open contact budgets that had been
spent.

Two rows can now normalise onto the same key, and retry_ledger.customer_id is
UNIQUE. Merging is done conservatively, always in the customer's favour:

  * counters take the MAX, not the sum — the tallies are per-window and the
    duplicate rows overlap; summing would over-count a limit into permanent
    exhaustion, while max preserves the strictest true observation
  * window anchors and last_* take the EARLIEST/LATEST that keeps the window
    open longest, so a merge never hands back budget the customer had used
  * consent_status: a single `opted_out` among the duplicates wins outright.
    Consent is not a majority vote — one "stop" is a stop.

Idempotent and inspector-guarded like every revision here, so a re-run on an
already-migrated database is a no-op.
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0006_canonical_customer_key"
down_revision = "0005_risk_events"
branch_labels = None
depends_on = None

_PREFIXES = ("email:", "phone:", "id:")


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _canonical(value: str | None) -> str | None:
    """
    A standalone copy of src.cases.canonical_key.

    Deliberately NOT an import: a migration must keep doing what it did on the
    day it was written, and importing application code makes an old revision's
    behaviour change under it the next time that function is edited.
    """
    if not value:
        return None
    if value.startswith(_PREFIXES):
        return value
    if "@" in value:
        cleaned = value.strip().lower()
        return f"email:{cleaned}" if "@" in cleaned else None
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 8:
        return f"phone:{digits}"
    cleaned = value.strip()
    return f"id:{cleaned}" if cleaned else None


def _merge_ledger_rows(bind: sa.engine.Connection) -> None:
    """Rewrite retry_ledger.customer_id, merging rows that collide."""
    rows = bind.execute(
        sa.text(
            "SELECT id, customer_id, total_retries_24h, total_nudges_24h, "
            "last_retry_at, last_nudge_at, retries_window_started_at, "
            "nudges_window_started_at, blocked_until, consent_status, opted_out_at "
            "FROM retry_ledger ORDER BY id"
        )
    ).mappings().all()

    winners: dict[str, dict] = {}
    losers: list[int] = []

    for row in rows:
        key = _canonical(row["customer_id"])
        if key is None:
            # Nothing identifiable in there. Leave it exactly as found rather
            # than inventing a key for a row no lookup will ever match again.
            continue
        held = winners.get(key)
        if held is None:
            winners[key] = dict(row) | {"customer_id": key}
            continue
        losers.append(row["id"])
        # Counters: MAX, not SUM. These are per-window tallies and the
        # duplicate rows cover overlapping windows, so summing would invent
        # contacts that never happened and lock the customer out.
        for col in ("total_retries_24h", "total_nudges_24h"):
            held[col] = max(held[col] or 0, row[col] or 0)
        # Latest contact and earliest window anchor: whichever keeps the
        # current window open longest, so a merge never refunds spent budget.
        for col in ("last_retry_at", "last_nudge_at", "blocked_until"):
            held[col] = _later(held[col], row[col])
        for col in ("retries_window_started_at", "nudges_window_started_at"):
            held[col] = _later(held[col], row[col])
        # One "stop" is a stop.
        if row["consent_status"] == "opted_out":
            held["consent_status"] = "opted_out"
            held["opted_out_at"] = _earlier(held["opted_out_at"], row["opted_out_at"])

    for key, held in winners.items():
        bind.execute(
            sa.text(
                "UPDATE retry_ledger SET customer_id = :key, "
                "total_retries_24h = :retries, total_nudges_24h = :nudges, "
                "last_retry_at = :last_retry, last_nudge_at = :last_nudge, "
                "retries_window_started_at = :retry_window, "
                "nudges_window_started_at = :nudge_window, "
                "blocked_until = :blocked, consent_status = :consent, "
                "opted_out_at = :opted_out WHERE id = :id"
            ),
            {
                "key": key,
                "retries": held["total_retries_24h"],
                "nudges": held["total_nudges_24h"],
                "last_retry": held["last_retry_at"],
                "last_nudge": held["last_nudge_at"],
                "retry_window": held["retries_window_started_at"],
                "nudge_window": held["nudges_window_started_at"],
                "blocked": held["blocked_until"],
                "consent": held["consent_status"],
                "opted_out": held["opted_out_at"],
                "id": held["id"],
            },
        )
    for loser_id in losers:
        bind.execute(
            sa.text("DELETE FROM retry_ledger WHERE id = :id"), {"id": loser_id}
        )


def _later(a: object, b: object) -> object:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b  # type: ignore[operator]


def _earlier(a: object, b: object) -> object:
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b  # type: ignore[operator]


def upgrade() -> None:
    bind = op.get_bind()

    if _has_table("retry_ledger"):
        _merge_ledger_rows(bind)

    # recovery_cases.customer_id has no UNIQUE constraint, so it is a plain
    # rewrite — but it MUST happen, because record_opt_out() closes cases by
    # exact match against the ledger key.
    if _has_table("recovery_cases"):
        rows = bind.execute(
            sa.text(
                "SELECT id, customer_id FROM recovery_cases "
                "WHERE customer_id IS NOT NULL"
            )
        ).mappings().all()
        for row in rows:
            key = _canonical(row["customer_id"])
            if key is not None and key != row["customer_id"]:
                bind.execute(
                    sa.text(
                        "UPDATE recovery_cases SET customer_id = :key WHERE id = :id"
                    ),
                    {"key": key, "id": row["id"]},
                )


def downgrade() -> None:
    """
    Strip the namespace prefix back off.

    Lossy and knowingly so: the merge above deleted duplicate ledger rows and
    lowercased addresses, and neither is recoverable. This restores the SHAPE
    the previous revision expects — a raw email or phone in the column — which
    is what the code at that revision reads. Down-migrating is a rollback of
    code, not a restore of data; take a backup first.
    """
    bind = op.get_bind()
    for table in ("retry_ledger", "recovery_cases"):
        if not _has_table(table):
            continue
        rows = bind.execute(
            sa.text(
                f"SELECT id, customer_id FROM {table} WHERE customer_id IS NOT NULL"  # noqa: S608
            )
        ).mappings().all()
        for row in rows:
            value = row["customer_id"]
            for prefix in _PREFIXES:
                if value.startswith(prefix):
                    bind.execute(
                        sa.text(
                            f"UPDATE {table} SET customer_id = :v WHERE id = :id"  # noqa: S608
                        ),
                        {"v": value[len(prefix):], "id": row["id"]},
                    )
                    break
