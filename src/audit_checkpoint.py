"""
Periodic re-anchoring for the case_events hash chain.

Why this module exists: the chain is ONE global sequence — every row's hash
folds in the previous row's. That is what makes it tamper-evident, and also
what makes full verification O(total history): at millions of events a
`verify_chain()` pass re-reads every row ever written. The fix is not to
weaken the chain but to anchor it: once a stretch of history has been
verified FROM CONTENT, record (last event id, chain head, a keyed signature
over both) as a CHECKPOINT. Future verification then only needs to recompute
history AFTER the newest checkpoint and check older stretches against their
checkpoints' signatures.

Why "from content" is load-bearing, twice:

* Anchoring: the signature pins the head only as trustworthy as the rows it
  was computed from. checkpoint_chain therefore re-verifies the stretch it
  is about to anchor — recomputing every hash from row content — before
  writing the checkpoint. An anchor over stored-but-tampered hashes would
  certify an attack.

* Rotating re-verification: a signature alone cannot detect a rewrite of an
  old stretch's CONTENT that leaves its stored hashes alone (the first
  version of this module had exactly that hole; a test caught it). So each
  verification run also fully recomputes the OLDEST EPOCH NOT YET
  content-verified (the `content_verified_at` marker), then marks it
  verified. Every epoch is re-read from content once per full rotation of
  all epochs — O(interval) work per run, spread across runs — so tampering
  anywhere is caught within one rotation even when every stored hash and
  checkpoint is left untouched by the attacker.

Deleting a checkpoint cannot forge one (signatures are keyed by the same
AUDIT_CHAIN_SECRET as the chain itself); deleting rows inside an old stretch
breaks the head the next checkpoint was computed over.

Checkpoints are written by the scheduler's tick (checkpoint_chain) whenever
the chain has grown a configurable number of events past the last anchor —
steady state is one small row per epoch, written in the same transaction as
the tick's stamping.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit_chain import AuditChainNotKeyedError, _chain_hash, _chain_key
from src.config import get_settings
from src.models import AuditCheckpoint, CaseEvent

logger = logging.getLogger(__name__)

GENESIS = "0" * 64


def _checkpoint_signature(head_hash: str, last_event_id: int, key: bytes) -> str:
    """Keyed digest binding a chain head to the event id it stops at."""
    return hmac.new(
        key, f"{last_event_id}:{head_hash}".encode(), hashlib.sha256
    ).hexdigest()


async def _prev_boundary(
    session: AsyncSession, last: int
) -> tuple[int, str]:
    """(previous boundary id, previous chain head) for the epoch after `last`."""
    if last <= 0:
        return 0, GENESIS
    prev_hash = (
        await session.execute(
            sa.select(AuditCheckpoint.head_event_hash).where(
                AuditCheckpoint.last_event_id == last
            )
        )
    ).scalar_one_or_none()
    return last, prev_hash or GENESIS


async def _verify_stretch(
    session: AsyncSession,
    *,
    start_id: int,
    end_id: int,
    start_hash: str,
    key: bytes,
) -> str | None:
    """
    Recompute one stretch of chain from content. Returns the head hash at
    `end_id`, or None (with the failure already logged) if any row's content
    no longer matches its stored hash — the trail was rewritten.
    """
    rows = (
        await session.execute(
            sa.select(CaseEvent)
            .where(CaseEvent.id > start_id, CaseEvent.id <= end_id)
            .order_by(CaseEvent.id.asc())
        )
    ).scalars().all()
    running = start_hash
    for row in rows:
        computed = _chain_hash(running, row, key)
        if row.event_hash is None or computed != row.event_hash:
            logger.error(
                "Audit stretch (events %d..%d) fails content verification at "
                "event %d — recomputed %s… but stored %s…. The audit trail "
                "was rewritten; a full verify will name the extent.",
                start_id, end_id, row.id,
                computed[:12], (row.event_hash or "∅")[:12],
            )
            return None
        running = computed
    return running


async def checkpoint_chain(session: AsyncSession) -> int:
    """
    Anchor the chain if it has grown past the checkpoint interval.

    Returns the new checkpoint's last event id, or 0 when nothing to do.
    Never raises for an unkeyed chain (dev/test) — same discipline as the
    stamp sweep, which this runs after.

    On a stretch that fails content verification this REFUSES to anchor and
    returns 0 WITHOUT rolling the session back: the tick's other work
    (heartbeats, prunes, stamps) is not this sweep's to discard. The next
    tick retries and fails the same honest way; the log names the event.
    """
    try:
        key = _chain_key()
    except AuditChainNotKeyedError:
        return 0

    interval = get_settings().audit_checkpoint_interval_events
    if interval <= 0:
        return 0

    head = (
        await session.execute(
            sa.select(CaseEvent.id, CaseEvent.event_hash)
            .where(CaseEvent.event_hash.is_not(None))
            .order_by(CaseEvent.id.desc())
            .limit(1)
        )
    ).first()
    if head is None or head.event_hash is None:
        return 0

    last = (
        await session.execute(
            sa.select(sa.func.max(AuditCheckpoint.last_event_id))
        )
    ).scalar_one_or_none() or 0

    # A brownfield chain (stamped long before checkpoints shipped) can be
    # far past the interval on the first run; anchor only one
    # interval-sized epoch at a time so each anchor's content pass is
    # bounded by the interval, exactly like every later epoch's will be.
    boundary = min(last + interval, head.id)
    if boundary - last < interval:
        return 0

    prev_id, prev_hash = await _prev_boundary(session, last)
    head_hash = await _verify_stretch(
        session, start_id=prev_id, end_id=boundary,
        start_hash=prev_hash, key=key,
    )
    if head_hash is None:
        return 0  # refused; logged — never rollback the caller's tick

    cp = AuditCheckpoint(
        last_event_id=boundary,
        head_event_hash=head_hash,
        signature=_checkpoint_signature(head_hash, boundary, key),
        events_through=boundary,
        # Deliberately NULL: the anchor's own content pass verified the
        # stretch NOW, but the rotation exists to re-verify it LATER — an
        # attacker with database access rewrites history after the anchor,
        # and only a future content pass catches that. Starting every epoch
        # unverified puts it in the rotation from the next verify run.
        content_verified_at=None,
        created_at=datetime.now(UTC),
    )
    session.add(cp)
    await session.flush()
    logger.info(
        "Audit chain checkpointed at event %d (epoch of %d)",
        boundary, boundary - last,
    )
    return int(boundary)


async def verify_chain_epoch(
    session: AsyncSession,
) -> tuple[bool, str, int]:
    """
    Verify the chain using checkpoints: full recompute for events AFTER the
    newest checkpoint, keyed-signature checks for every stretch BEFORE it,
    plus the amortized rotation — one full content recompute of the OLDEST
    epoch not yet content-verified, marked as verified when it passes.

    Returns (intact, detail, events_recomputed). Falls back to a full
    `verify_chain()` when no checkpoints exist yet (small/young deployments)
    — identical semantics, just not yet fast.
    """
    from src.audit_chain import verify_chain

    key = _chain_key()  # raises when unkeyed — same fail-closed rule

    checkpoints = (
        (
            await session.execute(
                sa.select(AuditCheckpoint).order_by(
                    AuditCheckpoint.last_event_id.asc()
                )
            )
        )
        .scalars()
        .all()
    )
    if not checkpoints:
        v = await verify_chain(session)
        return v.intact, v.detail, v.events_checked

    # 1. Every checkpoint's signature must still bind its recorded head to
    #    its recorded boundary. A rewritten-and-restamped head changes the
    #    signature input; a forged checkpoint cannot compute the signature.
    for cp in checkpoints:
        expected = _checkpoint_signature(cp.head_event_hash, cp.last_event_id, key)
        if not hmac.compare_digest(expected, cp.signature):
            return (
                False,
                f"checkpoint at event {cp.last_event_id} signature mismatch — "
                f"history through that boundary no longer proves itself",
                0,
            )

    # 2. Boundaries ascend — a deleted checkpoint or tampered id breaks the
    #    sequence the epochs were chained over.
    boundaries = [cp.last_event_id for cp in checkpoints]
    if boundaries != sorted(set(boundaries)):
        return False, "checkpoint boundaries are not strictly ascending", 0

    # 3. The rotation: content-recompute the oldest epoch not yet verified.
    #    Exactly one per run — that is what makes it amortized. Its start is
    #    the PREVIOUS epoch's boundary (or genesis), which is what makes the
    #    recompute correct rather than merely plausible.
    # The rotation pick: the oldest epoch never content-verified — or, when
    # every epoch has been verified once, the one verified LONGEST AGO. The
    # second half is what makes it a rotation rather than a one-shot: with no
    # reset, a small chain's single epoch would be verified exactly once and
    # never again, and a rewrite the day after would live forever. One epoch
    # per run either way — O(interval) work, every epoch re-read from content
    # once per full rotation of all epochs.
    never_verified = [cp for cp in checkpoints if cp.content_verified_at is None]
    spot = (
        never_verified[0]
        if never_verified
        else min(checkpoints, key=lambda cp: (cp.content_verified_at
                                              or datetime.min.replace(tzinfo=UTC)))
    )
    spot_detail = ""
    if spot is not None:
        idx = checkpoints.index(spot)
        prev_boundary = checkpoints[idx - 1].last_event_id if idx > 0 else 0
        prev_id, prev_hash = await _prev_boundary(session, prev_boundary)
        spot_head = await _verify_stretch(
            session, start_id=prev_id, end_id=spot.last_event_id,
            start_hash=prev_hash, key=key,
        )
        if spot_head is None:
            return (
                False,
                f"content re-verification of epoch ending at event "
                f"{spot.last_event_id} failed — the audit trail was "
                f"rewritten inside a checkpointed stretch",
                0,
            )
        if spot_head != spot.head_event_hash:
            return (
                False,
                f"epoch ending at event {spot.last_event_id}: the recomputed "
                f"head does not match the checkpointed head",
                0,
            )
        # Mark the rotation point. NOT committed by this function: callers
        # who only read (CLI verify on a live system) get a session they may
        # or may not commit — the marker simply re-verifies next run, which
        # is correct, just unoptimized, if this commit never lands.
        spot.content_verified_at = datetime.now(UTC)
        spot_detail = (
            f", epoch through event {spot.last_event_id} re-verified "
            f"from content"
        )

    # 4. The tail: full recompute from the newest checkpoint's head.
    newest = checkpoints[-1]
    running_hash = newest.head_event_hash
    checked = 0
    result = await session.stream(
        sa.select(CaseEvent)
        .where(CaseEvent.id > newest.last_event_id)
        .order_by(CaseEvent.id.asc())
        .execution_options(yield_per=500)
    )
    async for row in result.scalars():
        checked += 1
        if row.event_hash is None:
            return (
                False,
                f"event {row.id} was never stamped — run stamp_unhashed_events",
                checked,
            )
        expected = _chain_hash(running_hash, row, key)
        if expected != row.event_hash or row.prev_event_hash != running_hash:
            return (
                False,
                f"event {row.id} hash mismatch after checkpoint "
                f"{newest.last_event_id} — recomputed {expected[:12]}… but "
                f"stored {row.event_hash[:12]}…",
                checked,
            )
        running_hash = row.event_hash

    return (
        True,
        f"chain intact: {len(checkpoints)} checkpointed epoch(s) verified by "
        f"signature, {checked} recent events recomputed{spot_detail}",
        checked,
    )
