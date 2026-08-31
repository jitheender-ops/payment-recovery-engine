"""
Voice webhook — the provider-agnostic entry point for telephony callbacks.

Shape: the POST body an Indian telephony provider sends when a call leg
bridges (Exotel, Plivo, Knowlarity and Sarvam's voice stack all speak a
variant of it): an identity we can resolve to a case (customer_id or the
merchant's own subject reference), the STT transcript of what the caller
said, and a session id for logging. Providers differ in field names, so
the body accepts both spellings and the adapter for a specific provider
is a mapping exercise, not a protocol change.

Authentication: HMAC over the raw body with a dedicated secret
(VOICE_WEBHOOK_SECRET), the same construction as the Razorpay webhook
and POST /risks — fail-closed on an unset secret. A provider that cannot
be configured to sign gets blocked at the proxy, not by weakening this.

The turn itself runs the pipeline (4 gates, grounding-verified), and an
opt-out recognised in the transcript closes the customer's open cases via
cases.record_opt_out — the same never-rule that governs SMS outreach.
The reply returns as JSON the provider's TTS leg reads out (Sarvam for
Hinglish, per the Mic RAG measurements); the engine itself makes no
phone calls.
"""

from __future__ import annotations

import base64
import hmac
import logging
import time
import uuid
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from src.cases import record_opt_out
from src.config import get_settings, reveal
from src.database import async_session_factory
from src.models import RecoveryCase
from src.voice import pipeline, sarvam
from src.voice.dialogue import promise_refused
from src.voice.facts import load_facts

logger = logging.getLogger(__name__)
router = APIRouter()


def _voice_secret() -> str:
    return reveal(get_settings().voice_webhook_secret)


def _authorized(raw: bytes, signature: str | None) -> bool:
    secret = _voice_secret()
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw, sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _resolve_facts(body: dict[str, Any]) -> Any:
    """Bind the turn to a case, or answer product questions unbound."""
    customer_id = body.get("customer_id") or body.get("CallFrom") or body.get("From")
    subject_ref = body.get("subject_ref") or body.get("reference_id")

    case_id: uuid.UUID | None = None
    raw_case = body.get("case_id")
    if raw_case:
        try:
            case_id = uuid.UUID(str(raw_case))
        except ValueError:
            case_id = None

    if case_id is None and not subject_ref and customer_id:
        # Resolve through the customer's most recent open case — the
        # recovery call is about the thing currently owed.
        import sqlalchemy as sa

        async with async_session_factory() as session:
            row = (
                await session.execute(
                    sa.select(RecoveryCase.id, RecoveryCase.subject_ref)
                    .where(RecoveryCase.customer_id == customer_id)
                    .order_by(RecoveryCase.opened_at.desc())
                    .limit(1)
                )
            ).first()
            if row is None:
                return None
            return await load_facts(session, case_id=row.id)
    if case_id is None and subject_ref is None:
        return None
    async with async_session_factory() as session:
        return await load_facts(session, case_id=case_id, subject_ref=subject_ref)


@router.post("/voice/turn")
async def voice_turn(request: Request) -> JSONResponse:
    raw = await request.body()
    signature = request.headers.get("x-voice-signature")

    if not _authorized(raw, signature):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body: dict[str, Any] = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    transcript = str(body.get("transcript") or body.get("Speech") or "").strip()
    if not transcript:
        return JSONResponse({"error": "empty transcript"}, status_code=400)

    settings = get_settings()
    facts = await _resolve_facts(body)
    merchant = settings.merchant_name or None

    result = await pipeline.run_turn(
        transcript,
        facts=facts,
        merchant_name=merchant,
        llm_enabled=settings.voice_llm_enabled,
    )

    # An opt-out on the phone closes the customer's cases, same as SMS.
    if result.intent == "opt_out":
        customer_id = body.get("customer_id") or body.get("CallFrom") or body.get("From")
        if customer_id:
            async with async_session_factory() as session:
                closed = await record_opt_out(session, str(customer_id))
                await session.commit()
            logger.info(
                "voice opt-out for %s closed %d case(s)", customer_id, closed
            )

    # A promise to pay on the phone is recorded against the bound case —
    # the same ledger the page and the merchant API write to, with the same
    # silence invariant: record_promise pushes next_action_at to the promise
    # date and the chase stops until then. The parsed amount/due_at were
    # resolved deterministically upstream; the spoken confirmation already
    # re-stated them to the customer.
    if (
        result.intent == "promise_captured"
        and result.promise_amount_paise
        and result.promise_due_at
        and facts is not None
    ):
        async with async_session_factory() as session:
            case = await session.get(RecoveryCase, facts.case_id)
            if case is not None and case.state == "open":
                from src.cases import record_promise

                promise = await record_promise(
                    session,
                    case,
                    amount=result.promise_amount_paise,
                    due_at=result.promise_due_at,
                    channel="voice",
                    confidence="explicit",
                    is_partial=result.promise_is_partial,
                )
                if promise is None:
                    # The cap: the case has run out of belief in words. The
                    # spoken offer moves to the smaller-amount script — the
                    # refusal itself was already logged by record_promise.
                    return JSONResponse(
                        {
                            "reply": promise_refused(),
                            "intent": "promise_refused",
                            "cited": None,
                            "session": body.get("session_id") or body.get("CallSid"),
                            "grounded": True,
                            "notes": ["promise refused: case at promise cap"],
                        }
                    )
                await session.commit()
                logger.info(
                    "voice promise recorded: case=%s amount=%s due=%s",
                    case.id,
                    result.promise_amount_paise,
                    result.promise_due_at.isoformat(),
                )

    return JSONResponse(
        {
            "reply": result.reply,
            "intent": result.intent,
            "cited": result.cited,
            "session": body.get("session_id") or body.get("CallSid"),
            "grounded": bool(result.grounded_passages) or result.intent in
                        ("opt_out", "injection_refused", "abstain"),
            "notes": list(result.notes),
        }
    )


