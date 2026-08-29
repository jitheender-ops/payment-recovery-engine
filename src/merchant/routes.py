"""
The merchant's view of the recovery engine.

Everything else in this product is machinery the merchant never sees: the
webhook receiver, the agent, the guardrail, the ops console. This module is the
one surface built for the person who NEEDS the architecture, the merchant whose
revenue is leaking through failed charges, cold carts, overdue invoices and dead
mandates.

Two pages, two trust levels:

* ``GET /console`` — PUBLIC landing. What the engine recovers, the four chasers
  and the payment rail it wraps, how the architecture stays safe, and how to
  feed it (``POST /risks``). It renders product facts only (chase bounds come
  straight from ``src/chasers/policy.py``); it never touches the database and
  never shows a live number, so it is safe to serve to anyone on a public
  deployment.

* ``GET /console/live`` — the GATED console. Recovered rupees, recovery rate,
  per-chaser activity and the most recent recoveries, read live from the
  database. It is aggregate and PII-free: totals, counts and merchant-chosen
  references, never a customer email, phone or id.

Gating follows the codebase's fail-closed discipline. The live console is
protected by the same ``DASHBOARD_PASSWORD`` that gates the Streamlit ops
console (this is a single-tenant deployment: the merchant runs their own
instance, so the operator's password is the merchant's password). A signed,
expiring session cookie proves the password was entered; the signing reuses the
stdlib HMAC pattern from ``src/recovery_link.py`` rather than adding a
dependency. With ``DASHBOARD_PASSWORD`` unset the console refuses to open,
exactly like the ops console.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from src.auth import client_ip
from src.chasers.policy import RISK_POLICIES
from src.config import get_settings, reveal
from src.database import async_session_factory
from src.formatting import ist as _ist
from src.formatting import money as _money
from src.recovery_link import SEP, b64, sign, unb64

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ── Presentation helpers ────────────────────────────────────────────────────
def _window_label(hours: int) -> str:
    """A consent window a person can read: '7 days', not '168 hours'."""
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day" if days == 1 else f"{days} days"
    return f"{hours} hour" if hours == 1 else f"{hours} hours"


# ── Chaser product facts (for the public landing) ───────────────────────────
# Display copy for each recovery type, in display order (dicts keep insertion
# order). The numeric bounds are NOT written here: they are read from
# src/chasers/policy.py (and src/config.py for the payment rail) at render
# time, so the landing can never drift from the promises the engine actually
# enforces.
_CHASER_COPY: dict[str, dict[str, str]] = {
    "payment_failure": {
        "label": "Failed payment",
        "icon": "card",
        "blurb": (
            "A card or UPI charge declined at the gateway. The engine reads the "
            "decline, moves to the rail most likely to clear, and retries inside "
            "the consent window."
        ),
    },
    "checkout_abandonment": {
        "label": "Abandoned checkout",
        "icon": "cart",
        "blurb": (
            "A cart went cold before any payment was attempted. One gentle "
            "reminder with a link to finish, then one follow-up. Two touches, "
            "never a third."
        ),
    },
    "subscription_failure": {
        "label": "Failed subscription charge",
        "icon": "repeat",
        "blurb": (
            "A renewal did not go through, but the customer believes they are "
            "still subscribed. Reached before the grace period ends, on UPI to "
            "skip the OTP step."
        ),
    },
    "invoice_overdue": {
        "label": "Overdue invoice",
        "icon": "invoice",
        "blurb": (
            "A B2B invoice is past due. A slow ladder of four touches over thirty "
            "days, built around the customer's promise to pay."
        ),
    },
    "mandate_failure": {
        "label": "Failed autopay debit",
        "icon": "calendar",
        "blurb": (
            "A pre-approved mandate debit bounced. The mandate is standing consent "
            "to collect, so the charge is presented again, after a day for funds "
            "or the bank to recover."
        ),
    },
}


def _chaser_cards() -> list[dict[str, Any]]:
    """The five recovery types with their enforced bounds, for the landing."""
    settings = get_settings()
    cards: list[dict[str, Any]] = []
    for risk_type, copy in _CHASER_COPY.items():
        policy = RISK_POLICIES.get(risk_type)
        if policy is not None:
            max_attempts = policy.max_attempts
            window = _window_label(policy.consent_window_hours)
            rail = "UPI-first" if policy.recommended_rail == "upi" else "Best rail"
            source = "Merchant event"
        else:
            # The payment rail: webhook-driven, bounds live in config.
            max_attempts = settings.max_retries_per_payment
            window = _window_label(settings.consent_window_hours)
            rail = "Switches rail"
            source = "Razorpay webhook"
        cards.append(
            {
                "risk_type": risk_type,
                "label": copy["label"],
                "icon": copy["icon"],
                "blurb": copy["blurb"],
                "max_attempts": max_attempts,
                "window": window,
                "rail": rail,
                "source": source,
            }
        )
    return cards


# ── Session cookie (signed, expiring) ───────────────────────────────────────
# Same stdlib HMAC construction as src/recovery_link.py (the primitives are
# imported from there): base64(payload).sign. Keyed by DASHBOARD_PASSWORD, so
# a session is proof the password was entered, and rotating the password
# invalidates every open session. No new dependency.
_SESSION_COOKIE = "rc_session"
_SESSION_TTL_SECONDS = 12 * 3600


def _console_password() -> str:
    return reveal(get_settings().dashboard_password)


def _password_configured() -> bool:
    return bool(_console_password())


def _mint_session() -> str:
    # Only called once the password is configured and matched, so the signing
    # key is never empty here.
    payload = f"console{SEP}{int(time.time()) + _SESSION_TTL_SECONDS}"
    return f"{b64(payload.encode())}{SEP}{sign(payload, _console_password())}"


def _session_valid(request: Request) -> bool:
    """True only for an unexpired, correctly-signed session cookie."""
    secret = _console_password()
    token = request.cookies.get(_SESSION_COOKIE)
    if not secret or not token or token.count(SEP) != 1:
        return False
    encoded, signature = token.split(SEP)
    try:
        payload = unb64(encoded).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return False
    if not hmac.compare_digest(
        sign(payload, secret).encode("ascii"), signature.encode("utf-8", "replace")
    ):
        return False
    if payload.count(SEP) != 1:
        return False
    _, _, expiry = payload.partition(SEP)
    try:
        return int(expiry) >= int(time.time())
    except ValueError:
        return False


# ── Login throttling ────────────────────────────────────────────────────────
# A single shared static password with unlimited guesses falls to a script, so
# guesses are budgeted per client IP: six a minute, then the door closes for
# five. Mirrors dashboard/auth.py's lockout, adapted to per-IP buckets because
# FastAPI has a real client identity and Streamlit did not.
_LOGIN_WINDOW_SECONDS = 60.0
_LOGIN_MAX_FAILURES = 6
_LOGIN_LOCKOUT_SECONDS = 300.0
_LOGIN_FAILURES: dict[str, deque[float]] = {}
_LOGIN_LOCKED_UNTIL: dict[str, float] = {}


def _login_locked_seconds(ip: str) -> int:
    return max(0, int(_LOGIN_LOCKED_UNTIL.get(ip, 0.0) - time.monotonic()))


def _record_login_failure(ip: str) -> None:
    now = time.monotonic()
    bucket = _LOGIN_FAILURES.setdefault(ip, deque())
    while bucket and now - bucket[0] > _LOGIN_WINDOW_SECONDS:
        bucket.popleft()
    bucket.append(now)
    if len(bucket) >= _LOGIN_MAX_FAILURES:
        _LOGIN_LOCKED_UNTIL[ip] = now + _LOGIN_LOCKOUT_SECONDS
        bucket.clear()


# ── Live console data (aggregate, PII-free) ─────────────────────────────────
# One read-only session opened and closed here, not the get_session dependency:
# these are pure reads, and a console that cannot reach the database must
# degrade to an honest "not connected" state rather than a 500.
_OVERVIEW_SQL = text(
    """
    SELECT
      (SELECT COUNT(*) FROM recovery_cases)                          AS cases,
      (SELECT COUNT(*) FROM recovery_cases WHERE state='recovered')  AS recovered_cases,
      (SELECT COALESCE(SUM(amount_at_risk), 0) FROM recovery_cases)  AS at_risk_paise,
      (SELECT COALESCE(SUM(amount_recovered), 0) FROM recovery_cases) AS recovered_paise,
      (SELECT COALESCE(SUM(amount_recovered), 0) FROM recovery_cases
         WHERE recovered_via_attempt_id IS NOT NULL)                 AS attributed_paise,
      (SELECT COUNT(*) FROM retry_attempts WHERE result='pending')   AS pending,
      (SELECT COUNT(*) FROM retry_attempts WHERE result='scheduled') AS scheduled
    """
)

_CHASER_SQL = text(
    """
    SELECT risk_type,
      COUNT(*)                                            AS cases,
      COUNT(*) FILTER (WHERE state='recovered')           AS recovered,
      COALESCE(SUM(amount_at_risk), 0)                    AS at_risk,
      COALESCE(SUM(amount_recovered), 0)                  AS recovered_amt,
      COALESCE(SUM(amount_recovered)
        FILTER (WHERE recovered_via_attempt_id IS NOT NULL), 0) AS attributed
    FROM recovery_cases
    GROUP BY risk_type
    ORDER BY risk_type
    """
)

# Recent recoveries show the merchant's own reference and the amount, never the
# customer's identity: this page is aggregate by design.
_RECENT_SQL = text(
    """
    SELECT risk_type, subject_ref, amount_recovered, recovered_at
    FROM recovery_cases
    WHERE state='recovered' AND recovered_at IS NOT NULL
    ORDER BY recovered_at DESC
    LIMIT 8
    """
)


def _label_icon(risk_type: str) -> tuple[str, str]:
    """Display label and icon for a risk type, falling back to the raw name."""
    copy = _CHASER_COPY.get(risk_type, {})
    return copy.get("label", risk_type.replace("_", " ")), copy.get("icon", "card")


async def _console_data() -> dict[str, Any] | None:
    """Aggregate console numbers, or None when the database is unreachable."""
    try:
        async with async_session_factory() as session:
            overview = (await session.execute(_OVERVIEW_SQL)).mappings().one()
            chaser_rows = (await session.execute(_CHASER_SQL)).mappings().all()
            recent_rows = (await session.execute(_RECENT_SQL)).mappings().all()
    except Exception:
        logger.exception("Merchant console data query failed")
        return None

    cases = int(overview["cases"])
    recovered_cases = int(overview["recovered_cases"])

    chasers: list[dict[str, Any]] = []
    for row in chaser_rows:
        rc = int(row["cases"])
        rec = int(row["recovered"])
        label, icon = _label_icon(str(row["risk_type"]))
        chasers.append(
            {
                "risk_type": row["risk_type"],
                "label": label,
                "icon": icon,
                "cases": rc,
                "recovered": rec,
                "rate": round(rec / rc * 100, 1) if rc else 0.0,
                "at_risk": _money(int(row["at_risk"])),
                "recovered_amt": _money(int(row["recovered_amt"])),
                "attributed": _money(int(row["attributed"])),
            }
        )

    recent: list[dict[str, Any]] = []
    for row in recent_rows:
        recovered_at = row["recovered_at"]
        label, icon = _label_icon(str(row["risk_type"]))
        recent.append(
            {
                "label": label,
                "icon": icon,
                "subject_ref": row["subject_ref"],
                "amount": _money(int(row["amount_recovered"])),
                "when": (
                    _ist(recovered_at).strftime("%d %b, %H:%M")
                    if recovered_at is not None
                    else ""
                ),
            }
        )

    return {
        "cases": cases,
        "recovered_cases": recovered_cases,
        "recovery_rate": round(recovered_cases / cases * 100, 1) if cases else 0.0,
        "at_risk": _money(int(overview["at_risk_paise"])),
        "recovered": _money(int(overview["recovered_paise"])),
        "attributed": _money(int(overview["attributed_paise"])),
        "pending": int(overview["pending"]),
        "scheduled": int(overview["scheduled"]),
        "chasers": chasers,
        "recent": recent,
        "has_data": cases > 0,
    }


# ── Routes ──────────────────────────────────────────────────────────────────
@router.get("/console", response_class=HTMLResponse)
async def landing(request: Request) -> Any:
    """Public product landing. Product facts only; no database, no live numbers."""
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "merchant_name": settings.merchant_name or None,
            "chasers": _chaser_cards(),
            "authed": _session_valid(request),
        },
    )


def _login_page(request: Request, *, error: str | None = None) -> Any:
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "configured": _password_configured(),
            "error": error,
            "merchant_name": get_settings().merchant_name or None,
        },
    )


@router.get("/console/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Any:
    if _password_configured() and _session_valid(request):
        return RedirectResponse("/console/live", status_code=303)
    return _login_page(request)


@router.post("/console/login")
async def login_submit(request: Request) -> Any:
    if not _password_configured():
        return _login_page(request)

    form = await request.form()
    supplied = str(form.get("password", ""))
    ip = client_ip(request)

    wait = _login_locked_seconds(ip)
    if wait:
        return _login_page(
            request, error=f"Too many attempts. Sign-in reopens in {wait}s."
        )

    expected = _console_password()
    if supplied and hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
        _LOGIN_FAILURES.pop(ip, None)
        response = RedirectResponse("/console/live", status_code=303)
        response.set_cookie(
            _SESSION_COOKIE,
            _mint_session(),
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=get_settings().app_env == "production",
            path="/console",
        )
        return response

    _record_login_failure(ip)
    logger.warning("Failed merchant console sign-in from %s", ip)
    return _login_page(request, error="Incorrect password")


@router.post("/console/logout")
async def logout(request: Request) -> Any:
    response = RedirectResponse("/console", status_code=303)
    response.delete_cookie(_SESSION_COOKIE, path="/console")
    return response


@router.get("/console/live", response_class=HTMLResponse)
async def live_console(request: Request) -> Any:
    """The merchant's live recovery numbers. Gated, aggregate, PII-free."""
    if not _password_configured():
        return _login_page(request)
    if not _session_valid(request):
        return RedirectResponse("/console/login", status_code=303)

    data = await _console_data()
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "live.html",
        {
            "merchant_name": settings.merchant_name or None,
            "db_ok": data is not None,
            "data": data,
        },
    )
