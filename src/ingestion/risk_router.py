"""
FastAPI router for merchant-pushed risk events.

A card decline announces itself through Razorpay's webhook; an abandoned cart,
a halted subscription, an overdue invoice and a failed mandate debit only
exist in the merchant's own systems. The merchant POSTs them here:

    POST /risks
    X-Risk-Signature: <hmac-sha256 hex of the raw body, keyed by
                       RISK_WEBHOOK_SECRET>

    {
      "event_id": "evt_9f2c",                    // optional dedup key
      "risk_type": "checkout_abandonment",       // one of the four chased types
      "reference_id": "cart_123",                // id in the merchant's namespace
      "amount_paise": 249900,
      "currency": "INR",
      "customer_id": "cust_1",                   // optional
      "customer_email": "a@b.in",                // optional
      "customer_contact": "+919812345678",       // optional
      "occurred_at": "2026-08-27T10:00:00Z",     // optional, defaults to now
      "due_at": null,                            // invoice due date etc.
      "meta": {"cart_items": "2 books"}          // optional, untrusted
    }

Same discipline as the Razorpay webhook: verify HMAC → dedup → store in the
append-only risk_events table → COMMIT → process in a background task. A
failed task re-arms the event, and scheduler.reconcile_risk_events retries it
up to the shared cap — so a deploy between the 200 and the processing loses
nothing.

The endpoint is NOT behind require_api_key: like the Razorpay webhook, it
authenticates by HMAC over the raw body, because that is what a merchant's
outbound webhook client can actually produce. With RISK_WEBHOOK_SECRET unset
every event is rejected — the surface is closed until configured on purpose.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings, reveal
from src.database import async_session_factory, get_session
from src.ingestion.router import EVENT_RECONCILE_MAX_ATTEMPTS
from src.ingestion.signature import body_too_large, verify_webhook_signature
from src.models import RiskEvent

logger = logging.getLogger(__name__)
router = APIRouter()

# The four risk types the chasers hunt. payment_failure is deliberately not
# here — it arrives through Razorpay's webhook, and accepting it on this
# surface would let a caller bypass the gateway's classification.
ChasedRiskType = Literal[
    "checkout_abandonment",
    "subscription_failure",
    "invoice_overdue",
    "mandate_failure",
]


# Merchant identifiers travel a long way from here: into log lines, onto the
# recovery page as the visible reference, into CSV and audit exports, and into
# the agent's prompt. A control character in one of them forges log lines,
# splits a CSV row, and can pose as a prompt section. None of them has any
# legitimate use for one.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# What the meta map may carry. The prompt only ever renders 8 keys of 200
# chars (agent.prompts.sanitize_meta), so anything past this bound is dead
# weight that still costs JSONB storage on every event and re-stringifying on
# every chase — storage and prompt amplification from a single request.
_META_MAX_KEYS = 32
_META_MAX_CHARS = 4096


class RiskEventIn(BaseModel):
    """One revenue-at-risk event, as the merchant's systems report it."""

    event_id: str | None = Field(default=None, max_length=255)
    risk_type: ChasedRiskType
    reference_id: str = Field(min_length=1, max_length=255)
    # Upper bound is the Postgres INTEGER ceiling, not a product number: the
    # column stores paise in 32 bits, and an uncapped field would turn a ₹30Cr
    # invoice push into a 500 at insert instead of a clean 400 here. Anything
    # past the guardrail's amount ceiling is abandoned before a link exists
    # anyway; this just makes the storage boundary honest.
    amount_paise: int = Field(gt=0, le=2_147_483_647)
    # INR only, on purpose: the whole customer-facing stack — the ₹ display on
    # the recovery page, the UPI rail, the IST blackout, the amount ceiling in
    # paise — assumes rupees. Accepting another currency would mint a link in
    # it and then show the customer a ₹ figure, a lie on the money line.
    currency: str = Field(default="INR", pattern=r"^INR$")
    customer_id: str | None = Field(default=None, max_length=255)
    customer_email: str | None = Field(default=None, max_length=255)
    customer_contact: str | None = Field(default=None, max_length=20)
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id", "reference_id", "customer_id", "customer_contact")
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        """Reject identifiers carrying control characters — see _CONTROL_CHARS."""
        if value is not None and _CONTROL_CHARS.search(value):
            raise ValueError("control characters are not allowed in identifiers")
        return value

    @field_validator("meta")
    @classmethod
    def _bounded_meta(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Refuse a meta map past the bound the prompt could ever use."""
        if len(value) > _META_MAX_KEYS:
            raise ValueError(f"meta carries more than {_META_MAX_KEYS} keys")
        if len(json.dumps(value, default=str)) > _META_MAX_CHARS:
            raise ValueError(f"meta exceeds {_META_MAX_CHARS} characters")
        return value


async def rearm_failed_risk_event(session: AsyncSession, event_id: str) -> None:
    """
    Give a failed risk event another chance instead of burying it.

    Mirror of rearm_failed_event for webhook events — same cap, same logic:
    re-arm (processed=False) until EVENT_RECONCILE_MAX_ATTEMPTS, then rest
    with the error recorded. One number, one meaning, across both stores.
    """
    try:
        result = await session.execute(
            select(RiskEvent).where(RiskEvent.event_id == event_id)
        )
        event = result.scalar_one_or_none()
        if event is None:
            return
        attempts = (event.processing_attempts or 0) + 1
        if attempts < EVENT_RECONCILE_MAX_ATTEMPTS:
            await session.execute(
                update(RiskEvent)
                .where(RiskEvent.id == event.id)
                .values(
                    processed=False,
                    processing_attempts=attempts,
                    processing_error=(
                        f"Background processing failed (attempt {attempts}); re-armed"
                    ),
                )
            )
            logger.warning(
                "Risk event %s re-armed after failure (%d/%d)",
                event_id, attempts, EVENT_RECONCILE_MAX_ATTEMPTS,
            )
        else:
            await session.execute(
                update(RiskEvent)
                .where(RiskEvent.id == event.id)
                .values(
                    processed=True,
                    processing_attempts=attempts,
                    processing_error="Background processing failed after retry cap",
                )
            )
            logger.error(
                "Risk event %s permanently failed after %d attempts",
                event_id, attempts,
            )
        await session.commit()
    except Exception:
        logger.exception("Failed to re-arm risk event %s", event_id)


async def _process_risk_event_background(event_id: str) -> None:
    """Background task: open the case and run the first chase step."""
    async with async_session_factory() as session:
        try:
            from src.orchestrator import process_risk_event

            result = await session.execute(
                select(RiskEvent).where(RiskEvent.event_id == event_id)
            )
            event = result.scalar_one_or_none()
            if event is None:
                logger.error("Risk event not found in store: %s", event_id)
                return
            await process_risk_event(event, session)
            event.processed = True
            await session.commit()
            logger.info("Successfully processed risk event: %s", event_id)
        except Exception:
            logger.exception("Error processing risk event: %s", event_id)
            await session.rollback()
            await rearm_failed_risk_event(session, event_id)


@router.post(
    "",
    responses={
        200: {"description": "Accepted (or duplicate already received)"},
        400: {"description": "Body is not valid JSON or fails the schema"},
        413: {"description": "Body exceeds the size cap"},
        401: {"description": "Missing or invalid X-Risk-Signature — fail closed"},
    },
)
async def receive_risk_event(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    x_risk_signature: Annotated[str | None, Header()] = None,
) -> Response:
    """
    Receive a merchant-pushed risk event.

    Flow: verify signature → parse + validate → dedup → store → COMMIT →
    background processing. Returns 200 immediately; the case opens and the
    first chase step runs asynchronously.
    """
    settings = get_settings()

    if body_too_large(request.headers.get("content-length")):
        logger.warning("Risk event body exceeds the size cap — rejecting unread")
        return Response(status_code=413, content="Payload too large")
    raw_body = await request.body()
    if body_too_large(None, raw_body):
        logger.warning("Risk event body exceeds the size cap — rejecting")
        return Response(status_code=413, content="Payload too large")

    if not x_risk_signature:
        logger.warning("Risk event received without X-Risk-Signature header")
        return Response(status_code=401, content="Missing signature")

    if not verify_webhook_signature(
        raw_body, x_risk_signature, reveal(settings.risk_webhook_secret)
    ):
        logger.warning("Risk event signature verification failed")
        return Response(status_code=401, content="Invalid signature")

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.error("Invalid JSON in risk event payload")
        return Response(status_code=400, content="Invalid JSON")

    try:
        event_in = RiskEventIn.model_validate(body)
    except Exception as e:
        logger.warning("Risk event failed schema validation: %s", e)
        return Response(status_code=400, content="Invalid risk event")

    occurred = event_in.occurred_at or datetime.now(UTC)
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    due = event_in.due_at
    if due is not None and due.tzinfo is None:
        due = due.replace(tzinfo=UTC)

    # The dedup key: the merchant's own event id when supplied, otherwise
    # derived from the natural facts of the event. Either way deterministic,
    # so a re-delivery collides onto the same key and the UNIQUE constraint
    # makes it a clean 200.
    event_id = event_in.event_id or (
        f"{event_in.risk_type}_{event_in.reference_id}_{int(occurred.timestamp())}"
    )

    logger.info(
        "Risk event received: type=%s reference=%s event_id=%s",
        event_in.risk_type, event_in.reference_id, event_id,
    )

    risk_event = RiskEvent(
        event_id=event_id,
        risk_type=event_in.risk_type,
        reference_id=event_in.reference_id,
        amount=event_in.amount_paise,
        currency=event_in.currency,
        customer_id=event_in.customer_id,
        customer_email=event_in.customer_email,
        customer_contact=event_in.customer_contact,
        occurred_at=occurred,
        due_at=due,
        meta=event_in.meta,
        payload=body,
        received_at=datetime.now(UTC),
        processed=False,
    )
    session.add(risk_event)
    try:
        # COMMIT, not flush — the background task opens its own session and
        # looks this row up, same reasoning as the Razorpay webhook route.
        await session.commit()
    except IntegrityError:
        # Re-delivered event: the UNIQUE constraint on event_id fired. The
        # original row is the record; acknowledge and move on.
        await session.rollback()
        logger.info("Duplicate risk event skipped: %s", event_id)
        return Response(status_code=200, content="Already received")

    background_tasks.add_task(_process_risk_event_background, event_id)
    logger.info("Background task queued for risk event: %s", event_id)

    return Response(status_code=200, content="OK")
