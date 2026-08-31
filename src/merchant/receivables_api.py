"""FastAPI router for the merchant's receivables operations.

The B2B receivables chaser's merchant-facing API: the closures and verdicts
the engine cannot decide for itself. Three surfaces, all HMAC-signed like
POST /risks (the caller is the merchant's systems, which can produce a
signature over a raw body but not hold interactive credentials):

    POST /cases/paid      money arrived outside the payment rail (NEFT/RTGS/
                          cheque/cash) — closes the case, counted, never
                          claimed as engine-attributed
    POST /cases/dispute   the merchant's verdict on an open dispute:
                          upheld (the invoice was wrong — case closes) or
                          rejected (the chase resumes on its aging clock)
    POST /tasks/done      a human worked a call task — marks it done

Same discipline as the risk router everywhere: verify HMAC over the raw body
→ validate the schema → act → commit. Fail closed on an unset
RISK_WEBHOOK_SECRET — the same secret, because the signer is the same
merchant system; one identity, one key.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings, reveal
from src.database import get_session
from src.ingestion.signature import body_too_large, verify_webhook_signature
from src.receivables.disputes import resolve_dispute
from src.receivables.external import record_external_payment
from src.receivables.models import AccountTask, CaseDispute

logger = logging.getLogger(__name__)
router = APIRouter()


class ExternalPaymentIn(BaseModel):
    """One payment that arrived outside the payment rail."""

    # The recovery case's UUID — the merchant names the case because the
    # money has no engine-side id until this call creates one (recovered_ref).
    case_id: str = Field(min_length=36, max_length=36)
    amount_paise: int = Field(gt=0, le=2_147_483_647)
    # The merchant's own reference for the money: a UTR, a cheque number.
    # Deduped per case — the same ref re-POSTed is an ack, never a second
    # rupee counted.
    paid_ref: str = Field(min_length=1, max_length=255)
    paid_at: datetime | None = None
    method: Literal["neft", "rtgs", "imps", "cheque", "cash", "upi_manual"] = "neft"
    note: str | None = Field(default=None, max_length=500)

    @field_validator("case_id", "paid_ref")
    @classmethod
    def _shaped(cls, value: str) -> str:
        try:
            uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("case_id must be a UUID") from exc
        return value


class DisputeResolutionIn(BaseModel):
    """The merchant's verdict on one open dispute."""

    dispute_id: str = Field(min_length=36, max_length=36)
    # "upheld"   — the invoice was wrong: the case closes abandoned, nothing
    #             is recovered, the write-off is the merchant's bookkeeping.
    # "rejected" — the invoice stands: the chase resumes on its aging clock.
    outcome: Literal["upheld", "rejected"]
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("dispute_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        try:
            uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("dispute_id must be a UUID") from exc
        return value


class TaskDoneIn(BaseModel):
    """A human worked one call task from the merchant's queue."""

    task_id: str = Field(min_length=36, max_length=36)

    @field_validator("task_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        try:
            uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("task_id must be a UUID") from exc
        return value


async def _verified(
    request: Request, x_risk_signature: str | None
) -> dict[str, Any] | None:
    """HMAC-verify the raw body. None → the caller gets the 401/400/413.

    The same three-step gate as the risk router, kept in one place so the
    three endpoints here cannot drift: size cap → signature → parse.
    """
    if body_too_large(request.headers.get("content-length")):
        return None
    import json

    raw = await request.body()
    if body_too_large(None, raw):
        return None
    if not x_risk_signature:
        return None
    if not verify_webhook_signature(
        raw, x_risk_signature, reveal(get_settings().risk_webhook_secret)
    ):
        return None
    try:
        body: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return body


def _sig_failure(x_risk_signature: str | None) -> Response:
    if not x_risk_signature:
        return Response(status_code=401, content="Missing signature")
    return Response(status_code=401, content="Invalid signature")


@router.post("/cases/paid")
async def mark_case_paid(
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_risk_signature: Annotated[str | None, Header()] = None,
) -> Response:
    """
    Close a case on money that arrived outside the payment rail.

    The honesty rule is the same one attribute_capture gives self-recovery:
    the money COUNTS (the case closes, amount_recovered rises) but is never
    CLAIMED as engine-attributed. Idempotent per (case, ref): a re-POST of
    the same bank ref is an ack, never a second rupee.
    """
    import json

    body = await _verified(request, x_risk_signature)
    if body is None:
        # Distinguish the failure modes for the merchant's client: a body
        # that failed the size cap or JSON parse is a 400, a signature
        # failure is a 401.
        raw = await request.body()
        if x_risk_signature and verify_webhook_signature(
            raw, x_risk_signature, reveal(get_settings().risk_webhook_secret)
        ):
            return Response(status_code=400, content="Invalid JSON or body too large")
        return _sig_failure(x_risk_signature)

    try:
        payload = ExternalPaymentIn.model_validate(body)
    except Exception as exc:
        logger.warning("Mark-paid failed schema validation: %s", exc)
        return Response(status_code=400, content="Invalid payload")

    outcome = await record_external_payment(
        session,
        case_id=payload.case_id,
        amount_paise=payload.amount_paise,
        paid_ref=payload.paid_ref,
        paid_at=payload.paid_at,
        method=payload.method,
        note=payload.note,
    )
    await session.commit()
    # The closed vocabulary maps straight onto status codes; the body carries
    # the outcome string for the merchant's logging.
    outcome = outcome or "refused_no_case"
    status = {"recorded": 200, "already_recorded": 200}.get(outcome, 404)
    if outcome == "refused_amount":
        status = 400
    elif outcome in ("refused_no_case", "refused_terminal"):
        status = 409
    return Response(
        status_code=status, content=json.dumps({"outcome": outcome})
    )


@router.post("/cases/dispute")
async def resolve_case_dispute(
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_risk_signature: Annotated[str | None, Header()] = None,
) -> Response:
    """The merchant's verdict on an open dispute. First resolution wins."""
    import json

    body = await _verified(request, x_risk_signature)
    if body is None:
        raw = await request.body()
        if x_risk_signature and verify_webhook_signature(
            raw, x_risk_signature, reveal(get_settings().risk_webhook_secret)
        ):
            return Response(status_code=400, content="Invalid JSON or body too large")
        return _sig_failure(x_risk_signature)

    try:
        payload = DisputeResolutionIn.model_validate(body)
    except Exception as exc:
        logger.warning("Dispute resolution failed schema validation: %s", exc)
        return Response(status_code=400, content="Invalid payload")

    dispute = await session.get(CaseDispute, uuid.UUID(payload.dispute_id))
    if dispute is None:
        return Response(status_code=404, content="No such dispute")

    dispute = await resolve_dispute(
        session, dispute, outcome=payload.outcome, note=payload.note
    )
    await session.commit()
    return Response(
        status_code=200,
        content=json.dumps({"status": dispute.status}),
    )


@router.post("/tasks/done")
async def mark_task_done(
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_risk_signature: Annotated[str | None, Header()] = None,
) -> Response:
    """A human worked a call task. Idempotent: done stays done."""
    body = await _verified(request, x_risk_signature)
    if body is None:
        raw = await request.body()
        if x_risk_signature and verify_webhook_signature(
            raw, x_risk_signature, reveal(get_settings().risk_webhook_secret)
        ):
            return Response(status_code=400, content="Invalid JSON or body too large")
        return _sig_failure(x_risk_signature)

    try:
        payload = TaskDoneIn.model_validate(body)
    except Exception as exc:
        logger.warning("Task-done failed schema validation: %s", exc)
        return Response(status_code=400, content="Invalid payload")

    from src.receivables.tasks import complete_task

    task = await session.get(AccountTask, uuid.UUID(payload.task_id))
    if task is None:
        return Response(status_code=404, content="No such task")
    await complete_task(session, task)
    await session.commit()
    return Response(status_code=200, content="Done")
