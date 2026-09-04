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
  * expiring      — it dies after recovery_link_ttl_hours (a day by default,
                    never more than the consent window), so a link forwarded
                    weeks later stops working rather than reopening a closed
                    case
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
import re
import time
import uuid
from datetime import UTC, datetime
from hashlib import sha256

from src.config import get_settings, reveal

logger = logging.getLogger(__name__)

# Token primitives, shared with the merchant console session cookie
# (src/merchant/routes.py): base64(payload).sign, "." separated.
SEP = "."


def b64(raw: bytes) -> str:
    """URL-safe base64 with the padding stripped — '=' is ugly in an SMS."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: str, secret: str) -> str:
    return b64(hmac.new(secret.encode(), payload.encode(), sha256).digest())


def mint(case_id: uuid.UUID, *, ttl_hours: int | None = None) -> str | None:
    """
    Build a token for one case, or None when the feature is unconfigured.

    Default lifetime is recovery_link_ttl_hours (one day), capped at the
    consent window. Two bounds, two reasons: the cap exists because the
    guardrail refuses to act on a case past the consent window, so a link
    that outlived it would offer a payment the engine itself would not have
    initiated; the shorter default exists because the URL is a bearer
    credential in SMS logs and browser history, and every nudge mints a
    fresh link anyway — nothing needs the long life, so don't hand it out.
    """
    settings = get_settings()
    secret = reveal(settings.recovery_link_secret)
    if not secret:
        return None
    # The consent-window cap applies to explicit callers too: a link must
    # never outlive the engine's authority to act, whoever picks the number.
    ttl_hours = min(
        ttl_hours if ttl_hours is not None else settings.recovery_link_ttl_hours,
        settings.consent_window_hours,
    )
    payload = f"{case_id.hex}{SEP}{int(time.time()) + ttl_hours * 3600}"
    return f"{b64(payload.encode())}{SEP}{sign(payload, secret)}"


def verify(token: str) -> uuid.UUID | None:
    """
    The case this token names, or None if it is forged, expired or malformed.

    One return value for every failure. Telling a caller *why* a token failed
    lets someone probe the difference between "expired" and "bad signature",
    which is a slow oracle for the secret.
    """
    verified = verify_with_expiry(token)
    return verified[0] if verified else None


def verify_with_expiry(token: str) -> tuple[uuid.UUID, datetime] | None:
    """
    (case, expiry instant) for a valid token, else None.

    The page shows the link's REAL deadline — "this link works until Sat,
    11 AM" — because an honest deadline outperforms a fake countdown and the
    consent window is the one we actually enforce. Same one-return-value
    failure discipline as verify(): every failure looks identical.
    """
    secret = reveal(get_settings().recovery_link_secret)
    if not secret or not token or token.count(SEP) != 1:
        return None

    encoded, signature = token.split(SEP)
    try:
        payload = unb64(encoded).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None

    # compare_digest on bytes: it raises TypeError on a str holding non-ASCII,
    # and the token is whatever arrived in the URL.
    if not hmac.compare_digest(
        sign(payload, secret).encode("ascii"), signature.encode("utf-8", "replace")
    ):
        return None

    if payload.count(SEP) != 1:
        return None
    case_hex, _, expiry = payload.partition(SEP)
    try:
        expiry_epoch = int(expiry)
        if expiry_epoch < int(time.time()):
            return None
        if not re.fullmatch(r"[0-9a-f]{32}", case_hex):
            return None
        return uuid.UUID(hex=case_hex), datetime.fromtimestamp(expiry_epoch, tz=UTC)
    except ValueError:
        return None


# ── Account scope: the same scheme, one field wider ──────────────────────
#
# A statement page shows every open invoice on ONE buyer account, so its
# token names an account, not a case. Minting that as a bare id would make
# the two tokens indistinguishable — verify() would happily read an account
# id as a case id and serve the wrong page, which is scope confusion, not a
# typo.
#
# So an account payload carries an explicit scope marker and is one field
# longer: "acct.<hex>.<expiry>" against a case's "<hex>.<expiry>". Both
# verifiers pin the exact field count, so each rejects the other's tokens
# without either needing to know the other exists. Same secret, same
# signature, same fail-closed-on-no-secret rule — nothing new to get wrong.
ACCOUNT_SCOPE = "acct"


def mint_account(account_id: uuid.UUID, *, ttl_hours: int | None = None) -> str | None:
    """A token for one AR account's statement page, or None if unconfigured."""
    settings = get_settings()
    secret = reveal(settings.recovery_link_secret)
    if not secret:
        return None
    # Same consent-window cap as a case token: the statement page links into
    # per-case pages the engine may no longer act on, and a statement that
    # outlived that authority would keep offering them.
    ttl_hours = min(
        ttl_hours if ttl_hours is not None else settings.recovery_link_ttl_hours,
        settings.consent_window_hours,
    )
    payload = (
        f"{ACCOUNT_SCOPE}{SEP}{account_id.hex}{SEP}{int(time.time()) + ttl_hours * 3600}"
    )
    return f"{b64(payload.encode())}{SEP}{sign(payload, secret)}"