# ── Browser voice demo (local validation surface) ──────────────────────────
# GET /voice/demo — mic → saaras STT → pipeline → bulbul TTS, in the browser.
# Not the production surface: the demo binds no case (so no amounts can be
# spoken — the grounding gate has no facts to ground them in), and the STT
# leg costs real Sarvam quota, so it is not linked from the console nav.


_DEMO_TEMPLATES = None


def _demo_templates() -> Any:
    """Lazily reuse the merchant templates directory (base.html lives there)."""
    global _DEMO_TEMPLATES
    if _DEMO_TEMPLATES is None:
        from pathlib import Path

        from fastapi.templating import Jinja2Templates

        _DEMO_TEMPLATES = Jinja2Templates(
            directory=str(Path(__file__).parent.parent / "merchant" / "templates")
        )
    return _DEMO_TEMPLATES


@router.get("/voice/demo", response_class=HTMLResponse)
async def voice_demo(request: Request) -> Any:
    return _demo_templates().TemplateResponse(request, "voice_demo.html", {})


@router.post("/voice/demo/stt")
async def voice_demo_stt(audio: UploadFile = File(...)) -> JSONResponse:
    """Mic blob → saaras:v3 (auto-detect) → transcript. Real Sarvam quota."""
    try:
        data = await audio.read()
        transcript, language = sarvam.transcribe(data)
        return JSONResponse({"transcript": transcript, "language": language})
    except sarvam.SarvamError as e:
        logger.warning("voice demo STT failed: %s", e)
        return JSONResponse({"detail": str(e)}, status_code=502)


@router.post("/voice/demo/turn")
async def voice_demo_turn(request: Request) -> JSONResponse:
    """Transcript → pipeline → bulbul reply, with the LLM path per settings."""
    try:
        body: dict[str, Any] = await request.json()
    except ValueError:
        return JSONResponse({"detail": "invalid json"}, status_code=400)
    transcript = str(body.get("transcript") or "").strip()
    if not transcript:
        return JSONResponse({"detail": "empty transcript"}, status_code=400)

    settings = get_settings()
    result = await pipeline.run_turn(
        transcript,
        facts=None,  # the demo binds no case: no amounts can be spoken
        merchant_name=settings.merchant_name or None,
        llm_enabled=settings.voice_llm_enabled,
    )

    audio_b64: str | None = None
    tts_error: str | None = None
    t0 = time.monotonic()
    try:
        wav = sarvam.synthesize(result.reply)
        audio_b64 = base64.b64encode(wav).decode("ascii")
    except sarvam.SarvamError as e:
        tts_error = str(e)[:120]
        logger.warning("voice demo TTS failed: %s", e)
    tts_ms = int((time.monotonic() - t0) * 1000)

    return JSONResponse(
        {
            "reply": result.reply,
            "intent": result.intent,
            "cited": result.cited,
            "audio_b64": audio_b64,
            "tts_ms": tts_ms,
            "tts_error": tts_error,
        }
    )


# ── The call queue — the telephony leg's work items ────────────────────────
# POST /voice/queue/claim  { "worker": "exotel-bridge-1" }
#   -> the oldest queued call, claimed atomically, with everything the leg
#      needs to dial: phone, case id, subject_ref. The pipeline's facts are
#      loaded fresh per turn by /voice/turn, so nothing here can go stale.
# POST /voice/queue/report { "call_id", "result", "opted_out" }
#   -> terminal outcome; a spoken opt-out closes the customer's cases
#      through the same record_opt_out path the turn webhook uses.


