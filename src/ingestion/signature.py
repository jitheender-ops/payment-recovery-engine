"""
HMAC-SHA256 signature verification for Razorpay webhooks.

Razorpay signs every webhook with HMAC-SHA256 over the raw request body,
using the merchant's webhook secret as the key. The signature is sent in
the X-Razorpay-Signature header as a hex digest.

CRITICAL: Verify against the raw body bytes, NOT parsed-then-re-serialized JSON.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)

# Largest body either signed surface will read. A Razorpay webhook is a few KB
# and a risk event smaller; a megabyte is generous by two orders of magnitude.
# The cap matters because the whole body is read into memory and then stored
# as JSONB: without it, anyone holding a leaked signing secret could grow the
# event tables without bound, and an unsigned flood still costs a full read
# before the signature check can reject it.
MAX_BODY_BYTES = 1_048_576


def body_too_large(content_length: str | None, body: bytes | None = None) -> bool:
    """
    True when a request body exceeds MAX_BODY_BYTES.

    Checks the declared Content-Length first so an oversized body can be
    refused before it is read, and the actual length second because a chunked
    request declares nothing.
    """
    if content_length:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                return True
        except ValueError:
            # An unparseable Content-Length is not a size we can trust; fall
            # through to measuring what actually arrived.
            pass
    return body is not None and len(body) > MAX_BODY_BYTES


def verify_webhook_signature(
    raw_body: bytes,
    signature: str,
    secret: str,
) -> bool:
    """
    Verify a Razorpay webhook signature.

    Args:
        raw_body: The raw HTTP request body bytes (do NOT re-serialize).
        signature: The value of the X-Razorpay-Signature header.
        secret: The webhook secret from the Razorpay dashboard.

    Returns:
        True if the signature is valid, False otherwise.
    """
    if not signature or not secret:
        logger.warning("Missing signature or secret — rejecting webhook")
        return False

    try:
        expected = hmac.new(
            key=secret.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256,
        ).hexdigest()

        is_valid = hmac.compare_digest(expected, signature)

        if not is_valid:
            logger.warning(
                "Webhook signature mismatch: expected=%s..., got=%s...",
                expected[:12],
                signature[:12],
            )

        return is_valid

    except Exception:
        logger.exception("Error verifying webhook signature")
        return False
