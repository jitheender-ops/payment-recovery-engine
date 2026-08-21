"""
API key authentication for the non-webhook surface.

Kept in its own module rather than in main.py so routers can depend on it
without importing the app object back — that import would be circular.

The webhook route does NOT use this. Razorpay sends what Razorpay sends, and a
custom header is not on the list, so that endpoint authenticates by HMAC over
the raw request body (src/ingestion/signature.py). Two surfaces, two mechanisms,
each matched to what the caller can actually prove.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from src.config import get_settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """
    FastAPI dependency — 401 unless the caller presents the configured key.

    Fail-closed on an unset key. An unconfigured guard that waves everyone
    through is indistinguishable from having no guard at all, and this exists
    precisely for the deployment where someone forgot to set it.
    """
    expected = get_settings().api_key
    # Compare bytes, not str: compare_digest raises TypeError on a str
    # containing non-ASCII, which an attacker controls via the header and which
    # would surface as a 500 instead of a 401.
    if (
        not expected
        or not x_api_key
        or not hmac.compare_digest(x_api_key.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key",
        )
