"""
Plivo call leg — the mouth for the voice agent.

The engine never dials a phone. It queues calls (voice_call_queue), and this
bridge is the process that claims them through POST /voice/queue/claim,
dials through Plivo's REST API, and then serves the XML documents Plivo
fetches during the call:

    answer  ->  <Speak> the AI-disclosure greeting (dialogue.GREETING),
                then <GetInput> with a speech-ready action URL
    turn    ->  download Plivo's recording, Sarvam STT, POST /voice/turn
                (the signed, grounded pipeline), Sarvam TTS, <Play>
    hangup  ->  <Speak> the farewell, <Hangup>

Why XML and not the streaming API: GetInput/Record/Play is the documented,
region-safe shape for India outbound, works on every Plivo account tier,
and keeps the bridge stateless between turns — every callback carries the
CallUUID the queue row's state machine keys on.

Authentication, all three surfaces fail-closed on unset secrets:
  * Plivo -> bridge callbacks (answer/turn/hangup/audio): HMAC-SHA256 over
    the raw body with VOICE_WEBHOOK_SECRET — the same construction and the
    SAME secret as /voice/turn, because both sides are "the provider calls
    us" and one signing secret must not fork per endpoint.
  * bridge -> engine (queue claim/report, /voice/turn): the same secret.
  * bridge -> Plivo REST: basic auth with PLIVO_AUTH_ID/TOKEN.

The TTS audio is served at /plivo/audio/<call_id>/<n>.wav: unguessable via
a per-call random token bound to the CallUUID, because Plivo's <Play>
needs a public URL and the WAV bytes must not be enumerable.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import secrets
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from src import rate_limit
from src.auth import client_ip
from src.config import get_settings, reveal
from src.voice import sarvam
from src.voice.dialogue import GREETING, HANGUP, render

logger = logging.getLogger(__name__)
router = APIRouter()

PLIVO_API_BASE = "https://api.plivo.com/v1/Account"
# Max turns before the bridge wraps up: an unanswered question loop is a
# trapped customer, and a recovery call has one purpose.
MAX_TURNS = 12

# ── Rate limiting ───────────────────────────────────────────────────────────
# Signature auth proves the callback came from the proxy we trust; it says
# nothing about volume. A provider bug (or a leaked proxy secret) looping
# turn callbacks would burn Sarvam quota through THIS surface too, because
# each turn runs STT + an engine call + TTS. Fixed-window limiter per IP —
# in-process until REDIS_URL is set, shared across workers after
# (src/rate_limit.py) — sized for a real call: 12 turns + nudge retries +
# Plivo redeliveries, several calls at once, still inside the window.
_RATE_LIMIT = 120            # signed callbacks per IP per window


def _check_rate(request: Request) -> None:
    rate_limit.check(f"plivo:{client_ip(request)}", _RATE_LIMIT)


class BridgeError(RuntimeError):
    pass


# ── shared signing ────────────────────────────────────────────────────────


def _voice_secret() -> str:
    return reveal(get_settings().voice_webhook_secret)


def _authorized(raw: bytes, signature: str | None) -> bool:
    secret = _voice_secret()
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw, sha256).hexdigest()
    # Bytes against bytes, with the attacker-chosen side coerced by "replace":
    # compare_digest raises TypeError on a non-ASCII str, and an HTTP header
    # may legally carry obs-text, so a non-ASCII signature must be a clean
    # 401 rather than a 500. Same construction as src/voice/webhook.py.
    return hmac.compare_digest(
        expected.encode("ascii"), (signature or "").encode("utf-8", "replace")
    )


# ── per-call state (audio + turn counter) ────────────────────────────────
# In-process, keyed by CallUUID. A bridge restart drops it mid-call; Plivo
# then fails the next XML fetch and closes the call — acceptable for a
# recovery follow-up, which the queue can re-queue only via a fresh attempt.
# ponytail: no persistence — if a bridge survives past MAX_TURNS * (call
# length) with growth, move to a bounded LRU or the voice_call_queue row.

_CALLS: dict[str, dict[str, Any]] = {}
_AUDIO_DIR = Path("/tmp") / "voice_bridge_audio"


def _call_state(call_uuid: str) -> dict[str, Any]:
    state = _CALLS.get(call_uuid)
    if state is None:
        # Unknown CallUUID = a callback for a call this bridge never made
        # (or a restart wiped it). Serve the farewell rather than 500 —
        # the caller must never be dropped mid-sentence.
        state = {
            "turn": MAX_TURNS,  # forces hangup on the next turn
            "token": secrets.token_urlsafe(18),
            "case_id": None,
            "customer_id": None,
        }
        _CALLS[call_uuid] = state
    return state


# ── XML ───────────────────────────────────────────────────────────────────


def _xml(doc: str) -> Response:
    return Response(content=doc, media_type="application/xml")


def _merchant() -> str:
    return get_settings().merchant_name or "the merchant"


def _farewell() -> str:
    return render(HANGUP.response, _merchant())


def _get_input_attrs() -> str:
    return (
        "inputType=\"SPEECH\" method=\"POST\" timeout=\"7\" "
        "finishOnKey=\"#\" speechEndTimeout=\"auto\" redirect=\"true\" "
        "log=\"false\""
    )


def _turn_action(call_uuid: str, n: int) -> str:
    base = get_settings().plivo_bridge_base_url.rstrip("/")
    sig = _sign_getinput(call_uuid, n)
    return f"{base}/plivo/turn?call_uuid={call_uuid}&n={n}&sig={sig}"


def _speak_then_get_input(text: str, call_uuid: str, n: int) -> str:
    return (
        "<Response>"
        f"<Speak language=\"hi-IN\" voice=\"Polly.Aditi\">{escape(text)}</Speak>"
        f"<GetInput {_get_input_attrs()} action=\"{escape(_turn_action(call_uuid, n))}\">"
        "</GetInput>"
        "</Response>"
    )


def _play_then_get_input(audio_url: str, call_uuid: str, n: int) -> str:
    return (
        "<Response>"
        f"<Play>{escape(audio_url)}</Play>"
        f"<GetInput {_get_input_attrs()} action=\"{escape(_turn_action(call_uuid, n))}\">"
        "</GetInput>"
        "</Response>"
    )


def _sign_getinput(call_uuid: str, n: int) -> str:
    secret = _voice_secret()
    return hmac.new(
        (secret + call_uuid).encode(), str(n).encode(), sha256
    ).hexdigest()[:32]


def _check_getinput_sig(call_uuid: str, n: int, sig: str) -> bool:
    # Same bytes-vs-bytes coercion as _authorized: the sig arrives in the
    # URL query string and may be any bytes; compare_digest on a non-ASCII
    # str raises TypeError, which would surface as a 500 on the callback.
    return hmac.compare_digest(
        _sign_getinput(call_uuid, n).encode("ascii"),
        (sig or "").encode("utf-8", "replace"),
    )


def _hangup_xml(farewell: str | None = None) -> str:
    body = f"<Speak language=\"hi-IN\">{escape(farewell)}</Speak>" if farewell else ""
    return f"<Response>{body}<Hangup/></Response>"


# ── callbacks Plivo fetches during the call ────────────────────────────────


@router.post("/plivo/answer")
async def plivo_answer(request: Request) -> Response:
    """
    Call bridged -> greet (the AI disclosure is the first sentence) and
    listen. The greeting template is dialogue.GREETING, the exact line the
    compliance note in voice/TODO.md section 2 demands.

    Plivo echoes the `client` parameter the dialer set (the queue row's
    call_id) — that is how the callbacks bind Plivo's CallUUID to the
    engine's queue row for the terminal report.
    """
    raw = await request.body()
    if not _authorized(raw, request.headers.get("x-voice-signature")):
        return PlainTextResponse("unauthorized", status_code=401)
    _check_rate(request)
    form = _parse_form(raw)
    call_uuid = str(form.get("CallUUID") or "")
    queue_call_id = str(form.get("client") or "")
    if not call_uuid:
        return PlainTextResponse("missing CallUUID", status_code=400)
    state = _call_state(call_uuid)
    state["turn"] = 0
    if queue_call_id:
        # The `client` value was set by this bridge at dial time and the
        # whole body is signature-checked, so it is ours to trust.
        state["queue_call_id"] = queue_call_id
        claimed = _PENDING.pop(queue_call_id, None)
        if claimed is not None:
            state["case_id"] = claimed.get("case_id")
    merchant = get_settings().merchant_name or "the merchant"
    return _xml(_speak_then_get_input(render(GREETING.response, merchant), call_uuid, 1))


@router.post("/plivo/turn")
async def plivo_turn(request: Request) -> Response:
    """
    One GetInput round: recording in -> Sarvam STT -> the engine's signed
    /voice/turn (opt-out, injection refusal, grounded answer) -> Sarvam
    TTS -> Play + next GetInput.

    Every spoken number still passes the pipeline's numeric grounding gate:
    this bridge only ferries bytes; it invents nothing.
    """
    raw = await request.body()
    if not _authorized(raw, request.headers.get("x-voice-signature")):
        return PlainTextResponse("unauthorized", status_code=401)
    _check_rate(request)
    form = _parse_form(raw)
    call_uuid = str(form.get("CallUUID") or "")
    n = int(request.query_params.get("n") or "0")
    sig = request.query_params.get("sig") or ""
    if not call_uuid or not _check_getinput_sig(call_uuid, n, sig):
        return PlainTextResponse("bad turn context", status_code=400)
    state = _call_state(call_uuid)

    if n >= MAX_TURNS:
        return _xml(_hangup_xml(_farewell()))

    # No speech captured (caller said nothing for 7s) -> nudge once, then wrap.
    recording_url = str(form.get("RecordingUrl") or form.get("RecordingPlaybackUri") or "")
    if not recording_url:
        if state.get("silence_nudge"):
            return _xml(_hangup_xml(_farewell()))
        state["silence_nudge"] = True
        return _xml(_speak_then_get_input(
            "Aap wahin hain? Kuch poochna chahenge to boliye.", call_uuid, n + 1
        ))
    state.pop("silence_nudge", None)

    try:
        transcript = _stt_from_plivo(recording_url)
    except Exception:
        logger.exception("bridge STT failed for %s", call_uuid)
        return _xml(_speak_then_get_input(
            "Maaf kijiye, mujhe sunayi nahi diya. Kripya dohraayein.",
            call_uuid, n + 1,
        ))

    turn = _engine_turn(transcript, state, call_uuid)
    intent = str(turn.get("intent") or "answer")
    reply = str(turn.get("reply") or "")
    state["turn"] = n

    # Terminal intents end the call: opt_out closes the customer's cases
    # inside /voice/turn already; a promise is confirmed and needs no more
    # questions; repeated abstention is a hangup, per the TODO's policy note.
    if intent == "opt_out":
        state["reported"] = True  # the hangup callback must not overwrite it
        _report(call_uuid, "opted_out", opted_out=True)
        return _xml(_hangup_xml(reply))
    if intent in ("promise_captured", "promise_refused"):
        state["reported"] = True
        _report(call_uuid, "promise_captured" if intent == "promise_captured" else "done")
        return _xml(_hangup_xml(f"{reply} {_farewell()}"))
    abstains = state.get("abstains", 0)
    if intent == "abstain":
        abstains += 1
        state["abstains"] = abstains
        if abstains >= 3:
            state["reported"] = True
            _report(call_uuid, "abstained")
            return _xml(_hangup_xml(f"{reply} {_farewell()}"))

    try:
        url = _tts_to_public_url(reply, call_uuid, n)
    except Exception:
        logger.exception("bridge TTS failed for %s", call_uuid)
        # The text is grounded; the voice failed. Speak via Plivo's own TTS
        # rather than dropping the answer — degraded accent, same facts.
        return _xml(_speak_then_get_input(reply, call_uuid, n + 1))
    return _xml(_play_then_get_input(url, call_uuid, n + 1))


@router.post("/plivo/hangup")
async def plivo_hangup(request: Request) -> Response:
    """
    Plivo's terminal callback (hangup, no-answer, busy, failed). Marks the
    call done/failed with the queue so the row does not sit 'claimed'
    forever. Signature-checked like every other callback; unauthenticated
    failures are logged and ignored — there is nothing left to protect.
    """
    raw = await request.body()
    if not _authorized(raw, request.headers.get("x-voice-signature")):
        return PlainTextResponse("unauthorized", status_code=401)
    _check_rate(request)
    form = _parse_form(raw)
    call_uuid = str(form.get("CallUUID") or "")
    if not call_uuid:
        return PlainTextResponse("missing CallUUID", status_code=400)
    state = _CALLS.pop(call_uuid, None)
    status = str(form.get("CallStatus") or "").lower()
    result = "done" if status in ("completed",) else (
        "failed" if status in ("no-answer", "busy", "failed") else "done"
    )
    # Always report unless an earlier terminal report already landed
    # (opt_out, promise). A missing state (no-answer, or a bridge restart
    # between answer and hangup) must still free the queue row — a call
    # that never reached a callback would otherwise sit 'claimed' forever.
    if state is None or not state.get("reported"):
        _report(call_uuid, result)
    _cleanup_audio(call_uuid)
    return _xml("<Response/>")


@router.get("/plivo/audio/{call_uuid}/{name}")
async def plivo_audio(call_uuid: str, name: str) -> Response:
    """
    The WAV Plivo <Play>s. Exists only while the call is live.

    Not HMAC-able (Plivo fetches it with GET, no body, and signs nothing it
    fetches): the protection is that the URL exists only for the seconds the
    call is live, under the unguessable CallUUID, and carries nothing but
    this call's own grounded reply.
    """
    if call_uuid not in _CALLS:
        return PlainTextResponse("no such call", status_code=404)
    name_check = name.removesuffix(".wav")
    if not name_check.isdigit() or not name.endswith(".wav"):
        return PlainTextResponse("bad name", status_code=400)
    path = _AUDIO_DIR / call_uuid / name
    if not path.is_file():
        return PlainTextResponse("gone", status_code=404)
    return Response(
        content=path.read_bytes(),
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


# ── helpers: form parsing, STT/TTS, engine turn, reporting ───────────────


def _parse_form(raw: bytes) -> dict[str, str]:
    """Plivo POSTs application/x-www-form-urlencoded; read it as UTF-8."""
    from urllib.parse import parse_qs

    parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


def _plivo_rest(path: str, body: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    auth_id = settings.plivo_auth_id
    token = reveal(settings.plivo_auth_token)
    if not auth_id or not token:
        raise BridgeError("PLIVO_AUTH_ID / PLIVO_AUTH_TOKEN not configured")
    url = f"{PLIVO_API_BASE}/{auth_id}/{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Basic " + base64.b64encode(
        f"{auth_id}:{token}".encode()
    ).decode("ascii"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # nosemgrep: dynamic-urllib-use-detected
            return dict(json.loads(r.read()))
    except urllib.error.HTTPError as e:
        raise BridgeError(f"plivo REST {e.code}: {e.read()[:200]!r}") from e


def _stt_from_plivo(recording_url: str) -> str:
    """
    Download the call recording and transcribe via Sarvam saaras:v3.

    The URL is the value Plivo itself POSTed in the form body — not a value
    an unauthenticated third party can steer, because the form was
    signature-checked before this runs.
    """
    # URL is Plivo's own signed form value, never caller-supplied; see the
    # rationale in the docstring above. Suppressed at this line only.
    # nosemgrep: dynamic-urllib-use-detected
    with urllib.request.urlopen(recording_url, timeout=30) as r:
        audio = r.read()
    transcript, _ = sarvam.transcribe(audio)
    return transcript


def _tts_to_public_url(text: str, call_uuid: str, n: int) -> str:
    wav = sarvam.synthesize(text)
    _AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    call_dir = _AUDIO_DIR / call_uuid
    call_dir.mkdir(exist_ok=True)
    (call_dir / f"{n}.wav").write_bytes(wav)
    base = get_settings().plivo_bridge_base_url.rstrip("/")
    return f"{base}/plivo/audio/{call_uuid}/{n}.wav"


def _engine_turn(transcript: str, state: dict[str, Any], call_uuid: str) -> dict[str, Any]:
    """POST the signed /voice/turn — the grounded pipeline, not this bridge.

    case_id binds the turn to the case the queue row was claimed for, so
    the facts (amounts, state) the agent may speak come from the engine's
    own rows — never from anything the bridge could invent.
    """
    settings = get_settings()
    secret = _voice_secret()
    body = json.dumps({
        "transcript": transcript,
        "session_id": call_uuid,
        "case_id": state.get("case_id") or "",
    }).encode()
    sig = hmac.new(secret.encode(), body, sha256).hexdigest()
    engine_base = settings.plivo_engine_base_url or settings.plivo_bridge_base_url
    req = urllib.request.Request(
        f"{engine_base.rstrip('/')}/voice/turn", data=body, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Voice-Signature", sig)
    with urllib.request.urlopen(req, timeout=30) as r:  # nosemgrep: dynamic-urllib-use-detected
        return dict(json.loads(r.read()))


def _report(call_uuid: str, result: str, *, opted_out: bool = False) -> None:
    queue_call_id = _CALLS.get(call_uuid, {}).get("queue_call_id") or call_uuid
    settings = get_settings()
    secret = _voice_secret()
    body = json.dumps({
        "call_id": queue_call_id,
        "result": result,
        "opted_out": opted_out,
    }).encode()
    sig = hmac.new(secret.encode(), body, sha256).hexdigest()
    engine_base = settings.plivo_engine_base_url or settings.plivo_bridge_base_url
    req = urllib.request.Request(
        f"{engine_base.rstrip('/')}/voice/queue/report", data=body, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Voice-Signature", sig)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # nosemgrep: dynamic-urllib-use-detected
            r.read()
    except Exception:
        logger.exception("bridge failed to report call %s outcome", call_uuid)


def _cleanup_audio(call_uuid: str) -> None:
    import shutil

    shutil.rmtree(_AUDIO_DIR / call_uuid, ignore_errors=True)


# ── the worker: claim queued calls, dial them ─────────────────────────────


def claim_and_dial(worker: str = "plivo-bridge") -> dict[str, Any] | None:
    """
    One poll iteration: claim the oldest queued call and dial it.

    Returns the claimed call dict (for tests and logging) or None when the
    queue is empty. Synchronous by design: run this in a loop from
    scripts/run_plivo_bridge.py — asyncio here would buy nothing against
    Plivo's synchronous REST API.
    """
    settings = get_settings()
    secret = _voice_secret()
    if not secret:
        raise BridgeError(
            "VOICE_WEBHOOK_SECRET is not set — the bridge cannot sign its "
            "requests and refuses to run. Same fail-closed rule as every "
            "signed surface."
        )
    if not (settings.plivo_auth_id and settings.plivo_caller_number
            and settings.plivo_bridge_base_url):
        raise BridgeError(
            "PLIVO_AUTH_ID / PLIVO_CALLER_NUMBER / PLIVO_BRIDGE_BASE_URL "
            "are not configured — the bridge refuses to run half-configured."
        )

    engine_base = (settings.plivo_engine_base_url or settings.plivo_bridge_base_url).rstrip("/")
    claim_body = json.dumps({"worker": worker}).encode()
    claim_sig = hmac.new(secret.encode(), claim_body, sha256).hexdigest()
    req = urllib.request.Request(
        f"{engine_base}/voice/queue/claim", data=claim_body, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Voice-Signature", claim_sig)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # nosemgrep: dynamic-urllib-use-detected
            claimed = dict(json.loads(r.read())).get("call")
    except urllib.error.URLError as e:
        raise BridgeError(f"claim failed: {e}") from e
    if not claimed:
        return None

    call = dict(claimed)
    base = settings.plivo_bridge_base_url.rstrip("/")
    answer_url = f"{base}/plivo/answer"
    hangup_url = f"{base}/plivo/hangup"
    # Keep the claimed details reachable by queue call_id so the answer
    # callback can bind the case onto the per-call state.
    _PENDING[str(call.get("call_id") or "")] = call
    created = _plivo_rest("Call/", {
        "from": settings.plivo_caller_number,
        "to": str(call.get("phone") or ""),
        "answer_url": answer_url,
        "answer_method": "POST",
        "hangup_url": hangup_url,
        "hangup_method": "POST",
        # The engine's own queue row id rides along; Plivo echoes it in
        # the answer callback as the `client` parameter, which is how the
        # per-call state binds Plivo's CallUUID to the queue row.
        "client": str(call.get("call_id") or ""),
    })
    request_id = str(created.get("request_id") or "")
    if request_id:
        logger.info(
            "bridge dialled case=%s phone=%s request=%s",
            call.get("case_id"), call.get("phone"), request_id,
        )
    return call


# queue call_id -> claimed call details, from dial time until the answer
# callback resolves them onto the per-call state keyed by Plivo's CallUUID.
_PENDING: dict[str, dict[str, Any]] = {}
