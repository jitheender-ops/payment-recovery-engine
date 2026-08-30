"""
Hash-chain the append-only case_events audit trail.

Deliberately separate from cases.log_event(), which stays a synchronous,
no-I/O session.add() so an audit row always lands in the same transaction as
the change it describes — see that function's docstring. Chaining needs the
previous row's hash, which needs a query, which does not belong in that hot
path. Instead: events are written with event_hash/prev_event_hash left NULL,
and stamp_unhashed_events() fills them in afterward, in id order, each hash
folding in the one before it. Anyone holding AUDIT_CHAIN_SECRET can then
independently recompute the whole chain from the raw rows and prove nothing
in it has been altered — that's verify_chain(), and it does not trust the
stored hashes at all, only recomputes and compares.

The hash is keyed (HMAC-SHA256) rather than a bare SHA-256, because the
stamping algorithm is public — it is in this repository. A database
attacker can edit a row AND re-run the stamping pass to make the rewritten
chain look intact; what they cannot do is compute the key. The key lives in
the environment, outside the database, so a verification pass made after
that attack names the tampered row. Empty secret means fail-closed: stamping
and verifying refuse rather than produce a chain anyone could forge.

Run on demand via `python scripts/audit_chain.py --stamp` / `--verify`, or
from any async context via the two functions directly. Not wired into the
scheduler's tick — that loop has its own tight time budget (see
scheduler.py), and stamping is safe to run whenever, not required every tick.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings, reveal
from src.models import CaseEvent

GENESIS_HASH = "0" * 64


class AuditChainNotKeyedError(RuntimeError):
    """Raised when stamp/verify is attempted without AUDIT_CHAIN_SECRET set."""


def _chain_key() -> bytes:
    secret = reveal(get_settings().audit_chain_secret)
    if not secret:
        raise AuditChainNotKeyedError(
            "AUDIT_CHAIN_SECRET is not set — the hash chain is keyed on "
            "purpose (the stamping algorithm is public; only the key is not), "
            "so without it stamping and verifying are refused rather than "
            "producing a chain anyone with the database could forge. Set it "
            "in the environment and re-run; if rows were previously stamped "
            "under a different key or the old unkeyed algorithm, re-stamp "
            "them with --stamp after setting the key."
        )
    return secret.encode()


def _canonical_repr(row: CaseEvent) -> str:
    """
    Deterministic string form of everything about this row that must never
    change. sort_keys=True on `detail` because a JSONB round-trip through the
    database is not guaranteed to preserve key insertion order.
    """
    payload = {
        "id": row.id,
        "recovery_case_id": str(row.recovery_case_id),
        "event_type": row.event_type,
        "actor": row.actor,
        "detail": row.detail,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _chain_hash(prev_hash: str, row: CaseEvent, key: bytes) -> str:
    return hmac.new(
        key, (prev_hash + _canonical_repr(row)).encode(), hashlib.sha256
    ).hexdigest()


async def stamp_unhashed_events(session: AsyncSession, *, batch_size: int = 5000) -> int:
    """
    Fill in event_hash/prev_event_hash for every row that doesn't have one
    yet, in id order. Idempotent: rows that already have a hash are never
    touched or recomputed, so re-running this after new events have arrived
    only chains the new tail. Returns the number of rows stamped.

    Raises AuditChainNotKeyedError when AUDIT_CHAIN_SECRET is unset — an
    unkeyed chain is a chain anyone with the repo and database access can
    rewrite invisibly, which is the exact attack it exists to detect.
    """
    key = _chain_key()
    last_hashed = (
        await session.execute(
            sa.select(CaseEvent.event_hash)
            .where(CaseEvent.event_hash.is_not(None))
            .order_by(CaseEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    running_hash = last_hashed or GENESIS_HASH

    stamped = 0
    while True:
        rows = (
            await session.execute(
                sa.select(CaseEvent)
                .where(CaseEvent.event_hash.is_(None))
                .order_by(CaseEvent.id.asc())
                .limit(batch_size)
            )
        ).scalars().all()
        if not rows:
            break
        for row in rows:
            row.prev_event_hash = running_hash
            row.event_hash = _chain_hash(running_hash, row, key)
            running_hash = row.event_hash
            stamped += 1
        await session.flush()
    return stamped


@dataclass
class ChainVerification:
    intact: bool
    events_checked: int
    first_broken_id: int | None
    detail: str


async def verify_chain(session: AsyncSession) -> ChainVerification:
    """
    Recompute the entire chain from the raw rows and compare against what is
    stored. Trusts nothing stored — a tampered `detail`, `event_type` or
    `actor` on any row changes that row's recomputed hash, which breaks every
    hash after it, which this catches at the first row where recomputed and
    stored disagree.

    A mismatch also surfaces when rows were stamped under a different key
    (AUDIT_CHAIN_SECRET was rotated) or before the chain was keyed — the
    recomputation cannot tell an attacker's rewrite from an operator's
    rotation, which is deliberate: both mean the stored stamps no longer
    prove anything. Investigate if you did not rotate; re-stamp if you did.

    Streams row-by-row (`yield_per`) rather than loading the table, so
    verification stays O(1) in memory as the audit trail grows.
    """
    key = _chain_key()
    result = await session.stream(
        sa.select(CaseEvent).order_by(CaseEvent.id.asc()).execution_options(
            yield_per=500
        )
    )

    running_hash = GENESIS_HASH
    checked = 0
    async for row in result.scalars():
        checked += 1
        if row.event_hash is None:
            return ChainVerification(
                intact=False, events_checked=checked, first_broken_id=row.id,
                detail=f"event {row.id} was never stamped — run stamp_unhashed_events first",
            )
        expected = _chain_hash(running_hash, row, key)
        if expected != row.event_hash or row.prev_event_hash != running_hash:
            return ChainVerification(
                intact=False, events_checked=checked, first_broken_id=row.id,
                detail=(
                    f"event {row.id} hash mismatch — recomputed {expected[:12]}… "
                    f"but stored {row.event_hash[:12]}…"
                ),
            )
        running_hash = row.event_hash

    return ChainVerification(
        intact=True, events_checked=checked, first_broken_id=None,
        detail=f"{checked} events verified, chain intact",
    )
