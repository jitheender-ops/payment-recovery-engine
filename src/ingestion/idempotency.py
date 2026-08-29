"""
Webhook idempotency — deduplication using the processed_events table.

Uses a unique constraint on razorpay_event_id with an insert-or-skip pattern.
No Redis needed at this scale.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import ProcessedEvent

logger = logging.getLogger(__name__)


async def is_duplicate_event(session: AsyncSession, event_id: str) -> bool:
    """
    Check whether a Razorpay event has already been processed.

    Attempts to insert the event_id into the processed_events table.
    If the insert succeeds, the event is new. If it fails due to the
    unique constraint, it is a duplicate — the constraint is the check,
    so no pre-SELECT is needed.

    Args:
        session: Async SQLAlchemy session.
        event_id: The Razorpay event to check.

    Returns:
        True if the event has already been processed (duplicate).
        False if this is a new event (successfully inserted).
    """
    try:
        new_record = ProcessedEvent(razorpay_event_id=event_id)
        session.add(new_record)
        await session.flush()
        logger.debug("New event recorded: %s", event_id)
        return False
    except IntegrityError:
        await session.rollback()
        logger.info("Duplicate event detected: %s", event_id)
        return True

    # Try to insert — handles race conditions via unique constraint
    try:
        new_record = ProcessedEvent(razorpay_event_id=event_id)
        session.add(new_record)
        await session.flush()
        logger.debug("New event recorded: %s", event_id)
        return False
    except IntegrityError:
        await session.rollback()
        logger.info("Duplicate event detected (constraint): %s", event_id)
        return True
