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
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import async_session_factory, get_session
from src.ingestion.idempotency import is_duplicate_event
from src.ingestion.signature import verify_webhook_signature
from src.models import WebhookEvent

logger = logging.getLogger(__name__)
router = APIRouter()


async def _process_event_background(event_id: str, event_type: str, payload: dict) -> None:
    """Background task: process the webhook event through the recovery pipeline."""
    async with async_session_factory() as session:
        try:
            if event_type == "payment.failed":
                from src.orchestrator import process_payment_failure

                # Retrieve the stored event
                from sqlalchemy import select
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
                # Mark any pending retry attempts for this payment as no longer needed
                payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
                payment_id = payment_entity.get("id")
                if payment_id:
                    from sqlalchemy import update
                    from src.models import RetryAttempt
                    await session.execute(
                        update(RetryAttempt)
                        .where(
                            RetryAttempt.payment_id == payment_id,
                            RetryAttempt.result == "pending",
                        )
                        .values(result="superseded", executed_at=datetime.now(timezone.utc))
                    )
                    await session.commit()
                    logger.info(
                        "Payment captured — marked pending retries as superseded: %s",
                        payment_id,
                    )

        except Exception:
            logger.exception("Error processing background event: %s", event_id)
            # Mark event as failed
            try:
                from sqlalchemy import update
                await session.execute(
                    update(WebhookEvent)
                    .where(WebhookEvent.razorpay_event_id == event_id)
                    .values(processed=True, processing_error="Background processing failed")
                )
                await session.commit()
            except Exception:
                logger.exception("Failed to update event error status")


@router.post("/razorpay")
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
    raw_body = await request.body()

    # 2. Verify signature
    if not x_razorpay_signature:
        logger.warning("Webhook received without X-Razorpay-Signature header")
        return Response(status_code=401, content="Missing signature")

    if not verify_webhook_signature(raw_body, x_razorpay_signature, settings.razorpay_webhook_secret):
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
        received_at=datetime.now(timezone.utc),
        processed=False,
    )
    session.add(webhook_event)
    await session.flush()  # get the ID assigned

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