def verify_account(token: str) -> uuid.UUID | None:
    """
    The account this token names, or None if forged, expired, malformed, or
    scoped to something else. One return value for every failure, same
    reasoning as verify().
    """
    secret = reveal(get_settings().recovery_link_secret)
    if not secret or not token or token.count(SEP) != 1:
        return None

    encoded, signature = token.split(SEP)
    try:
        payload = unb64(encoded).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None

    if not hmac.compare_digest(
        sign(payload, secret).encode("ascii"), signature.encode("utf-8", "replace")
    ):
        return None

    parts = payload.split(SEP)
    if len(parts) != 3 or parts[0] != ACCOUNT_SCOPE:
        return None
    _, account_hex, expiry = parts
    try:
        if int(expiry) < int(time.time()):
            return None
    except ValueError:
        return None
    if not re.fullmatch(r"[0-9a-f]{32}", account_hex):
        return None
    return uuid.UUID(hex=account_hex)


def url_for_account(account_id: uuid.UUID) -> str | None:
    """The absolute statement link to put in a consolidated message."""
    token = mint_account(account_id)
    if token is None:
        return None
    base = get_settings().public_base_url.rstrip("/")
    if not base:
        return None
    return f"{base}/statement/{token}"


# ── Customer scope: every case one person has open ───────────────────────
#
# The statement page above solves this for a B2B buyer, because a buyer is an
# ArAccount with an id. A consumer is not: their identity is
# `RecoveryCase.customer_id`, a canonical key that is an EMAIL or a PHONE
# ("email:a@b.in", "phone:9198…"). Putting that in a URL is exactly what this
# module's opening comment forbids — URLs end up in SMS logs, browser history
# and referrer headers.
#
# So a customer token names a CASE, and the page resolves that case's customer
# server-side. PII-free, no new table, no new secret.
#
# It is a third scope rather than a reuse of the case token on purpose: this
# link shows more than a case link does, and `mine.<hex>.<expiry>` cannot be
# produced by anyone holding only a `<hex>.<expiry>` — the signature covers the
# marker. That is the same rule the account scope exists for: a link to one
# invoice must not be swappable for a link to everything.
CUSTOMER_SCOPE = "mine"


def mint_customer(case_id: uuid.UUID, *, ttl_hours: int | None = None) -> str | None:
    """A token for the customer-home page of the person this case belongs to."""
    settings = get_settings()
    secret = reveal(settings.recovery_link_secret)
    if not secret:
        return None
    ttl_hours = min(
        ttl_hours if ttl_hours is not None else settings.recovery_link_ttl_hours,
        settings.consent_window_hours,
    )
    payload = (
        f"{CUSTOMER_SCOPE}{SEP}{case_id.hex}{SEP}{int(time.time()) + ttl_hours * 3600}"
    )
    return f"{b64(payload.encode())}{SEP}{sign(payload, secret)}"


def verify_customer(token: str) -> uuid.UUID | None:
    """
    The case whose customer this token names, or None if forged, expired,
    malformed, or scoped to something else. One return value for every
    failure, same reasoning as verify().
    """
    verified = verify_customer_with_expiry(token)
    return verified[0] if verified else None


def verify_customer_with_expiry(token: str) -> tuple[uuid.UUID, datetime] | None:
    """
    (case, expiry instant) for a valid customer token, else None.

    The page states its own real deadline for the same reason /recover does:
    an honest one outperforms a fake countdown, and it is the window we
    actually enforce.
    """
    secret = reveal(get_settings().recovery_link_secret)
    if not secret or not token or token.count(SEP) != 1:
        return None

    encoded, signature = token.split(SEP)
    try:
        payload = unb64(encoded).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None

    if not hmac.compare_digest(
        sign(payload, secret).encode("ascii"), signature.encode("utf-8", "replace")
    ):
        return None

    parts = payload.split(SEP)
    if len(parts) != 3 or parts[0] != CUSTOMER_SCOPE:
        return None
    _, case_hex, expiry = parts
    try:
        if int(expiry) < int(time.time()):
            return None
    except ValueError:
        return None
    if not re.fullmatch(r"[0-9a-f]{32}", case_hex):
        return None
    return uuid.UUID(hex=case_hex), datetime.fromtimestamp(int(expiry), tz=UTC)


def url_for_customer(case_id: uuid.UUID) -> str | None:
    """The absolute customer-home link, or None if unconfigured."""
    token = mint_customer(case_id)
    if token is None:
        return None
    base = get_settings().public_base_url.rstrip("/")
    if not base:
        return None
    return f"{base}/mine/{token}"


def url_for(case_id: uuid.UUID) -> str | None:
    """The absolute link to put in a message, or None if unconfigured."""
    token = mint(case_id)
    if token is None:
        return None
    base = get_settings().public_base_url.rstrip("/")
    if not base:
        return None
    return f"{base}/recover/{token}"
