"""
Signed, expiring links that let a customer open their own recovery page.

The page at /recover/<token> shows one customer their own failed payment, so
the URL is the only credential. That rules out the two obvious designs: a raw
case id is guessable and enumerable, and an API key cannot be handed to a
member of the public.

So the token carries the case id plus an expiry and is signed with a dedicated
secret. It is:

  * unguessable   — forging one requires the secret
  * scoped        — it names exactly one case and grants nothing else
  * expiring      — it dies with the consent window, so a link forwarded weeks
                    later stops working rather than reopening a closed case
  * PII-free      — no email, phone, amount or order id in the URL, because
                    URLs end up in SMS logs, browser history and referrer
                    headers

A DEDICATED secret, not the Razorpay webhook secret. Reusing that would mean a
leak of one is a leak of both, and the two have completely different blast
radii — one verifies Razorpay's identity, the other authorises a stranger to
view a payment.

Empty secret means the feature is OFF and every token is rejected. Same
fail-closed rule the rest of the config follows: a guard that waves everyone
through when unconfigured is the exact failure it exists to prevent.
"""

from __future__ import annotations

import base64
import hmac
import logging
import time
import uuid
from hashlib import sha256

from src.config import get_settings, reveal

logger = logging.getLogger(__name__)

_SEP = "."


def _b64(raw: bytes) -> str:
    """URL-safe base64 with the padding stripped — '=' is ugly in an SMS."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), sha256).digest())


def mint(case_id: uuid.UUID, *, ttl_hours: int | None = None) -> str | None:
    """
    Build a token for one case, or None when the feature is unconfigured.

    Default lifetime is the consent window. The guardrail already refuses to
    act on a case past that point, so a link that outlived it would offer a
    payment the engine itself would not have initiated.
    """
    settings = get_settings()
    secret = reveal(settings.recovery_link_secret)
    if not secret:
        return None
    hours = ttl_hours if ttl_hours is not None else settings.consent_window_hours
    payload = f"{case_id.hex}{_SEP}{int(time.time()) + hours * 3600}"
    return f"{_b64(payload.encode())}{_SEP}{_sign(payload, secret)}"


def verify(token: str) -> uuid.UUID | None:
    """
    The case this token names, or None if it is forged, expired or malformed.

    One return value for every failure. Telling a caller *why* a token failed
    lets someone probe the difference between "expired" and "bad signature",
    which is a slow oracle for the secret.
    """
    secret = reveal(get_settings().recovery_link_secret)
    if not secret or not token or token.count(_SEP) != 1:
        return None

    encoded, signature = token.split(_SEP)
    try:
        payload = _unb64(encoded).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None

    # compare_digest on bytes: it raises TypeError on a str holding non-ASCII,
    # and the token is whatever arrived in the URL.
    if not hmac.compare_digest(
        _sign(payload, secret).encode("ascii"), signature.encode("utf-8", "replace")
    ):
        return None

    if payload.count(_SEP) != 1:
        return None
    case_hex, _, expiry = payload.partition(_SEP)
    try:
        if int(expiry) < int(time.time()):
            return None
        return uuid.UUID(hex=case_hex)
    except ValueError:
        return None


def url_for(case_id: uuid.UUID) -> str | None:
    """The absolute link to put in a message, or None if unconfigured."""
    token = mint(case_id)
    if token is None:
        return None
    base = get_settings().public_base_url.rstrip("/")
    if not base:
        return None
    return f"{base}/recover/{token}"
