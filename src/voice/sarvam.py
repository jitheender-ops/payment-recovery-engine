"""
Sarvam AI clients for the voice agent — STT (saaras:v3) and TTS (bulbul:v2).

Ported from the Mic RAG model's stt/sarvam.py and tts/sarvam.py, which
carried the measurements (STT P50 520.7 ms over real clips; TTS ~478 ms
for 0.87 s of speech) and the hardening decisions worth keeping:

  * magic-byte content-type detection — recorders write MP3 into .wav
    names constantly, and the vendor answers a mislabelled upload with a
    confusing error;
  * bounded linear-backoff retries on 5xx/429/timeouts only — a 4xx
    cannot be retried into success;
  * stdlib only (urllib, no requests dependency on this path);
  * the ssl context fix for macOS interpreters that ship no CA bundle.

Text-in text-out contract: the telephony provider (or the browser demo)
POSTs a transcript; the engine replies text; the caller does its own
STT/TTS through this module or on their side. The engine makes no phone
calls by itself.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
import uuid

from src.config import get_settings, reveal

logger = logging.getLogger(__name__)

STT_URL = "https://api.sarvam.ai/speech-to-text"
TTS_URL = "https://api.sarvam.ai/text-to-speech"
STT_MODEL = "saaras:v3"
TTS_MODEL = "bulbul:v3"  # v2 is deprecated — the API 400s with a redirect to v3
# anushka (the v2 pick) is gone in v3; priya is the closest warm female voice
# of the v3 set (aditya, ritu, ashutosh, priya, neha, rahul, pooja, rohan,
# simran, kavya, amit, dev, ...). SARVAM_TTS_SPEAKER overrides it.
DEFAULT_SPEAKER = os.getenv("SARVAM_TTS_SPEAKER", "priya")

# Hinglish is Hindi + English code-mix; saaras auto-detects when told
# "unknown" and handles mixed-script speech natively.
STT_LANGUAGE = "unknown"
TTS_LANGUAGE = "hi-IN"

_MAGIC = (
    (b"RIFF", "audio/wav"), (b"OggS", "audio/ogg"), (b"fLaC", "audio/flac"),
    (b"ID3", "audio/mpeg"), (b"\x1a\x45\xdf\xa3", "audio/webm"),
)


class SarvamError(RuntimeError):
    pass


def _key() -> str:
    key = reveal(get_settings().sarvam_api_key)
    if not key:
        raise SarvamError(
            "SARVAM_API_KEY is not set — the voice demo cannot transcribe. "
            "Put it in .env; the demo page shows this error verbatim."
        )
    return key


def _ssl_context() -> ssl.SSLContext:
    """macOS python.org/uv interpreters ship no CA bundle; certifi fixes it,
    the system default is the fallback, and no-verification never ships."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL: ssl.SSLContext | None = None


def _ctx() -> ssl.SSLContext:
    global _SSL
    if _SSL is None:
        _SSL = _ssl_context()
    return _SSL


def content_type_of(audio: bytes) -> str:
    """What the bytes actually are — the extension is a claim, the first
    bytes are the fact."""
    head = audio[:16]
    for sig, mime in _MAGIC:
        if head.startswith(sig):
            return mime
    if len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    if head[4:8] == b"ftyp":
        return "audio/mp4"
    return "application/octet-stream"


def _post(
    url: str, body: bytes, content_type: str, *, retries: int = 2, timeout_s: float = 20.0
) -> dict[str, object]:
    key = _key()
    last = ""
    for attempt in range(1, retries + 2):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("api-subscription-key", key)
        req.add_header("Content-Type", content_type)
        try:
            # `url` is a parameter, but it is never attacker-influenced: both
            # call sites pass a module-level constant (STT_URL / TTS_URL, the
            # two literal https://api.sarvam.ai endpoints above) and nothing
            # reaches this function from a request body. The rule's concern is
            # a `file://` scheme smuggled in through user input, which has no
            # path here. Suppressed at this line rather than by rule, so a
            # genuinely caller-supplied URL would still be flagged.
            # The annotation has to be the LAST line before the finding —
            # semgrep reads only the immediately preceding line, so the
            # rationale goes above it, not between it and the code.
            # nosemgrep: dynamic-urllib-use-detected
            with urllib.request.urlopen(req, timeout=timeout_s, context=_ctx()) as r:
                return dict(json.loads(r.read()))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:200]!r}"
            if e.code < 500 and e.code != 429:  # client error: retrying cannot fix it
                break
        except Exception as e:  # timeout, DNS, reset — worth a retry
            last = repr(e)
            if "CERTIFICATE_VERIFY_FAILED" in last:
                raise SarvamError(
                    "TLS verification failed against the Sarvam API — this "
                    "interpreter has no CA bundle. `uv pip install certifi` "
                    "fixes it."
                ) from e
        if attempt <= retries:
            time.sleep(0.25 * attempt)
    raise SarvamError(f"sarvam call failed after {retries + 1} attempt(s): {last}")


def transcribe(
    audio: bytes, *, retries: int = 2, timeout_s: float = 20.0
) -> tuple[str, str]:
    """
    (transcript, detected_language). saaras:v3 with language_code="unknown"
    auto-detects — the right mode for code-mixed Hinglish.
    """
    boundary = f"----voice-{uuid.uuid4().hex}"
    fields = {"model": STT_MODEL, "language_code": STT_LANGUAGE}
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, v in fields.items()
    ]
    ctype = content_type_of(audio)
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"audio\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
    )
    parts.append(audio)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    payload = _post(
        STT_URL, body, f"multipart/form-data; boundary={boundary}",
        retries=retries, timeout_s=timeout_s,
    )
    text = str(payload.get("transcript", "")).strip()
    lang = str(payload.get("language_code", "") or "unknown")
    if not text:
        raise SarvamError("sarvam returned an empty transcript")
    return text, lang


def synthesize(
    text: str, *, speaker: str = DEFAULT_SPEAKER, retries: int = 1, timeout_s: float = 30.0
) -> bytes:
    """WAV bytes — bulbul:v2, anushka, hi-IN. The reply path's LLM rephrase
    keeps spoken answers short enough that the 1500-char API cap never
    binds, and this enforces a hard 1500 cut regardless."""
    text = (text or "").strip()[:1500]
    if not text:
        raise SarvamError("nothing to speak")
    body = json.dumps(
        {
            "text": text,
            "target_language_code": TTS_LANGUAGE,
            "speaker": speaker,
            "model": TTS_MODEL,
        }
    ).encode()
    payload = _post(TTS_URL, body, "application/json", retries=retries, timeout_s=timeout_s)
    audios = payload.get("audios")
    if not isinstance(audios, list) or not audios:
        raise SarvamError(f"no audio in the response: {list(payload)}")
    return base64.b64decode(str(audios[0]))
