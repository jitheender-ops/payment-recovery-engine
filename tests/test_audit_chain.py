"""Tests for the case_events hash chain."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.audit_chain import stamp_unhashed_events, verify_chain
from src.models import CaseEvent


async def _add_event(
    session: AsyncSession, case_id: uuid.UUID, event_type: str, **detail: Any
) -> CaseEvent:
    row = CaseEvent(recovery_case_id=case_id, event_type=event_type, detail=detail or None)
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def session(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with db_sessionmaker() as s:
        yield s


async def test_stamping_an_empty_table_is_a_noop(session: AsyncSession) -> None:
    n = await stamp_unhashed_events(session)
    assert n == 0


async def test_stamps_every_unhashed_row_in_order(session: AsyncSession) -> None:
    case_id = uuid.uuid4()
    await _add_event(session, case_id, "opened")
    await _add_event(session, case_id, "contacted", channel="sms")
    await _add_event(session, case_id, "attributed", amount=500)
    await session.commit()

    n = await stamp_unhashed_events(session)
    assert n == 3

    rows = (
        await session.execute(sa.select(CaseEvent).order_by(CaseEvent.id))
    ).scalars().all()
    assert all(r.event_hash is not None for r in rows)
    # Each row's prev_event_hash is the one before it, chained.
    assert rows[0].prev_event_hash == "0" * 64
    assert rows[1].prev_event_hash == rows[0].event_hash
    assert rows[2].prev_event_hash == rows[1].event_hash


async def test_verify_passes_on_an_untouched_chain(session: AsyncSession) -> None:
    case_id = uuid.uuid4()
    await _add_event(session, case_id, "opened")
    await _add_event(session, case_id, "closed", reason="test")
    await session.commit()
    await stamp_unhashed_events(session)
    await session.commit()

    result = await verify_chain(session)
    assert result.intact is True
    assert result.events_checked == 2
    assert result.first_broken_id is None


async def test_verify_catches_a_tampered_detail_field(session: AsyncSession) -> None:
    """
    The whole point: an operator with raw database access edits `detail` on
    a row that was already chained. verify_chain must catch it, and name the
    row it happened at.
    """
    case_id = uuid.uuid4()
    await _add_event(session, case_id, "opened")
    row2 = await _add_event(session, case_id, "attributed", amount=500)
    await _add_event(session, case_id, "closed", reason="recovered")
    await session.commit()
    await stamp_unhashed_events(session)
    await session.commit()

    # Tamper: change what was recovered without touching the stored hash —
    # exactly what a direct SQL UPDATE against the table would do.
    row2.detail = {"amount": 999999}
    await session.commit()

    result = await verify_chain(session)
    assert result.intact is False
    assert result.first_broken_id == row2.id


async def test_stamping_is_idempotent_and_only_chains_new_rows(
    session: AsyncSession,
) -> None:
    case_id = uuid.uuid4()
    await _add_event(session, case_id, "opened")
    await session.commit()
    first = await stamp_unhashed_events(session)
    await session.commit()
    assert first == 1

    # Re-running with nothing new touches nothing.
    again = await stamp_unhashed_events(session)
    assert again == 0

    # A new event added later chains onto the existing tail, not from genesis.
    await _add_event(session, case_id, "contacted")
    await session.commit()
    second = await stamp_unhashed_events(session)
    await session.commit()
    assert second == 1

    result = await verify_chain(session)
    assert result.intact is True
    assert result.events_checked == 2
