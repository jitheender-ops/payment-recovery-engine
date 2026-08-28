"""
FastAPI router for Razorpay webhook ingestion.

Handles payment.failed and payment.captured events:
1. Verify HMAC-SHA256 signature
2. Check idempotency (dedup)
3. Store event in append-only event store
4. Trigger async processing via background task
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings, reveal
from src.database import async_session_factory, get_session
from src.ingestion.idempotency import is_duplicate_event
from src.ingestion.signature import body_too_large, verify_webhook_signature
from src.models import WebhookEvent

logger = logging.getLogger(__name__)
router = APIRouter()

# How many times an event survives a processing failure before it rests with
# the error recorded. Shared with scheduler.reconcile_events — the same cap
# governs an event whether it dies on the first pass or on a sweep, so the
# constant lives here and the scheduler imports it. One number, one meaning.
EVENT_RECONCILE_MAX_ATTEMPTS = 3


async def attribute_captured_payload(
    session: AsyncSession, payload: dict[str, Any]
) -> Any:
    """
    Attribute a payment.captured payload to the case that earned it.

    Shared by the first-pass background task and scheduler.reconcile_events:
    a capture whose first processing attempt failed must be retriable by the
    sweep, and two copies of this resolution order would drift.
    """
    from src.cases import attribute_capture

    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    payment_id = payment_entity.get("id")
    if not payment_id:
        return None
    # No default. `.get("amount", 0)` turned a payload with no amount into a
    # zero-rupee capture, which attribute_capture then had to reason about —
    # a missing field is missing, not zero.
    amount = payment_entity.get("amount")
    if amount is None:
        logger.warning(
            "Captured payload for %s carries no amount — refusing", payment_id
        )
        return None
    notes = payment_entity.get("notes") or {}
    link_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    return await attribute_capture(
        session,
        amount=amount,
        recovered_ref=payment_id,
        # Razorpay copies a Payment Link's notes onto the payment, so the
        # idempotency key written at link creation comes back here. It is the
        # stronger of the two keys: ours, deterministic, and UNIQUE in
        # retry_attempts.
        link_id=link_entity.get("id"),
        idempotency_key=notes.get("retry_idempotency_key"),
        order_ref=payment_entity.get("order_id"),
    )


async def rearm_failed_event(session: AsyncSession, event_id: str) -> None:
    """
    Give a failed event another chance instead of burying it.

    Marking a failed event processed=True made it invisible to
    reconcile_events — a transient database blip on the first pass permanently
    dropped a real payment failure, and (worse) a capture: money that arrived
    and was never attributed, on a case that kept chasing a customer who had
    already paid. Razorpay will not re-send after our 200, so the database is
    the only retry mechanism there is.

    The event is therefore RE-ARMED (processed=False) until it has failed
    EVENT_RECONCILE_MAX_ATTEMPTS times; only then does it rest with the error
    recorded. A deterministically-broken payload still stops eating sweep
    batches, but a bad minute no longer loses money.
    """
    from sqlalchemy import select, update

    try:
        result = await session.execute(
            select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event_id)
        )
        event = result.scalar_one_or_none()
        if event is None:
            return
        attempts = (event.processing_attempts or 0) + 1
        if attempts < EVENT_RECONCILE_MAX_ATTEMPTS:
            await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.id == event.id)
                .values(
                    processed=False,
                    processing_attempts=attempts,
                    processing_error=(
                        f"Background processing failed (attempt {attempts}); re-armed"
                    ),
                )
            )
            logger.warning(
                "Event %s re-armed after failure (%d/%d)",
                event_id, attempts, EVENT_RECONCILE_MAX_ATTEMPTS,
            )
        else:
            await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.id == event.id)
                .values(
                    processed=True,
                    processing_attempts=attempts,
                    processing_error="Background processing failed after retry cap",
                )
            )
            logger.error(
                "Event %s permanently failed after %d attempts", event_id, attempts
            )
        await session.commit()
    except Exception:
        logger.exception("Failed to re-arm event %s", event_id)


async def _process_event_background(
    event_id: str, event_type: str, payload: dict[str, Any]
) -> None:
    """Background task: process the webhook event through the recovery pipeline."""
    async with async_session_factory() as session:
        try:
            if event_type == "payment.failed":
                # Retrieve the stored event
                from sqlalchemy import select

                from src.orchestrator import process_payment_failure
                result = await session.execute(
                    select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event_id)
                )
                event = result.scalar_one_or_none()
                if event:
                    await process_payment_failure(event, session)
                    event.processed = True
                    await session.commit()
                    logger.info("Successfully processed payment.failed event: %s", event_id)
                else:
                    logger.error("Event not found in store: %s", event_id)

            elif event_type == "payment.captured":
                # Revenue attribution. The captured payment is a payment we have
                # never seen: recovery goes out as a Payment Link and paying it
                # mints a new id. The old code compared that new id against
                # RetryAttempt.payment_id, which holds the ORIGINAL failed id, so
                # the update matched nothing, pending rows never resolved, and no
                # rupee was ever creditable to the engine.
                case = await attribute_captured_payload(session, payload)
                # Mark processed whether or not a case matched: most captures
                # are payments that never failed (the common path), and they
                # must not be re-attributed on every scheduler tick. A match
                # failure is a no-op, not an error.
                from sqlalchemy import update
                await session.execute(
                    update(WebhookEvent)
                    .where(WebhookEvent.razorpay_event_id == event_id)
                    .values(processed=True)
                )
                await session.commit()
                if case is not None:
                    logger.info(
                        "Capture attributed: payment=%s case=%s state=%s recovered=%s",
                        payload.get("payload", {})
                        .get("payment", {}).get("entity", {}).get("id"),
                        case.id,
                        case.state,
                        case.amount_recovered,
                    )

        except Exception:
            logger.exception("Error processing background event: %s", event_id)
            # Re-arm, not bury: the sweep retries it until the cap (see
            # rearm_failed_event). The old code set processed=True here, which
            # hid the event from reconcile_events forever — one transient
            # failure was enough to lose a payment failure or a capture.
            await session.rollback()
            await rearm_failed_event(session, event_id)


@router.post(
    "/razorpay",
    responses={
        200: {"description": "Accepted (or duplicate already processed)"},
        400: {"description": "Body is not valid JSON"},
        413: {"description": "Body exceeds the size cap"},
        401: {"description": "Missing or invalid X-Razorpay-Signature — fail closed"},
    },
)
async def receive_razorpay_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    x_razorpay_signature: str | None = Header(default=None),
) -> Response:
    """
    Receive and process Razorpay webhooks.

    Flow: verify signature → check idempotency → store event → trigger background processing.
    Returns 200 OK immediately; actual processing happens asynchronously.
    """
    settings = get_settings()

    # 1. Read raw body (MUST be raw bytes for signature verification)
    if body_too_large(request.headers.get("content-length")):
        logger.warning("Webhook body exceeds the size cap — rejecting unread")
        return Response(status_code=413, content="Payload too large")
    raw_body = await request.body()
    if body_too_large(None, raw_body):
        logger.warning("Webhook body exceeds the size cap — rejecting")
        return Response(status_code=413, content="Payload too large")

    # 2. Verify signature
    if not x_razorpay_signature:
        logger.warning("Webhook received without X-Razorpay-Signature header")
        return Response(status_code=401, content="Missing signature")

    if not verify_webhook_signature(
        raw_body, x_razorpay_signature, reveal(settings.razorpay_webhook_secret)
    ):
        logger.warning("Webhook signature verification failed")
        return Response(status_code=401, content="Invalid signature")

    # 3. Parse payload
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook payload")
        return Response(status_code=400, content="Invalid JSON")

    event_type = payload.get("event", "unknown")
    # Razorpay uses the payment ID as a quasi-event-ID; construct a unique one
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    payment_id = payment_entity.get("id", "unknown")
    event_id = f"{event_type}_{payment_id}_{payload.get('created_at', 0)}"

    logger.info(
        "Webhook received: type=%s, payment_id=%s, event_id=%s",
        event_type,
        payment_id,
        event_id,
    )

    # 4. Idempotency check
    if await is_duplicate_event(session, event_id):
        logger.info("Duplicate webhook skipped: %s", event_id)
        return Response(status_code=200, content="Already processed")

    # 5. Store in event log (append-only)
    webhook_event = WebhookEvent(
        razorpay_event_id=event_id,
        event_type=event_type,
        payload=payload,
        received_at=datetime.now(UTC),
        processed=False,
    )
    session.add(webhook_event)
    # COMMIT, not flush. The background task below opens its own session and
    # looks this row up by razorpay_event_id. A flush leaves the row invisible
    # outside this transaction, so the task raced the commit in get_session()'s
    # teardown and logged "Event not found in store" for every single webhook —
    # the event was persisted, then the entire recovery pipeline was skipped.
    # Committing here is also correct on its own terms: we are about to return
    # 200 to Razorpay, and we must not acknowledge an event we have not durably
    # stored, or a delivery we drop is never re-sent.
    await session.commit()
    await session.refresh(webhook_event)

    # 6. Trigger async processing
    if event_type in ("payment.failed", "payment.captured"):
        background_tasks.add_task(
            _process_event_background,
            event_id,
            event_type,
            payload,
        )
        logger.info("Background task queued for: %s", event_id)
    else:
        logger.info("Unhandled event type (stored but not processed): %s", event_type)

    return Response(status_code=200, content="OK")
