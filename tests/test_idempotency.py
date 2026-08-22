"""
Webhook deduplication — the double-charge guard at the front door.

Razorpay re-sends on any non-2xx and on its own retry schedule, so the same
payment.failed can arrive several times. Every one of those that gets through
opens the pipeline again, and the only thing standing between a re-delivery and
a second recovery attempt is this table.

The race test is the one that matters: the fast-path SELECT cannot close the
window between "not found" and "inserted", so the UNIQUE constraint has to be
what actually decides, and the loser has to come back as a clean duplicate
rather than an exception.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.ingestion.idempotency import is_duplicate_event
from src.models import ProcessedEvent

EVENT = "payment.failed_pay_dedupe_001_1700000000"


async def _count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count(ProcessedEvent.id)))).scalar_one())


async def test_a_new_event_is_not_a_duplicate_and_gets_recorded(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        assert await is_duplicate_event(session, EVENT) is False
        await session.commit()
        assert await _count(session) == 1


async def test_the_same_event_twice_is_a_duplicate(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        assert await is_duplicate_event(session, EVENT) is False
        await session.commit()
        # Fast path: the row is now visible to the SELECT.
        assert await is_duplicate_event(session, EVENT) is True
        assert await _count(session) == 1


async def test_a_duplicate_across_sessions_is_still_a_duplicate(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Razorpay's re-delivery arrives on a different request, hence a different session."""
    async with db_sessionmaker() as first:
        assert await is_duplicate_event(first, EVENT) is False
        await first.commit()

    async with db_sessionmaker() as second:
        assert await is_duplicate_event(second, EVENT) is True
        assert await _count(second) == 1


async def test_the_constraint_decides_when_the_fast_path_misses(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    The actual race. Two deliveries both run the SELECT before either commits,
    so both see "new". The UNIQUE constraint is what breaks the tie, and the
    loser must report a duplicate rather than raise.

    Staged deterministically: session B does its lookup first (sees nothing),
    then A commits underneath it, then B tries to insert.
    """
    async with db_sessionmaker() as b:
        # B looks and sees nothing.
        assert (
            await b.execute(
                select(ProcessedEvent.id).where(ProcessedEvent.razorpay_event_id == EVENT)
            )
        ).scalar_one_or_none() is None

        # A wins the race and commits.
        async with db_sessionmaker() as a:
            assert await is_duplicate_event(a, EVENT) is False
            await a.commit()

        # B now inserts into a table that already holds the row.
        assert await is_duplicate_event(b, EVENT) is True

    async with db_sessionmaker() as reader:
        assert await _count(reader) == 1, "the race produced two rows"


async def test_different_events_do_not_collide(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        assert await is_duplicate_event(session, "evt_a") is False
        assert await is_duplicate_event(session, "evt_b") is False
        await session.commit()
        assert await _count(session) == 2
