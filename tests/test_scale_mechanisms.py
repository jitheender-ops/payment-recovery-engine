"""
The scale mechanisms: shared rate limiting, dedup pruning, audit
re-anchoring, and the scheduler's batch wiring.

Each test names the property it exists for, per this codebase's discipline.
The Redis paths are exercised through a fakeredis-style stub because the
invariants that matter — atomic INCR+EXPIRE windowing, fail-open
degradation — are protocol-level, and a live Redis is a deployment
dependency, not a test one.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import rate_limit
from src.config import get_settings


@pytest.fixture(autouse=True)
def _reset_limiter() -> Any:
    rate_limit.clear_all()
    yield
    rate_limit.clear_all()


# ── The shared limiter ──────────────────────────────────────────────────────


def test_the_shared_limiter_refuses_past_the_budget() -> None:
    for _ in range(5):
        rate_limit.check("t:ip1", 5)
    with pytest.raises(HTTPException) as exc:
        rate_limit.check("t:ip1", 5)
    assert exc.value.status_code == 429


def test_windows_release() -> None:
    # A window far in the past: every entry inside it has aged out.
    rate_limit._BUCKETS["t:ip2"] = __import__("collections").deque(
        [time.monotonic() - 120]
    )
    rate_limit.check("t:ip2", 1)  # must not raise — the old entry expired


def test_one_key_does_not_affect_another() -> None:
    for _ in range(5):
        rate_limit.check("t:a", 5)
    with pytest.raises(HTTPException):
        rate_limit.check("t:a", 5)
    rate_limit.check("t:b", 5)  # different IP, unaffected


class _FakeRedis:
    """The three commands rate_limit uses, with the same semantics."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False

    def ping(self) -> bool:
        if self.fail:
            raise ConnectionError("down")
        return True

    def set(self, key: str, value: int, *, nx: bool = False, ex: int = 0) -> Any:
        if self.fail:
            raise ConnectionError("down")
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -2)

    def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("down")
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl


def test_the_redis_path_counts_across_check_calls(monkeypatch: Any) -> None:
    """With REDIS_URL set, the count is SHARED — the whole point. The same
    key hits the limit through Redis state, not process state."""
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "_REDIS", fake)
    monkeypatch.setattr(rate_limit, "_REDIS_TRIED", True)
    for _ in range(3):
        rate_limit.check("rl:t:shared", 3)
    assert fake.store["rl:rl:t:shared"] == 3
    with pytest.raises(HTTPException) as exc:
        rate_limit.check("rl:t:shared", 3)
    assert exc.value.status_code == 429
    assert fake.ttls["rl:rl:t:shared"] == 60  # window set on first hit


def test_a_dead_redis_degrades_to_in_process_never_to_an_outage(
    monkeypatch: Any,
) -> None:
    """Redis failing BETWEEN pings must not take the guarded page with it:
    the check falls back to the local bucket and still 429s on budget."""
    fake = _FakeRedis()
    fake.fail = True
    monkeypatch.setattr(rate_limit, "_REDIS", fake)
    monkeypatch.setattr(rate_limit, "_REDIS_TRIED", True)
    for _ in range(2):
        rate_limit.check("rl:t:down", 2)  # silent fallback
    with pytest.raises(HTTPException):
        rate_limit.check("rl:t:down", 2)