@router.post("/voice/queue/claim")
async def voice_queue_claim(request: Request) -> JSONResponse:
    raw = await request.body()
    if not _authorized(raw, request.headers.get("x-voice-signature")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body: dict[str, Any] = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    worker = str(body.get("worker") or "unknown")[:120]

    import sqlalchemy as sa

    from src.models import VoiceCallQueue

    async with async_session_factory() as session:
        # Claim = a single UPDATE ... RETURNING so two workers racing for the
        # same row cannot both win: the database picks the winner, not the
        # code. Postgres RETURNING is why this is not a SELECT-then-UPDATE.
        #
        # The join onto recovery_cases is the fire-time re-validation this
        # queue was missing. A queue row is a write-ahead intent, exactly like
        # a RetryAttempt — and every other deferred action in this codebase
        # re-checks the world before acting (fire_due_retries re-runs the
        # guardrail; chase_case re-reads the case). This one did not, so a
        # customer who opted out — on the page, by SMS, or by saying "band
        # karo" on an earlier call — still got dialled from a row queued
        # before they said stop. record_opt_out closes their open cases, and
        # a capture closes a recovered one, so "the case is still open" is the
        # one condition that covers both: no closed case can be called.
        #
        # ponytail: rows on closed cases stay 'queued' and inert rather than
        # being swept to a terminal state. They are unclaimable, so nothing
        # dials them; add a cleanup sweep only if the table's size becomes a
        # real problem.
        row = (
            await session.execute(
                sa.update(VoiceCallQueue)
                .where(
                    VoiceCallQueue.state == "queued",
                    VoiceCallQueue.id
                    == sa.select(VoiceCallQueue.id)
                    .join(
                        RecoveryCase,
                        RecoveryCase.id == VoiceCallQueue.recovery_case_id,
                    )
                    .where(
                        VoiceCallQueue.state == "queued",
                        RecoveryCase.state == "open",
                    )
                    .order_by(VoiceCallQueue.created_at.asc())
                    .limit(1)
                    .scalar_subquery(),
                )
                .values(state="claimed", claimed_at=sa.func.now(), claimed_by=worker)
                .returning(
                    VoiceCallQueue.id,
                    VoiceCallQueue.recovery_case_id,
                    VoiceCallQueue.customer_contact,
                    VoiceCallQueue.risk_type,
                    VoiceCallQueue.amount_paise,
                )
            )
        ).first()
        await session.commit()
        if row is None:
            return JSONResponse({"call": None})

        from src.formatting import money as fmt_money

        return JSONResponse(
            {
                "call": {
                    "call_id": str(row.id),
                    "case_id": str(row.recovery_case_id),
                    "phone": row.customer_contact,
                    "risk_type": row.risk_type,
                    "amount": fmt_money(row.amount_paise),
                    # The conversation runs against /voice/turn with
                    # customer_id/case_id in the body; the leg sends each
                    # turn's transcript there and speaks the reply.
                    "turn_endpoint": "/voice/turn",
                }
            }
        )


@router.post("/voice/queue/report")
async def voice_queue_report(request: Request) -> JSONResponse:
    raw = await request.body()
    if not _authorized(raw, request.headers.get("x-voice-signature")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body: dict[str, Any] = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    call_id = str(body.get("call_id") or "")
    result = str(body.get("result") or "done")[:40]

    if not call_id:
        return JSONResponse({"error": "call_id required"}, status_code=400)

    import uuid as _uuid

    from src.models import VoiceCallQueue

    try:
        parsed = _uuid.UUID(call_id)
    except ValueError:
        return JSONResponse({"error": "bad call_id"}, status_code=400)

    async with async_session_factory() as session:
        row = await session.get(VoiceCallQueue, parsed)
        if row is None:
            return JSONResponse({"error": "unknown call"}, status_code=404)
        if row.state not in ("claimed", "queued"):
            return JSONResponse({"error": "already terminal"}, status_code=409)
        row.state = "opted_out" if body.get("opted_out") else result
        if body.get("opted_out"):
            row.state = "opted_out"
            row.result = "opted_out"
        else:
            row.result = result
            row.state = "done" if result in ("done", "completed", "answered") else "failed"
        await session.commit()

        # A spoken opt-out closes the customer's cases — same never-rule as
        # every other channel. The queue row alone does not know the
        # customer; the case does.
        if body.get("opted_out"):
            case = await session.get(RecoveryCase, row.recovery_case_id)
            if case is not None and case.customer_id:
                closed = await record_opt_out(session, case.customer_id)
                await session.commit()
                logger.info(
                    "voice queue opt-out for %s closed %d case(s)",
                    case.customer_id, closed,
                )

    return JSONResponse({"ok": True, "state": row.state})
