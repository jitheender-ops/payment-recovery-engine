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
      "customer_email": "a@b.in",               // optional
      "customer_contact": "+919812345678",       // optional, but see the note
      "occurred_at": "2026-08-27T10:00:00Z",     // optional, defaults to now
      "due_at": null,                            // invoice due date etc.
      "meta": {"cart_items": "2 books"}          // optional, untrusted
    }

`customer_contact` vs `customer_id`: they are NOT interchangeable. The
voice chaser (VOICE_CHASER_ENABLED) queues a follow-up call only when the
risk event carried a `customer_contact` — the queue row is a phone number,
and `customer_id` (an email, an ERP id, anything) is not assumed to be
dialable. An event pushed with only `customer_id` chases by SMS/page and
silently never gets a call; if calls are wanted, push the phone in
`customer_contact`.

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
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings, reveal
from src.database import async_session_factory, get_session
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
    # The merchant's own code for the buyer organisation (their ERP customer
    # code). Optional: with it, the receivables layer consolidates this
    # invoice's chase under one account for the whole buyer — one statement,
    # one contact budget; without it, the account is derived from the
    # customer identity.
    account_ref: str | None = Field(default=None, max_length=255)
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    # A Razorpay offer id in the MERCHANT's own account, cart events only.
    # The engine relays a discount the merchant already created — it never
    # computes money it does not control. Applied from the second touch on
    # (never the first: research is clear an incentive on touch 1 trains
    # discount-waiting). Validated as an offer-shaped string; Razorpay
    # enforces the offer's real validity/amount rules at link creation.
    offer_id: str | None = Field(default=None, pattern=r"^offer_[A-Za-z0-9]{6,40}$")
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "event_id", "reference_id", "customer_id", "customer_contact", "account_ref"
    )
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        """Reject identifiers carrying control characters — see _CONTROL_CHARS."""
        if value is not None and _CONTROL_CHARS.search(value):
            raise ValueError("control characters are not allowed in identifiers")
        return value

    @field_validator("offer_id")
    @classmethod
    def _offer_only_on_carts(cls, value: str | None, info: Any) -> str | None:
        """Refuse offers outside the cart rail — the merchant's incentive
        discipline (invoice/mandate/subscription money is contract money,
        not discountable through a recovery link)."""
        if value is not None and info.data.get("risk_type") != "checkout_abandonment":
            raise ValueError("offer_id is only valid on checkout_abandonment events")
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
    Re-arm a failed risk event — the shared rearm(), risk flavour. Same cap,
    same logic as the webhook store: one number, one meaning, both stores.
    """
    from src.ingestion.router import rearm

    await rearm(
        session,
        model=RiskEvent,
        lookup_col=RiskEvent.event_id,
        id_col=RiskEvent.id,
        event_id=event_id,
        label="Risk event",
    )


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


class PromiseIn(BaseModel):
    """
    A promise the merchant's own systems collected (a human call, a chat,
    an email reply), for the same ledger the voice agent and recovery page
    write to. The merchant heard it; the engine enforces the silence.
    """

    amount_paise: int = Field(gt=0, le=2_147_483_647)
    due_at: datetime
    is_partial: bool | None = None
    # "explicit" | "tentative" | "conditional" — the merchant's read of how
    # firmly it was said. Free text beyond these is refused here, not
    # sanitized later: the column is a segment label, not a note field.
    confidence: Literal["explicit", "tentative", "conditional"] | None = None
    condition_note: str | None = Field(default=None, max_length=200)
    channel: str | None = Field(default=None, max_length=20)
    language: str | None = Field(default=None, max_length=20)

    @field_validator("condition_note", "channel", "language")
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        """Reject control characters — see _CONTROL_CHARS."""
        if value is not None and _CONTROL_CHARS.search(value):
            raise ValueError("control characters are not allowed")
        return value


@router.post(
    "/{risk_type}/{reference_id}/promise",
    responses={
        200: {"description": "Promise recorded — the case goes quiet until due_at"},
        401: {"description": "Missing or invalid X-Risk-Signature — fail closed"},
        404: {"description": "No open case for that reference"},
        422: {"description": "Promise refused: per-case promise cap reached"},
    },
)
async def record_promise_for_case(
    request: Request,
    risk_type: str,
    reference_id: str,
    x_risk_signature: Annotated[str | None, Header()] = None,
) -> Response:
    """
    Log a promise a merchant's operator collected, against their open case.

    Same HMAC envelope as POST /risks — this is the merchant's second write
    surface, and a forgeable one would let a third party silence arbitrary
    cases (an attacker's dream: stop the recovery, never pay). The promise
    enforces the exact silence a customer-voiced one does: next_action_at
    moves out to due_at, the chase stops, and the kept-rate ledger gains a
    row whose source is auditable (`channel`, `source_ref`).

    Closed cases 404 — a promise on a recovered case is not a deferral,
    it is noise, and writing it would fabricate ledger history.
    """
    raw_body = await request.body()
    if not x_risk_signature:
        return Response(status_code=401, content="Missing signature")
    if not verify_webhook_signature(
        raw_body, x_risk_signature, reveal(get_settings().risk_webhook_secret)
    ):
        return Response(status_code=401, content="Invalid signature")

    try:
        body = json.loads(raw_body)
        promise_in = PromiseIn.model_validate(body)
    except Exception as e:
        logger.warning("Merchant promise failed schema validation: %s", e)
        return Response(status_code=400, content="Invalid promise payload")

    # The risk types this surface covers. A payment-failure promise has its
    # own path (the gateway's webhook drives that rail); anything unknown
    # fails closed like policy_for does.
    if risk_type not in ("checkout_abandonment", "subscription_failure",
                        "invoice_overdue", "mandate_failure"):
        return Response(status_code=404, content="Unknown risk type")

    due = promise_in.due_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    horizon = timedelta(days=get_settings().promise_max_horizon_days)
    if not (now < due <= now + horizon):
        return Response(
            status_code=400,
            content=f"due_at must be in the future and within {horizon.days} days",
        )

    from src.cases import customer_promise_score, find_case, record_promise

    async with async_session_factory() as session:
        case = await find_case(session, risk_type=risk_type, subject_ref=reference_id)
        if case is None or case.state != "open":
            return Response(status_code=404, content="No open case for that reference")

        promise = await record_promise(
            session,
            case,
            amount=promise_in.amount_paise,
            due_at=due,
            channel=promise_in.channel or "merchant",
            language=promise_in.language,
            source_ref="merchant_api",
            is_partial=promise_in.is_partial,
            confidence=promise_in.confidence,
            condition_note=promise_in.condition_note,
        )
        if promise is None:
            await session.commit()  # the refusal audit row persists too
            return Response(
                status_code=422,
                content="Promise cap reached for this case",
            )
        score = await customer_promise_score(session, case.customer_id)
        await session.commit()

    logger.info(
        "Merchant promise recorded: %s/%s amount=%s due=%s",
        risk_type, reference_id, promise_in.amount_paise, due.isoformat(),
    )
    return JSONResponse(
        {
            "status": "recorded",
            "silenced_until": due.isoformat(),
            "attempts_used": case.attempts_used,
            "max_attempts": case.max_attempts,
            "customer_kept_rate": (
                round(score.kept_rate, 3) if score.kept_rate is not None else None
            ),
        }
    )


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
        account_ref=(
            event_in.account_ref.strip() if event_in.account_ref else None
        ),
        occurred_at=occurred,
        due_at=due,
        offer_id=event_in.offer_id,
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