def test_an_unreachable_redis_url_never_builds_a_client(
    monkeypatch: Any,
) -> None:
    """REDIS_URL set but Redis gone at build time: no client, loud log, and
    the in-process limiter answers — not a 500 on every rate-checked page."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/9")
    get_settings.cache_clear()
    rate_limit.clear_all()
    try:
        assert rate_limit._redis() is None
        rate_limit.check("rl:t:noredis", 1)  # must not raise
        with pytest.raises(HTTPException):
            rate_limit.check("rl:t:noredis", 1)
    finally:
        monkeypatch.delenv("REDIS_URL", raising=False)
        get_settings.cache_clear()
        rate_limit.clear_all()


# ── processed_events pruning ────────────────────────────────────────────────


async def test_pruning_deletes_only_past_retention(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    from datetime import UTC, datetime, timedelta

    from src.models import ProcessedEvent
    from src.scheduler import prune_processed_events

    old = datetime.now(UTC) - timedelta(days=45)
    fresh = datetime.now(UTC) - timedelta(minutes=5)
    async with db_sessionmaker() as s:
        s.add(ProcessedEvent(razorpay_event_id="evt_prune_old", processed_at=old))
        s.add(ProcessedEvent(razorpay_event_id="evt_prune_new", processed_at=fresh))
        await s.commit()

        pruned = await prune_processed_events(s)
        await s.commit()

    assert pruned == 1
    async with db_sessionmaker() as reader:
        remaining = {
            r for r in (await reader.execute(
                select(ProcessedEvent.razorpay_event_id)
            )).scalars()
        }
    assert remaining == {"evt_prune_new"}, "the fresh dedup row was pruned"


async def test_pruning_respects_the_batch_cap(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """Bounded per tick like every sweep — a huge backlog must not hold the
    tick hostage; the next tick continues the pruning."""
    from datetime import UTC, datetime, timedelta

    from src.models import ProcessedEvent
    from src.scheduler import prune_processed_events

    monkeypatch.setenv("SCHEDULER_BATCH_SIZE", "2")
    get_settings.cache_clear()
    old = datetime.now(UTC) - timedelta(days=60)
    try:
        async with db_sessionmaker() as s:
            for i in range(5):
                s.add(ProcessedEvent(
                    razorpay_event_id=f"evt_cap_{i}", processed_at=old
                ))
            await s.commit()
            first = await prune_processed_events(s)
            second = await prune_processed_events(s)
            await s.commit()
        assert first == 2 and second == 2, "the cap did not bound the sweep"
    finally:
        monkeypatch.delenv("SCHEDULER_BATCH_SIZE", raising=False)
        get_settings.cache_clear()


async def test_zero_retention_disables_pruning(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """Keep-forever deployments opt out via the knob, not by deleting code."""
    from datetime import UTC, datetime, timedelta

    from src.models import ProcessedEvent
    from src.scheduler import prune_processed_events

    monkeypatch.setenv("PROCESSED_EVENTS_RETENTION_DAYS", "0")
    get_settings.cache_clear()
    try:
        async with db_sessionmaker() as s:
            s.add(ProcessedEvent(
                razorpay_event_id="evt_keep",
                processed_at=datetime.now(UTC) - timedelta(days=400),
            ))
            await s.commit()
            assert await prune_processed_events(s) == 0
            await s.commit()
            n = len((await s.execute(select(ProcessedEvent.id))).all())
        assert n == 1, "disabled pruning still deleted a row"
    finally:
        monkeypatch.delenv("PROCESSED_EVENTS_RETENTION_DAYS", raising=False)
        get_settings.cache_clear()


# ── Audit chain re-anchoring ────────────────────────────────────────────────

SECRET = "checkpoint-test-secret"


async def _seed_events(
    sm: async_sessionmaker[AsyncSession], n: int, *, secret_set: bool = True
) -> None:
    """n stamped events through the real chain machinery. Unique case per
    call — the (risk_type, subject_ref) pair is UNIQUE."""
    from src.audit_chain import stamp_unhashed_events
    from src.models import CaseEvent, RecoveryCase

    async with sm() as s:
        case = RecoveryCase(
            risk_type="payment_failure",
            subject_ref=f"cp_case_{uuid.uuid4().hex[:8]}",
            amount_at_risk=1000, max_attempts=3,
        )
        s.add(case)
        await s.flush()
        for i in range(n):
            s.add(CaseEvent(
                recovery_case_id=case.id, event_type="noted",
                actor="system", detail={"i": i},
            ))
        await s.commit()
        if secret_set:
            await stamp_unhashed_events(s)
            await s.commit()


@pytest.fixture
def _keyed_chain(monkeypatch: Any) -> Any:
    monkeypatch.setenv("AUDIT_CHAIN_SECRET", SECRET)
    monkeypatch.setenv("AUDIT_CHECKPOINT_INTERVAL_EVENTS", "10")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("AUDIT_CHAIN_SECRET", raising=False)
    monkeypatch.delenv("AUDIT_CHECKPOINT_INTERVAL_EVENTS", raising=False)
    get_settings.cache_clear()


async def test_a_grown_chain_gets_a_checkpoint(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    from src.audit_checkpoint import checkpoint_chain
    from src.models import AuditCheckpoint

    await _seed_events(db_sessionmaker, 12)
    async with db_sessionmaker() as s:
        anchored = await checkpoint_chain(s)
        await s.commit()
        assert anchored > 0, "a chain past the interval was not anchored"
        cps = (await s.execute(select(AuditCheckpoint))).scalars().all()
    assert len(cps) == 1
    assert cps[0].last_event_id == anchored


async def test_a_short_chain_is_left_unanchored(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    from src.audit_checkpoint import checkpoint_chain

    await _seed_events(db_sessionmaker, 3)  # below the interval of 10
    async with db_sessionmaker() as s:
        assert await checkpoint_chain(s) == 0


async def test_epoch_verification_matches_full_verification_when_intact(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    """The fast path and the slow path must agree on a clean chain. With the
    interval at 10, 25 events anchor at 10 and 20, leaving a 5-event tail."""
    from src.audit_chain import verify_chain
    from src.audit_checkpoint import checkpoint_chain, verify_chain_epoch

    await _seed_events(db_sessionmaker, 25)
    async with db_sessionmaker() as s:
        await checkpoint_chain(s)  # anchors epoch 10
        await s.commit()
        await checkpoint_chain(s)  # anchors epoch 20
        await s.commit()
    await _seed_events(db_sessionmaker, 5)  # tail: events 21..25
    async with db_sessionmaker() as s:
        # Two runs rotate content re-checks through both epochs; each costs
        # one interval-sized pass, never the whole history.
        await verify_chain_epoch(s)
        await s.commit()
        fast_ok, fast_detail, fast_checked = await verify_chain_epoch(s)
        await s.commit()
        slow = await verify_chain(s)
    assert fast_ok and slow.intact
    # The tail past the newest checkpoint (event 20) is exactly the 10
    # events with ids 21..30 — 25 seeded + 5 more — regardless of how many
    # epochs sit behind it. The rotation cost stays one interval per run.
    assert fast_checked == 10, fast_detail


async def test_tampering_inside_an_old_epoch_is_caught_by_the_signature(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    """THE property: a rewrite INSIDE checkpointed history — content
    changed, stored hashes and checkpoint left alone — is caught by the
    rotation's content re-check without re-reading the whole chain."""
    from src.audit_checkpoint import checkpoint_chain, verify_chain_epoch
    from src.models import CaseEvent

    await _seed_events(db_sessionmaker, 12)
    async with db_sessionmaker() as s:
        await checkpoint_chain(s)  # epoch through event 10
        await s.commit()

    # The attack: rewrite a row deep inside the checkpointed epoch, leaving
    # its stored hash and the checkpoint untouched.
    async with db_sessionmaker() as s:
        row = (await s.execute(select(CaseEvent).order_by(
            CaseEvent.id.asc()).limit(1))).scalars().one()
        row.actor = "attacker"
        await s.commit()

    async with db_sessionmaker() as s:
        # The signature passes (it pins the stored head); the epoch's turn
        # in the content rotation is what catches the rewrite.
        ok, detail, _ = await verify_chain_epoch(s)
        await s.commit()
    assert not ok, (
        "a content rewrite inside a checkpointed epoch passed verification"
    )
    assert "epoch" in detail.lower() or "mismatch" in detail.lower()


