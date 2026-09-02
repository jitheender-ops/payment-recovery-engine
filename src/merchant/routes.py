"""
The merchant's view of the recovery engine.

Everything else in this product is machinery the merchant never sees: the
webhook receiver, the agent, the guardrail, the ops console. This module is the
one surface built for the person who NEEDS the architecture, the merchant whose
revenue is leaking through failed charges, cold carts, overdue invoices and dead
mandates.

Two pages, two trust levels:

* ``GET /console`` — PUBLIC landing. What the engine recovers, the five chasers
  and the payment rail it wraps, what it does once the customer answers, how
  the architecture stays safe, and how to feed it (``POST /risks``). It renders
  product facts only (chase bounds come straight from ``src/chasers/policy.py``,
  escalation rungs from ``src/receivables/ladder.py``); it never touches the
  database and never shows a live number, so it is safe to serve to anyone on a
  public deployment.

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
from sqlalchemy import func, select

from src.auth import client_ip
from src.chasers.policy import RISK_POLICIES
from src.config import get_settings, reveal
from src.database import async_session_factory
from src.formatting import ist as _ist
from src.formatting import money as _money
from src.merchant import console_data
from src.models import RecoveryCase, RetryAttempt
from src.receivables.ladder import INVOICE_LADDER
from src.receivables.models import MerchantAlert
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


def _ladder_rungs() -> list[dict[str, Any]]:
    """The B2B escalation rungs, for the landing's 'after the link' section.

    Read from INVOICE_LADDER for the same reason the chaser bounds are read
    from RISK_POLICIES: the page states what the engine enforces, so it must
    read the enforcing structure rather than restate it.
    """
    return [
        {
            "tone": stage.tone,
            "days": stage.days_past_due,
            "addresses": _ROLE_LABELS[stage.addresses[-1]],
        }
        for stage in INVOICE_LADDER
    ]


# Contact roles as a merchant would name them, not as the schema stores them.
_ROLE_LABELS = {
    "ap_clerk": "accounts payable",
    "finance_manager": "the finance manager",
    "escalation": "their escalation contact",
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
#
# ORM CONSTRUCTS, NOT text() STRINGS. These were hand-written SQL, and the
# three bugs that produced are all the same bug: a string is not checked
# against the schema, not translated per dialect, and not type-coerced on the
# way back.
#
#   * `WHERE delivered = 0` on a Boolean column — SQLite says 0/1, Postgres
#     refuses `boolean = integer`. The alerts feed was empty in production.
#   * `avg(... max(x) ...)` in the aging module — a nested aggregate no
#     dialect accepts. It never returned a number anywhere.
#   * `SELECT ... recovered_at` through text() comes back a STRING on SQLite,
#     because a raw string carries no column type for SQLAlchemy to coerce
#     against — so date formatting blew up on the test dialect only.
#
# Written as select(Model.col), each of those is either impossible or caught
# by mypy. The FILTER clauses below render as FILTER on Postgres and a CASE on
# SQLite; SQLAlchemy owns that difference so this module does not have to.
_OVERVIEW = select(
    func.count(RecoveryCase.id),
    func.count(RecoveryCase.id).filter(RecoveryCase.state == "recovered"),
    func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0),
    func.coalesce(func.sum(RecoveryCase.amount_recovered), 0),
    func.coalesce(
        func.sum(RecoveryCase.amount_recovered).filter(
            RecoveryCase.recovered_via_attempt_id.is_not(None)
        ),
        0,
    ),
)

_CHASERS = (
    select(
        RecoveryCase.risk_type,
        func.count(RecoveryCase.id),
        func.count(RecoveryCase.id).filter(RecoveryCase.state == "recovered"),
        func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0),
        func.coalesce(func.sum(RecoveryCase.amount_recovered), 0),
        func.coalesce(
            func.sum(RecoveryCase.amount_recovered).filter(
                RecoveryCase.recovered_via_attempt_id.is_not(None)
            ),
            0,
        ),
    )
    .group_by(RecoveryCase.risk_type)
    .order_by(RecoveryCase.risk_type)
)

# Recent recoveries show the merchant's own reference and the amount, never the
# customer's identity: this page is aggregate by design.
_RECENT = (
    select(
        RecoveryCase.risk_type,
        RecoveryCase.subject_ref,
        RecoveryCase.amount_recovered,
        RecoveryCase.recovered_at,
    )
    .where(RecoveryCase.state == "recovered", RecoveryCase.recovered_at.is_not(None))
    .order_by(RecoveryCase.recovered_at.desc())
    .limit(8)
)

# "Blocked" is every guardrail rejection (budget, blackout, amount ceiling,
# ...). "Compliance blocks" is the subset citing a specific regulatory clause
# (currently just the RBI e-mandate rule) — a narrower, stronger claim than
# "blocked", so it is counted separately rather than inferred from the
# total. Named "blocks", not "violations": these are attempts the engine
# PREVENTED before execution — a violation that reached a customer would be a
# bug, and a counter that could read as "N violations happened" inverts what
# the number proves. Both read straight off RetryAttempt, no new table.
_GUARDRAIL = select(
    func.count(RetryAttempt.id).filter(RetryAttempt.result == "rejected"),
    func.count(RetryAttempt.id).filter(
        RetryAttempt.result == "rejected",
        RetryAttempt.guardrail_rejection_reason.like("%RBI%"),
    ),
)

# Cases the engine has genuinely given up on — attempt budget spent, still
# open, never recovered — surfaced honestly instead of quietly aging off the
# dashboard. Mirrors cases.stop_reason()'s "attempt budget spent" branch.
_EXCEPTIONS = (
    select(
        RecoveryCase.risk_type,
        RecoveryCase.subject_ref,
        RecoveryCase.amount_at_risk,
        RecoveryCase.attempts_used,
        RecoveryCase.max_attempts,
    )
    .where(
        RecoveryCase.state.not_in(
            ["recovered", "exhausted", "abandoned", "expired", "opted_out"]
        ),
        RecoveryCase.attempts_used >= RecoveryCase.max_attempts,
    )
    .order_by(RecoveryCase.opened_at.desc())
    .limit(10)
)

# In-flight attempt counts. Separate from _OVERVIEW because they read a
# different table — the old single statement stitched both together with
# scalar subqueries, which is what made it a string in the first place.
_INFLIGHT = select(
    func.count(RetryAttempt.id).filter(RetryAttempt.result == "pending"),
    func.count(RetryAttempt.id).filter(RetryAttempt.result == "scheduled"),
)


def _label_icon(risk_type: str) -> tuple[str, str]:
    """Display label and icon for a risk type, falling back to the raw name."""
    copy = _CHASER_COPY.get(risk_type, {})
    return copy.get("label", risk_type.replace("_", " ")), copy.get("icon", "card")


async def _console_data() -> dict[str, Any] | None:
    """Aggregate console numbers, or None when the database is unreachable."""
    # Every query runs inside ONE session block. The receivables reads used to
    # sit after it, using `session` once the context manager had already closed
    # it — each wrapped in its own try/except, so the panel degraded to empty
    # instead of failing loudly. A console that silently renders zeros is worse
    # than one that says "not connected": the merchant reads the zeros as their
    # business, not as a bug.
    ar_aging: list[dict[str, Any]] = []
    days_to_pay: float | None = None
    promise_stats: dict[str, Any] = {"kept_rate": None}
    alerts: list[dict[str, Any]] = []

    try:
        async with async_session_factory() as session:
            (
                cases,
                recovered_cases,
                at_risk_paise,
                recovered_paise,
                attributed_paise,
            ) = (await session.execute(_OVERVIEW)).one()
            chaser_rows = (await session.execute(_CHASERS)).all()
            recent_rows = (await session.execute(_RECENT)).all()
            actions_blocked, compliance_blocks = (
                await session.execute(_GUARDRAIL)
            ).one()
            exception_rows = (await session.execute(_EXCEPTIONS)).all()
            pending, scheduled = (await session.execute(_INFLIGHT)).one()

            # ── The receivables panel (B2B layer) ────────────────────────
            # Aging buckets, days-to-pay and promise effectiveness from the
            # receivables layer's analytics; the alerts feed is the merchant's
            # undelivered writeback queue. Aggregate and PII-free throughout:
            # refs and amounts, never a customer email or phone.
            from src.receivables import aging as ar_aging_mod

            for bucket in await ar_aging_mod.aging_buckets(session):
                ar_aging.append(
                    {
                        "label": bucket["label"],
                        "count": bucket["count"],
                        "outstanding": _money(int(bucket["outstanding_paise"])),
                    }
                )
            days_to_pay = await ar_aging_mod.avg_days_to_pay(session)
            promise_stats = await ar_aging_mod.promise_effectiveness(session)

            # The features that shipped and were never surfaced. Each is one
            # indexed read; they run in this same session block so a database
            # that goes away mid-page fails the whole console honestly rather
            # than rendering half a truth.
            promises = await console_data.promise_panel(session)
            plans = await console_data.plan_panel(session)
            disputes = await console_data.dispute_panel(session)
            voice = await console_data.voice_panel(session)
            ladder = await console_data.ladder_panel(session)
            health = await console_data.engine_health(session)
            outstanding = await console_data.outstanding_total(session)
            flight = await console_data.in_flight(session)
            activity = await console_data.activity_feed(session)
            stopping = await console_data.stopping_rules(session)

            alert_rows = (
                await session.execute(
                    select(
                        MerchantAlert.event_type,
                        MerchantAlert.account_ref,
                        MerchantAlert.case_ref,
                        MerchantAlert.created_at,
                    )
                    # `delivered.is_(False)`, not raw `delivered = 0`. The
                    # column is Boolean: SQLite accepts the integer compare and
                    # Postgres refuses it outright ("operator does not exist:
                    # boolean = integer"), so the alerts feed worked in tests
                    # and was permanently empty in production — swallowed by
                    # the old per-query except.
                    .where(MerchantAlert.delivered.is_(False))
                    .order_by(MerchantAlert.created_at.desc())
                    .limit(10)
                )
            ).mappings().all()
            for row in alert_rows:
                alerts.append(
                    {
                        "event_type": row["event_type"],
                        "account_ref": row["account_ref"],
                        "case_ref": row["case_ref"],
                        "when": (
                            _ist(row["created_at"]).strftime("%d %b, %H:%M")
                            if row["created_at"] is not None
                            else ""
                        ),
                    }
                )
    except Exception:
        logger.exception("Merchant console data query failed")
        return None

    cases = int(cases)
    recovered_cases = int(recovered_cases)

    chasers: list[dict[str, Any]] = []
    for risk_type, rc, rec, at_risk, recovered_amt, attributed in chaser_rows:
        rc, rec = int(rc), int(rec)
        label, icon = _label_icon(str(risk_type))
        chasers.append(
            {
                "risk_type": risk_type,
                "label": label,
                "icon": icon,
                "cases": rc,
                "recovered": rec,
                "rate": round(rec / rc * 100, 1) if rc else 0.0,
                "at_risk": _money(int(at_risk)),
                "recovered_amt": _money(int(recovered_amt)),
                "attributed": _money(int(attributed)),
            }
        )

    recent: list[dict[str, Any]] = []
    for risk_type, subject_ref, amount, recovered_at in recent_rows:
        label, icon = _label_icon(str(risk_type))
        recent.append(
            {
                "label": label,
                "icon": icon,
                "subject_ref": subject_ref,
                "amount": _money(int(amount)),
                "when": (
                    _ist(recovered_at).strftime("%d %b, %H:%M")
                    if recovered_at is not None
                    else ""
                ),
            }
        )

    exceptions: list[dict[str, Any]] = []
    for risk_type, subject_ref, at_risk, used, allowed in exception_rows:
        label, icon = _label_icon(str(risk_type))
        exceptions.append(
            {
                "label": label,
                "icon": icon,
                "subject_ref": subject_ref,
                "at_risk": _money(int(at_risk)),
                "attempts_used": int(used),
                "max_attempts": int(allowed),
                "reason": f"attempt budget spent ({used}/{allowed})",
            }
        )

    return {
        "cases": cases,
        "recovered_cases": recovered_cases,
        "recovery_rate": round(recovered_cases / cases * 100, 1) if cases else 0.0,
        "at_risk": _money(int(at_risk_paise)),
        "recovered": _money(int(recovered_paise)),
        "attributed": _money(int(attributed_paise)),
        "pending": int(pending),
        "scheduled": int(scheduled),
        "chasers": chasers,
        "recent": recent,
        "has_data": cases > 0,
        # Scoreboard honesty: what the policy engine actually refused, not
        # just what it approved. "0 compliance blocks" only means something
        # because this number is a live query, not a claim.
        "actions_blocked": int(actions_blocked),
        "compliance_blocks": int(compliance_blocks),
        "exceptions": exceptions,
        # The B2B receivables panel: aging, days-to-pay, promise
        # effectiveness, and the merchant's undelivered alerts feed — all
        # from the receivables layer, all PII-free (refs and amounts only).
        "ar_aging": ar_aging,
        "ar_days_to_pay": days_to_pay,
        "ar_promise": promise_stats,
        "ar_alerts": alerts,
        # ── Previously invisible ─────────────────────────────────────────
        # Promises, plans, disputes, the voice queue and the dunning ladder
        # all shipped with tables, sweeps and tests, and none of them had a
        # merchant-facing surface: the console read three tables out of a
        # dozen. src/merchant/console_data.py is the read layer.
        "promises": promises,
        "plans": plans,
        "disputes": disputes,
        "voice": voice,
        "ladder": ladder,
        # Is the engine ticking at all — the question every number above
        # silently assumes a "yes" to.
        "health": health,
        # The BALANCE, not the opening figure. See console_data.
        "outstanding": outstanding,
        "flight": flight,
        "activity": activity,
        "stopping": stopping,
        # The worklist, assembled from the panels above rather than re-read:
        # everything the engine deliberately stopped short of and cannot
        # resolve without a person. Empty is a real answer the page states.
        "attention": console_data.attention_items(
            disputes=disputes,
            voice=voice,
            plans=plans,
            exceptions=exceptions,
            health=health,
        ),
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
            "ladder_rungs": _ladder_rungs(),
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


# ── The rest of the console ─────────────────────────────────────────────────
# These five pages used to be a SEPARATE Streamlit service. Two consoles meant
# two hosts, two passwords and two cold starts, and the operator one sat
# silently broken because nobody had a reason to open it. They are ordinary
# routes now: same app, same session cookie, same PII-free contract.


def _gate(request: Request) -> Any | None:
    """Shared entry check. Returns a response to send, or None to continue."""
    if not _password_configured():
        return _login_page(request)
    if not _session_valid(request):
        return RedirectResponse("/console/login", status_code=303)
    return None


def _eval_summary() -> dict[str, Any]:
    """
    The eval harness's own output, read from disk. Never computed here.

    The numbers on that page are a claim about whether the agent beats a fixed
    baseline; recomputing them in a web request would make the page the source
    of truth for its own marking. It reads the file the harness wrote or says
    there isn't one.
    """
    import json
    from pathlib import Path

    results = Path("eval/results/eval_results.json")
    try:
        with results.open() as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"available": False}

    policies = raw.get("policies")
    if not isinstance(policies, dict) or not policies:
        return {"available": False}

    rows: list[dict[str, Any]] = []
    for name, m in policies.items():
        if not isinstance(m, dict):
            continue
        rows.append({
            "name": name,
            "recovery_raw": float(m.get("recovery_rate_%") or 0.0),
            "recovery": round(float(m.get("recovery_rate_%") or 0.0), 2),
            "recovery_std": round(float(m.get("recovery_rate_%_std") or 0.0), 2) or None,
            "attempts": round(float(m.get("retry_cost_avg") or 0.0), 2),
            "false_retry": round(float(m.get("false_retry_rate_%") or 0.0), 2),
            "net": _money(int(float(m.get("net_₹_per_₹1Cr_failed") or 0.0) * 100)),
            "net_raw": float(m.get("net_₹_per_₹1Cr_failed") or 0.0),
        })
    if not rows:
        return {"available": False}

    rows.sort(key=lambda r: r["net_raw"], reverse=True)
    best = max(r["net_raw"] for r in rows)
    for r in rows:
        r["best"] = r["net_raw"] == best and best > 0

    # The paired comparison, which is the part that makes any of this a
    # claim rather than a number. Each scenario is run under every policy
    # with the identical random sequence, so outcomes are differenced
    # one-to-one and a difference is only called real when the 95% CI
    # excludes zero. The page showed the per-policy rates and dropped this
    # entirely — the rates alone cannot tell you whether the gap is signal.
    paired: list[dict[str, Any]] = []
    for name, metrics in (raw.get("paired_vs_baseline") or {}).items():
        if not isinstance(metrics, dict):
            continue
        rr = metrics.get("recovery_rate_pp") or {}
        cost = metrics.get("retry_cost") or {}
        ci = rr.get("ci95") or [None, None]
        if rr.get("mean_delta") is None:
            continue
        paired.append({
            "name": name,
            "delta_pp": round(float(rr["mean_delta"]), 2),
            "ci_low": round(float(ci[0]), 2) if ci[0] is not None else None,
            "ci_high": round(float(ci[1]), 2) if ci[1] is not None else None,
            "n": int(rr.get("n_paired") or 0),
            "significant": bool(rr.get("significant")),
            "attempts_delta": (
                round(float(cost["mean_delta"]), 2)
                if cost.get("mean_delta") is not None else None
            ),
        })
    paired.sort(key=lambda r: r["delta_pp"], reverse=True)

    # Retry economics. break_even is None when a policy wins even if a retry
    # is free — which is a stronger statement than any particular cost
    # assumption, and the reason the harness reports it that way.
    economics: list[dict[str, Any]] = []
    for name, e in (raw.get("economics_vs_baseline") or {}).items():
        if not isinstance(e, dict):
            continue
        economics.append({
            "name": name,
            "delta_revenue": _money(
                int(float(e.get("delta_revenue_per_crore") or 0.0) * 100)
            ),
            "delta_attempts": int(float(e.get("delta_attempts_per_crore") or 0.0)),
            "break_even": e.get("break_even_cost_per_retry_inr"),
            "verdict": e.get("verdict") or "—",
        })

    return {
        "available": True,
        "policies": rows,
        "max_recovery": max(r["recovery_raw"] for r in rows) or 1.0,
        "retry_cost": raw.get("retry_cost_inr", "—"),
        "paired": paired,
        "economics": economics,
        "n_paired": max((p["n"] for p in paired), default=0),
    }


async def _render_console(
    request: Request, template: str, build: Any, **extra: Any
) -> Any:
    """One shape for every console page: gate, read, render, and stay up.

    A failure here renders the page with `db_ok=False` rather than a 500: a
    console that cannot read is a fact the merchant needs stated, not a stack
    trace.
    """
    gated = _gate(request)
    if gated is not None:
        return gated

    data: dict[str, Any] = {}
    db_ok = True
    try:
        async with async_session_factory() as session:
            data = await build(session)
    except Exception:
        logger.exception("Console page %s could not read the database", template)
        db_ok = False

    return templates.TemplateResponse(
        request, template,
        {
            "merchant_name": get_settings().merchant_name or None,
            "db_ok": db_ok, "data": data, **extra,
        },
    )


@router.get("/console/pipeline", response_class=HTMLResponse)
async def console_pipeline(request: Request) -> Any:
    """Where money leaves the pipeline, and what the gateway blamed."""

    async def build(session: Any) -> dict[str, Any]:
        return {
            "funnel": await console_data.pipeline_funnel(session),
            "causes": await console_data.failure_causes(session),
            "min_sample": console_data.FAILURE_CLASS_MIN_SAMPLE,
        }

    return await _render_console(request, "console_pipeline.html", build)


@router.get("/console/routing", response_class=HTMLResponse)
async def console_routing(request: Request) -> Any:
    """Which bank, on which rail — the evidence behind switch_rail."""

    async def build(session: Any) -> dict[str, Any]:
        return {"routing": await console_data.routing_panel(session)}

    return await _render_console(request, "console_routing.html", build)


@router.get("/console/cases", response_class=HTMLResponse)
async def console_cases(request: Request) -> Any:
    """Every case, filterable by state. References are the merchant's own."""
    state = request.query_params.get("state", "all")
    if state != "all" and state not in {
        "open", "recovered", "exhausted", "abandoned", "expired", "opted_out",
    }:
        state = "all"

    async def build(session: Any) -> dict[str, Any]:
        return {
            "cases": await console_data.case_list(session, state=state),
            "states": await console_data.case_states(session),
        }

    return await _render_console(request, "console_cases.html", build, state=state)


@router.get("/console/case/{case_id}", response_class=HTMLResponse)
async def console_case(request: Request, case_id: str) -> Any:
    """One case as the whole decision chain, over its audit trail."""

    async def build(session: Any) -> dict[str, Any]:
        return {"case": await console_data.case_detail(session, case_id)}

    return await _render_console(request, "console_case.html", build)


@router.get("/console/ops", response_class=HTMLResponse)
async def console_ops(request: Request) -> Any:
    """Is the machinery running — sweeps, heartbeat, and what fires next."""

    async def build(session: Any) -> dict[str, Any]:
        return {
            "ops": await console_data.operations_panel(session),
            "health": await console_data.engine_health(session),
        }

    return await _render_console(request, "console_ops.html", build)


@router.get("/console/evidence", response_class=HTMLResponse)
async def console_evidence(request: Request) -> Any:
    """The eval harness's verdict. Read from disk, never recomputed here."""
    gated = _gate(request)
    if gated is not None:
        return gated
    return templates.TemplateResponse(
        request, "console_evidence.html",
        {
            "merchant_name": get_settings().merchant_name or None,
            "db_ok": True,
            "data": {"eval": _eval_summary()},
        },
    )
