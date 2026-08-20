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