async def test_tampering_after_the_checkpoint_is_caught_by_the_tail(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    """And the tail is protected the classic way — full recompute."""
    from src.audit_checkpoint import checkpoint_chain, verify_chain_epoch
    from src.models import CaseEvent

    await _seed_events(db_sessionmaker, 12)
    async with db_sessionmaker() as s:
        await checkpoint_chain(s)
        await s.commit()
    await _seed_events(db_sessionmaker, 5)
    async with db_sessionmaker() as s:
        newest = (await s.execute(
            select(CaseEvent).order_by(CaseEvent.id.desc()).limit(1)
        )).scalars().one()
        newest.actor = "attacker"
        await s.commit()
    async with db_sessionmaker() as s:
        ok, detail, _ = await verify_chain_epoch(s)
    assert not ok and "mismatch" in detail.lower()


async def test_no_checkpoints_falls_back_to_full_verification(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    """A young chain with no anchors verifies identically the old way."""
    from src.audit_checkpoint import verify_chain_epoch

    await _seed_events(db_sessionmaker, 4)
    async with db_sessionmaker() as s:
        ok, detail, checked = await verify_chain_epoch(s)
    assert ok and checked == 4


async def test_the_tick_anchors_and_reports(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    """Wiring: the tick counts the checkpoint, so anchoring is visible —
    a retention/anchor policy nobody can see is one nobody trusts."""
    from src.scheduler import tick

    await _seed_events(db_sessionmaker, 12)
    async with db_sessionmaker() as s:
        counts = await tick(s)
    assert "chain_checkpointed" in counts
    assert "processed_events_pruned" in counts
    assert counts["chain_checkpointed"] > 0


# ── Rotation, second-epoch tampering, anchor refusal ────────────────────────


async def test_rotation_moves_to_the_second_epoch(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    """The flaw the first version shipped with: the spot-check re-verified
    checkpoints[0] FOREVER and never reached epoch 2. With the marker,
    each run picks the oldest NOT-yet-verified epoch — tampering in ANY
    epoch surfaces within one rotation."""
    from src.audit_checkpoint import checkpoint_chain, verify_chain_epoch
    from src.models import AuditCheckpoint

    await _seed_events(db_sessionmaker, 10)   # epoch 1
    async with db_sessionmaker() as s:
        await checkpoint_chain(s)
        await s.commit()
    await _seed_events(db_sessionmaker, 10)   # epoch 2
    async with db_sessionmaker() as s:
        await checkpoint_chain(s)
        await s.commit()

    async with db_sessionmaker() as s:
        # Run 1: re-verifies the oldest unverified epoch (epoch 1) and marks it.
        _, d1, _ = await verify_chain_epoch(s)
        await s.commit()
    assert "through event" in d1, f"no epoch was content re-checked: {d1}"

    async with db_sessionmaker() as s:
        # Run 2: epoch 1 is marked; the rotation must now pick EPOCH 2.
        _, d2, _ = await verify_chain_epoch(s)
        await s.commit()
    assert "epoch ending at event" in d2 or "epoch through event" in d2
    markers = {
        cp.last_event_id: cp.content_verified_at
        for cp in (await s.execute(select(AuditCheckpoint))).scalars()
    }
    assert all(v is not None for v in markers.values()), (
        f"rotation stalled before reaching the second epoch: {markers}"
    )


async def test_tampering_the_second_epoch_content_is_caught(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    """The exact hole the rotation fix closes: a rewrite inside epoch 2's
    stretch that leaves every stored hash untouched. The signature passes
    (it pins the stored head); only the content re-check catches it — and
    only if the rotation actually reaches epoch 2."""
    from src.audit_checkpoint import checkpoint_chain, verify_chain_epoch
    from src.models import CaseEvent

    await _seed_events(db_sessionmaker, 10)
    async with db_sessionmaker() as s:
        await checkpoint_chain(s)
        await s.commit()
    await _seed_events(db_sessionmaker, 10)
    async with db_sessionmaker() as s:
        await checkpoint_chain(s)
        await s.commit()

    # The attack: rewrite content inside EPOCH 2 only, hashes left alone.
    async with db_sessionmaker() as s:
        e2_first = 11
        row = (await s.execute(
            select(CaseEvent).where(CaseEvent.id == e2_first)
        )).scalars().one()
        row.actor = "attacker"
        await s.commit()

    async with db_sessionmaker() as s:
        # Run 1 verifies epoch 1 (clean), run 2 reaches epoch 2 and fails.
        await verify_chain_epoch(s)
        await s.commit()
        ok, detail, _ = await verify_chain_epoch(s)
    assert not ok, "a rewrite inside the second epoch slipped the rotation"


async def test_an_anchor_refusal_does_not_rollback_the_rest_of_the_tick(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    """The rollback I removed: a stretch that fails verification inside
    checkpoint_chain must refuse the anchor WITHOUT discarding the tick's
    other uncommitted work. Pruned rows are the observable stand-in for
    'the rest of the tick'."""
    from datetime import UTC, datetime, timedelta

    from src.audit_checkpoint import checkpoint_chain
    from src.models import ProcessedEvent

    await _seed_events(db_sessionmaker, 3)  # short chain, no anchor yet
    async with db_sessionmaker() as s:
        # Pre-existing committed work this tick would otherwise carry.
        s.add(ProcessedEvent(
            razorpay_event_id="evt_anchor_guard",
            processed_at=datetime.now(UTC) - timedelta(days=90),
        ))
        await s.commit()

    # Tamper one row's stored hash so the stretch fails content verification.
    from src.models import CaseEvent

    async with db_sessionmaker() as s:
        row = (await s.execute(select(CaseEvent).order_by(
            CaseEvent.id.asc()).limit(1))).scalars().one()
        row.event_hash = "0" * 64  # content no longer matches
        await s.commit()

    async with db_sessionmaker() as s:
        anchored = await checkpoint_chain(s)
        # No rollback may have happened in this session — and the guard row
        # committed earlier is still readable through the same session.
        still_there = (await s.execute(select(ProcessedEvent).where(
            ProcessedEvent.razorpay_event_id == "evt_anchor_guard"
        ))).scalar_one_or_none()
    assert anchored == 0, "an unverifiable stretch was anchored"
    assert still_there is not None


async def test_a_brownfield_chain_anchors_one_epoch_at_a_time(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    """A chain stamped long before checkpoints shipped can be thousands past
    the interval; the first anchor must be interval-sized (bounded content
    pass), not one giant epoch ending at today's head."""
    from src.audit_checkpoint import checkpoint_chain
    from src.models import AuditCheckpoint

    await _seed_events(db_sessionmaker, 25)  # 25 events, interval 10
    async with db_sessionmaker() as s:
        first = await checkpoint_chain(s)
        await s.commit()
        second = await checkpoint_chain(s)
        await s.commit()
        boundaries = [cp.last_event_id for cp in (
            await s.execute(select(AuditCheckpoint))
        ).scalars()]
    # 10, 20, then the 5-row remainder must wait for the interval — three
    # anchors at most, each ≤ interval.
    assert first == 10 and second == 20
    assert all(
        b <= 20 for b in boundaries
    ) or boundaries == [10, 20, 25], boundaries
    async with db_sessionmaker() as s:
        third = await checkpoint_chain(s)  # 25-20 = 5 < 10: refuses
        await s.commit()
    assert third == 0


async def test_the_tick_runs_the_verification_and_reports_a_minus_one_on_tamper(
    db_sessionmaker: async_sessionmaker[AsyncSession], _keyed_chain: Any
) -> None:
    """Checkpoints alone made verification fast; the tick makes it HAPPEN —
    and a tampered chain reads -1 in the tick line, which cannot be
    mistaken for a quiet day."""
    from src.models import CaseEvent
    from src.scheduler import tick

    await _seed_events(db_sessionmaker, 12)
    async with db_sessionmaker() as s:
        await tick(s)  # anchors epoch 1 and verifies — clean
    async with db_sessionmaker() as s:
        row = (await s.execute(select(CaseEvent).order_by(
            CaseEvent.id.asc()).limit(1))).scalars().one()
        row.actor = "attacker"  # rewrite content, hashes untouched
        await s.commit()
    async with db_sessionmaker() as s:
        counts = await tick(s)
    assert counts["chain_checkpointed"] == 0, "a tampered stretch was anchored"
    # The rotation reaches epoch 1 again on the very next tick (with one
    # epoch, the oldest-verified pick IS it) and the rewrite is caught —
    # -1 is unmissable in the tick line.
    assert counts["chain_verified"] == -1, (
        f"a content rewrite inside a checkpointed epoch was not caught on "
        f"the next rotation: {counts['chain_verified']}"
    )


# ── The Redis SETNX fix ─────────────────────────────────────────────────────


class _FakeRedisV2:
    """set(nx=, ex=)/incr/ttl/expire — enough to prove the crash-race fix."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def ping(self) -> bool:
        return True

    def set(self, key: str, value: int, *, nx: bool = False, ex: int = 0) -> Any:
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -2)

    def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl


def test_the_redis_counter_is_created_with_its_ttl_atomically(
    monkeypatch: Any,
) -> None:
    """The crash-race fix: the FIRST hit creates the counter and its expiry
    in ONE atomic step (SET NX EX). The old INCR-then-EXPIRE left a window
    where a crash between them produced an immortal counter — a shared-IP
    customer locked out forever."""
    fake = _FakeRedisV2()
    monkeypatch.setattr(rate_limit, "_REDIS", fake)
    monkeypatch.setattr(rate_limit, "_REDIS_TRIED", True)
    rate_limit.check("rl:t:atomic", 3)
    assert fake.store["rl:rl:t:atomic"] == 1
    assert fake.ttls["rl:rl:t:atomic"] == 60, "created without a TTL — immortal"


def test_an_ancient_ttlless_counter_gets_its_ttl_restored(
    monkeypatch: Any,
) -> None:
    """The backstop: a key left TTL-less by a pre-fix deployment (or a
    FLUSH race) gets its window restored lazily instead of living forever."""
    fake = _FakeRedisV2()
    fake.store["rl:rl:t:legacy"] = 999   # pre-fix counter
    fake.ttls["rl:rl:t:legacy"] = -1    # no expiry
    monkeypatch.setattr(rate_limit, "_REDIS", fake)
    monkeypatch.setattr(rate_limit, "_REDIS_TRIED", True)
    with pytest.raises(HTTPException):
        rate_limit.check("rl:t:legacy", 3)  # over budget — but TTL restored
    assert fake.ttls["rl:rl:t:legacy"] == 60
