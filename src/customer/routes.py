"""
The page a customer lands on after their payment failed.

This is the only surface in the product a paying customer ever sees. Everything
else — the webhook receiver, the agent, the ops console — is machinery they
never touch, and until now the entire customer experience of "recovery" was a
bare Razorpay payment link arriving by SMS with no explanation attached. Someone
who just watched a payment fail got a link asking for money again and nothing
telling them whether the first attempt had taken it.

Three rules hold this file together:

1. THE SERVER DECIDES THE STATE. Never the page, never the URL, never "the
   payment window closed". The state rendered here is read from recovery_cases
   on every request, and that row is only ever moved by the payment.captured
   webhook. A customer who reloads mid-confirmation sees "still confirming",
   which is the truth, rather than a guess in either direction.

2. NEVER OFFER A SECOND PAYMENT WHILE THE FIRST MIGHT BE ALIVE. The pay route
   re-reads the case before doing anything, refuses outright once the case is
   recovered, and reuses an existing payment link rather than minting a new one.
   A double-tap on a bad connection must not become two charges.

3. SAY NOTHING WE DO NOT KNOW. Where a status is genuinely unknown, the page
   says so and gives a way forward, instead of picking the optimistic reading.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# HTTPException is re-exported by this import even though the limiter that
# raised it moved to src/rate_limit.py: the route handlers' tests (and the
# /pay flows) reference it through this module, and the 429 their callers
# see is this surface's contract.
from fastapi import APIRouter, Depends, HTTPException, Request  # noqa: F401
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src import rate_limit, recovery_link
from src.agent.actions import PaymentRail
from src.auth import client_ip
from src.cases import ledger_keys
from src.config import get_settings
from src.customer.explain import explain
from src.customer.i18n import Translator, pick
from src.database import get_session
from src.formatting import ist as _ist
from src.formatting import money as _money
from src.models import (
    PaymentFailure,
    PromiseToPay,
    RecoveryCase,
    RetryAttempt,
    VoiceCallQueue,
)
from src.receivables.models import (
    ArAccount,
    CaseDispute,
    PaymentPlan,
    PlanInstalment,
)
from src.receivables.plans import MAX_INSTALMENTS

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# How long an attempt may sit unresolved before "we are confirming" stops being
# a fair thing to say. Razorpay's capture webhook normally lands in seconds; a
# quarter of an hour means something is wrong, and the customer deserves the
# honest "we don't know yet" rather than a spinner forever.
CONFIRMING_WINDOW = timedelta(minutes=15)

# Failure classes where UPI is the honest recommendation. The research is
# blunt about why: the OTP step is India's biggest drop-off, and UPI skips it.
# The page makes UPI the primary button's verb and the /pay route mints a
# UPI-ONLY link (upi_link=True) — the recommendation is enforced, not
# decorative. Everything else stays a generic link payable by anything.
_UPI_RECOMMENDED_CLASSES = {
    "3ds_dropoff",
    "risk_check_failed",
    "card_limit_exceeded",
    "issuer_decline",
    "insufficient_funds",
    "invalid_card",
    "expired_instrument",
}


def _recommended_rail(failure_class: str | None) -> PaymentRail | None:
    return "upi" if (failure_class or "") in _UPI_RECOMMENDED_CLASSES else None


# The hero's first line, per risk type. Three of the four chaser-driven types
# never attempted a payment, so "About your payment to" would be a lie on the
# first line the customer reads. payment_failure (and anything unknown) keeps
# the original label.
_HERO_KEY_BY_RISK = {
    "checkout_abandonment": "hero_about_order",
    "subscription_failure": "hero_about_subscription",
    "invoice_overdue": "hero_about_invoice",
    "mandate_failure": "hero_about_mandate",
}

# The timeline's "what happened" row, per risk type. The payment rail builds
# its own from the gateway decline; these never had a gateway decline to quote.
_TIMELINE_KEY_BY_RISK = {
    "checkout_abandonment": "timeline_risk_order",
    "subscription_failure": "timeline_risk_subscription",
    "invoice_overdue": "timeline_risk_invoice",
    "mandate_failure": "timeline_risk_mandate",
}


def _format_expiry(expires_at: datetime, lang: str) -> str:
    """'Sat, 28 Aug, 11 AM' in IST — honest urgency, not a fake countdown."""
    local = _ist(expires_at)
    if lang == "hi":
        return local.strftime("%a, %d %b · %I:%M %p")
    return local.strftime("%a, %d %b, %I:%M %p")


def _format_promise_due(due_at: datetime) -> str:
    """'28 Aug' in IST — honest short format for the promise line."""
    return _ist(due_at).strftime("%d %b")


def _aging(due_at: datetime | None) -> tuple[str | None, int | None]:
    """
    ('04 Sep 2026', days-overdue-or-None) for an invoice's due date.

    The ladder and the merchant console have both computed days-past-due
    since the receivables layer landed; the customer looking at the invoice
    was the only one who could not see it. Days overdue is None before the
    date passes — a not-yet-due invoice must never render "0 days overdue"
    or, worse, a negative one dressed up as urgency.
    """
    if due_at is None:
        return None, None
    due = _ist(_aware(due_at))
    # Both sides in IST: the reader is in India, and comparing a UTC "today"
    # against an IST due date flips the answer for five and a half hours a day.
    overdue = (_ist(datetime.now(UTC)).date() - due.date()).days
    return due.strftime("%d %b %Y"), (overdue if overdue > 0 else None)

# ── Rate limiting ───────────────────────────────────────────────────────────
# This is the one public, unauthenticated surface in the product (the token in
# the URL is the credential), and until now nothing throttled it: token
# guessing, /pay hammering and slow-loris loops were all free. A fixed window
# per client IP, in-process when REDIS_URL is unset — exactly right for the
# deployment this ships on (one uvicorn worker; render.yaml pins
# WEB_CONCURRENCY=1) — and shared across workers via Redis the moment the
# API tier goes multi-replica (src/rate_limit.py owns that switch).
# Two limits because they protect different things: page views are cheap
# reads, /pay mints Razorpay objects.
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_PAGE_LIMIT = 30          # page views per window per IP
_PAY_LIMIT = 6            # payment starts per window per IP — link creation is the expensive call
# Kept as an alias for the tests that clear it between cases; the buckets
# themselves now live in src/rate_limit.py (one store, one GC pass).
_RATE_LIMIT_BUCKETS = rate_limit._BUCKETS


def _check_rate_limit(request: Request, *, kind: str, limit: int) -> None:
    """Raise 429 once an IP exhausts its window budget for this kind of call."""
    rate_limit.check(
        f"{kind}:{client_ip(request)}", limit,
        window=_RATE_LIMIT_WINDOW_SECONDS,
    )


async def _load(
    session: AsyncSession, token: str
) -> tuple[Any, Any, Any, str | None] | None:
    """(case, failure, blocking_attempt, live_link) for a valid token, else None."""
    verified = recovery_link.verify_with_expiry(token)
    if verified is None:
        return None
    case_id, _expires_at = verified
    case = await session.get(RecoveryCase, case_id)
    if case is None:
        return None

    # Only the payment rail has a gateway failure row behind it. Chaser-driven
    # cases skip the lookup entirely — it could only return None — except it
    # could not: subject_ref is merchant-chosen for those, and a reference_id
    # that collides with a real payment id would otherwise drag an unrelated
    # failure onto the page and render the payment rail's story for a cart.
    failure = None
    if case.risk_type == "payment_failure":
        failure = (
            await session.execute(
                select(PaymentFailure).where(PaymentFailure.payment_id == case.subject_ref)
            )
        ).scalars().first()
    # ALL of the case's attempts, not just the newest. Reading only the newest
    # row was a duplicate-payment hole in both directions: a newer `scheduled`
    # or nudge row hid an older `pending` one, so the "never offer a second
    # payment while the first might be alive" guard stopped firing; and it hid
    # an older live link, so /pay minted a SECOND payable link instead of
    # reusing the one the customer already has. The case's budget bounds this
    # list to a handful of rows.
    attempts = (
        await session.execute(
            select(RetryAttempt)
            .where(RetryAttempt.recovery_case_id == case.id)
            .order_by(desc(RetryAttempt.created_at))
            .limit(_ATTEMPT_SCAN_LIMIT)
        )
    ).scalars().all()
    return case, failure, _blocking_attempt(attempts), _live_link(attempts)


def _aware(ts: datetime) -> datetime:
    """
    Force a timestamp to UTC-aware before it meets datetime.now(UTC).

    The columns are DateTime(timezone=True), so Postgres hands these back
    aware — but SQLite does not, and neither does a value that has been through
    a driver that drops the zone. Subtracting the two kinds raises TypeError,
    and it would raise in the "confirming" branch specifically, which is the one
    branch whose whole job is preventing a second charge. A crash there fails
    open into a page that offers to pay again.
    """
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _view_state(
    case: Any,
    attempt: Any,
    retryable: bool,
    failed_at: datetime | None = None,
    window_hours: int | None = None,
) -> str:
    """
    The single customer-visible state. Derived here so the template branches
    once and the ordering of these checks is reviewable in one place.

    `window_hours` overrides the global consent window for chaser-driven risk
    types, whose windows are per-type (src/chasers/policy.py) — a cold cart is
    stale in two days, a receivable is chaseable for a month. None keeps the
    global setting, which is every payment-failure page's behaviour.
    """
    if case.state == "recovered" or case.amount_recovered >= case.amount_at_risk:
        return "recovered"
    if case.state == "opted_out":
        return "opted_out"
    # An attempt that went out and has not resolved. Checked BEFORE the
    # payable branch: while this is true, offering another payment is how a
    # customer ends up paying twice.
    if attempt is not None and attempt.result == "pending":
        age = datetime.now(UTC) - _aware(attempt.executed_at or attempt.created_at)
        return "confirming" if age < CONFIRMING_WINDOW else "unknown"
    if case.state in ("exhausted", "abandoned", "expired"):
        return "stopped"
    # Backstop for the window passing. Chaser-driven cases now close as
    # "expired" when the sweep meets them past their window (chase_case), but
    # a case can still cross the line without being touched again — the
    # scheduler down, a payment-rail case anchored to failed_at with no
    # re-trigger. The page must not offer payment in that gap: minting a link
    # past the window is acting without authority (the /pay guardrail refuses
    # it anyway; this stops the page offering a button that only produces an
    # error).
    if failed_at is not None:
        hours = window_hours or get_settings().consent_window_hours
        window = timedelta(hours=hours)
        if datetime.now(UTC) > _aware(failed_at) + window:
            return "stopped"
    if not retryable:
        return "not_retryable"
    return "payable"


# How far back to scan a case's attempts. The case's own budget
# (max_attempts) bounds the real number to single digits; this is only a
# guard against a pathological row count dragging the page's one query.
_ATTEMPT_SCAN_LIMIT = 20


def _blocking_attempt(attempts: Sequence[Any]) -> Any | None:
    """
    The newest attempt still in flight, across every attempt on the case.

    This is what stops a second charge. It must NOT be "the newest attempt,
    if that one happens to be pending": the engine parks `scheduled` rows and
    records nudges, and either of those arriving after a live retry hid the
    pending row that was the whole reason to refuse.
    """
    return next((a for a in attempts if a.result == "pending"), None)


def _live_link(attempts: Sequence[Any]) -> str | None:
    """
    The newest payment link this case has already handed out, if one is live.

    Cancelled links are skipped — the scheduler stamps `link_cancelled_at`
    once Razorpay has confirmed a link inert, and sending a customer back to
    a dead link is worse than minting a fresh one.
    """
    for attempt in attempts:
        details = attempt.result_details
        if not isinstance(details, dict) or details.get("link_cancelled_at"):
            continue
        url = details.get("short_url")
        if url:
            return str(url)
    return None


# ── Payment redirect validation ─────────────────────────────────────────────
# /pay ends in a redirect to the payment object the customer is about to hand
# money to. That target is read from result_details.short_url — JSONB this
# service wrote from a Razorpay response — and from the executor's return
# value. Neither is something the customer typed, but neither is inside the
# trust boundary of THIS request: a poisoned row (SQL elsewhere, a compromised
# upstream, a malicious response stored by a bug) would otherwise turn a
# genuine recovery link into a 303 to a phishing page that wears Razorpay's
# face. On a page whose entire job is "ask this person for money", an open
# redirect is the phishing page writing itself. So the target is validated at
# the one place every pay-path redirect funnels through: https only, and the
# host must BE a Razorpay property or a subdomain of one. Anything else is
# refused back to the recovery page with the friendly error, loudly logged.
#
# Allowlist, not blocklist: a blocklist of "evil" hosts is never complete, and
# the set of places a real Razorpay payment object lives is small and known —
# rzp.io is where Payment Links resolve, *.razorpay.com is everything else on
# their side. endswith("." + host) matches subdomains only, so
# "razorpay.com.evil.in" and "evilrazorpay.com" both fail the check.
_PAYMENT_REDIRECT_HOSTS = ("razorpay.com", "rzp.io")


def _is_demo_redirect_target(url: str) -> bool:
    """
    True only for this process's own demo checkout, and only in demo mode.

    Demo mode replaces Razorpay with a local fake, so its short_url is on
    localhost over http and the allowlist above correctly refuses it. This
    is the narrowest possible exception to that, and every clause is load-
    bearing: demo_mode is itself refused outside development
    (Settings._demo_mode_is_development_only), the URL must sit on exactly
    the configured PUBLIC_BASE_URL origin — scheme, host AND port — and the
    path must be under /demo/checkout/, which is the only place the fake
    ever points. A poisoned short_url therefore still cannot redirect
    anywhere, in demo mode or out of it.
    """
    settings = get_settings()
    if not settings.demo_mode:
        return False
    base = urlparse(settings.public_base_url)
    try:
        parsed = urlparse(url)
        # .port is parsed lazily ON ACCESS and raises ValueError for a
        # non-numeric one, so it has to be read inside this try — otherwise
        # "http://127.0.0.1:8000.evil.example/..." escapes as a 500 from the
        # pay route instead of a clean refusal. Same for base.
        candidate = (parsed.scheme, (parsed.hostname or "").lower(), parsed.port)
        expected = (base.scheme, (base.hostname or "").lower(), base.port)
    except (ValueError, TypeError):
        return False
    return (
        bool(base.hostname)
        and candidate == expected
        and parsed.path.startswith("/demo/checkout/")
    )


def _is_payment_redirect_target(url: str) -> bool:
    """True only for an https URL on a Razorpay host (or subdomain)."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except (ValueError, TypeError):
        return False
    if parsed.scheme != "https" or not host:
        return False
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in _PAYMENT_REDIRECT_HOSTS
    )


def _payment_redirect(url: str, token: str) -> RedirectResponse:
    """
    Redirect the customer to the payment object, or refuse a non-Razorpay target.

    Every pay-path redirect (reused live link AND freshly minted short_url)
    goes through here. A target that fails the allowlist is not followed: the
    customer is sent back to the recovery page with ?error=1 (the "we couldn't
    open the payment page" note) instead of being shipped to an attacker host.
    """
    if _is_payment_redirect_target(url) or _is_demo_redirect_target(url):
        return RedirectResponse(url, status_code=303)
    logger.error(
        "Refusing pay redirect to non-Razorpay target %r — possible poisoned "
        "short_url; sending the customer back with an error instead",
        url,
    )
    return RedirectResponse(f"/recover/{token}?error=1", status_code=303)


@router.get("/recover/{token}", response_class=HTMLResponse)
async def recovery_page(
    request: Request, token: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """
    Show one customer their own failed payment and what to do about it.

    The reader's order of anxiety sets the page order: is my money gone →
    what happened → what do I do. Everything on the page is read from the
    database on this request; nothing is inferred from the URL beyond the
    token that names the case.
    """
    _check_rate_limit(request, kind="page", limit=_PAGE_LIMIT)

    settings = get_settings()
    lang = pick(request.query_params.get("lang"), request.headers.get("accept-language"))
    t = Translator(lang)

    loaded = await _load(session, token)
    if loaded is None:
        # One response for expired, forged and unknown alike. Distinguishing
        # them tells someone probing the URL which guesses are getting warmer.
        return templates.TemplateResponse(
            request, "expired.html",
            {"t": t, "lang": lang, "merchant_name": settings.merchant_name},
            status_code=404,
        )

    case, failure, attempt, live_link = loaded

    # The token's REAL deadline, surfaced as honest urgency: it is the exact
    # instant the consent window closes and the link stops working. No fake
    # countdown anywhere on this page.
    verified = recovery_link.verify_with_expiry(token)
    expires_at = verified[1] if verified else None

    if failure is not None:
        # The payment rail: a gateway failure class drives the explanation,
        # the window anchor is the failure instant, and the rail recommendation
        # comes from the failure class.
        detail = explain(failure.failure_class)
        anchor: datetime | None = failure.failed_at
        window_hours: int | None = None
        recommended_rail = _recommended_rail(failure.failure_class)
        hero_key = "hero_about"
        is_payment = True
        risk_timeline_key = None
        cart_items = None
        sequence_attempts: list[dict[str, str]] = []
    else:
        # A chaser-driven risk type: no payment was (necessarily) attempted,
        # so the risk type names what happened, the window anchor is when WE
        # opened the case, and both the window and the rail come from the
        # type's chase policy.
        from src.chasers.policy import policy_for

        policy = policy_for(case.risk_type)
        detail = explain(None, risk_type=case.risk_type)
        anchor = _aware(case.opened_at)
        window_hours = policy.consent_window_hours if policy else None
        recommended_rail = policy.recommended_rail if policy else None
        hero_key = _HERO_KEY_BY_RISK.get(case.risk_type, "hero_about")
        is_payment = False
        risk_timeline_key = _TIMELINE_KEY_BY_RISK.get(case.risk_type)
        # Carts only: name what was left in it, from the merchant's own event.
        # Same reduction discipline as the nudge path (orchestrator.
        # cart_summary_from_meta) — the value renders on a money page.
        cart_items = None
        if case.risk_type == "checkout_abandonment":
            from src.models import RiskEvent as _RiskEvent
            from src.orchestrator import cart_summary_from_meta

            event_row = (
                await session.execute(
                    select(_RiskEvent.meta)
                    .where(
                        _RiskEvent.risk_type == case.risk_type,
                        _RiskEvent.reference_id == case.subject_ref,
                    )
                    .order_by(_RiskEvent.received_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            cart_items = cart_summary_from_meta(event_row)

        # Subscription/mandate only: the retry sequence — past collection
        # touches actually made, one row per executed attempt. Never a
        # decision that was only made (rejected/pending/skipped never
        # reached the customer) — executed_at is the one honest gate for
        # "something actually happened here". Pre-formatted here (not raw
        # ORM rows) so the template never has to reach for an IST converter
        # of its own — same discipline as recovered_at below.
        sequence_attempts = []
        if case.risk_type in ("subscription_failure", "mandate_failure"):
            rows = (
                await session.execute(
                    select(RetryAttempt)
                    .where(
                        RetryAttempt.recovery_case_id == case.id,
                        RetryAttempt.executed_at.isnot(None),
                    )
                    .order_by(RetryAttempt.executed_at)
                )
            ).scalars()
            sequence_attempts = [
                {
                    "when": _ist(a.executed_at).strftime("%d %b, %H:%M"),
                    "action_type": a.action_type,
                }
                for a in rows
                if a.executed_at is not None
            ]

    due_when, days_overdue = (
        _aging(case.due_at) if case.risk_type == "invoice_overdue" else (None, None)
    )

    state = _view_state(
        case, attempt, detail.retryable,
        failed_at=anchor, window_hours=window_hours,
    )

    # Upcoming row for the retry sequence: only when a next touch is
    # genuinely scheduled and budget remains. Deliberately silent on WHAT it
    # will be — retry_now, switch_rail and nudge_customer are all decided
    # live by the agent at that future moment (see e.g. the RBI pre-debit
    # gate in src/guardrail/rules.py, which itself only knows the answer at
    # execution time), never stored ahead of time. This page can honestly
    # say WHEN it'll hear from us again; it cannot honestly say WHAT that
    # contact will be, so it never claims to.
    sequence_upcoming_when = None
    if (
        case.risk_type in ("subscription_failure", "mandate_failure")
        and state == "payable"
        and case.next_action_at is not None
        and case.attempts_used < case.max_attempts
    ):
        sequence_upcoming_when = _format_promise_due(case.next_action_at)

    # Promise-to-pay tracker: a live pending promise, or (absent that) the
    # transparent fact that the last one broke. Mutually exclusive — never
    # both — so the page always shows exactly where things stand.
    promise = (
        await session.execute(
            select(PromiseToPay)
            .where(
                PromiseToPay.recovery_case_id == case.id,
                PromiseToPay.status == "pending",
            )
            .order_by(PromiseToPay.promised_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    broken_promise = None
    if promise is None:
        broken_promise = (
            await session.execute(
                select(PromiseToPay)
                .where(
                    PromiseToPay.recovery_case_id == case.id,
                    PromiseToPay.status == "broken",
                )
                .order_by(PromiseToPay.resolved_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    promise_due_when = _format_promise_due(promise.due_at) if promise else None
    promise_days_left = (
        max(0, (promise.due_at.date() - datetime.now(UTC).date()).days) if promise else 0
    )

    # Honest "we called you" trace. VoiceCallQueue.state == "done" is the
    # real signal for a completed call — RetryAttempt.channel is never set
    # to "voice" by any code path (a voice call is queued as a follow-up
    # against the ORIGINAL nudge_customer attempt, not written as its own
    # attempt row), so that field would silently never match.
    voice_call = (
        await session.execute(
            select(VoiceCallQueue)
            .where(
                VoiceCallQueue.recovery_case_id == case.id,
                VoiceCallQueue.state == "done",
            )
            .order_by(VoiceCallQueue.claimed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    voice_call_when = (
        _ist(voice_call.claimed_at).strftime("%d %b, %H:%M")
        if voice_call is not None and voice_call.claimed_at is not None
        else None
    )

    # Confirming resolves itself: one automatic re-check after a few seconds,
    # then the honest "we'll message you" instead of a spinner that can spin
    # for a minute. The ?r=1 flag stops the loop after the single re-check.
    auto_refresh = state == "confirming" and not request.query_params.get("r")

    # Everything else this person has open with us. One COUNT, not a second
    # page render: the link is worth offering only when it leads somewhere
    # this page is not already showing.
    mine_link: str | None = None
    mine_count = 0
    keys = ledger_keys(case.customer_id)
    if keys:
        mine_count = int(await session.scalar(
            select(func.count())
            .select_from(RecoveryCase)
            .where(
                RecoveryCase.customer_id.in_(keys),
                RecoveryCase.state == "open",
                RecoveryCase.id != case.id,
            )
        ) or 0)
        if mine_count:
            mine_token = recovery_link.mint_customer(case.id)
            mine_link = f"/mine/{mine_token}" if mine_token else None

    # The click-through signal: one audit row per serve. Best-effort on
    # purpose — a metrics write failing must never cost a customer their
    # page. Not written on the 404 path (no case to attribute it to); the
    # /pay path records its own attempt row instead.
    #
    # `actor` is the customer UNLESS the viewer arrived from the console's
    # "open their page" button, which leaves a signed, ten-minute, single-case
    # marker cookie for exactly this question. Both views are worth recording;
    # only one is a click-through. Without the split, every operator preview
    # inflated the nudge CTR in cases.contact_effectiveness(), which counts
    # page_viewed rows against cases contacted — a figure the console
    # publishes. The console's own session cookie cannot answer this: it is
    # scoped to path=/console so that a customer page never carries operator
    # authority, which also means it never arrives here.
    try:
        from src.cases import log_event
        from src.merchant.routes import preview_marker_names

        operator = preview_marker_names(request, case.id)
        log_event(
            session, case, "page_viewed",
            actor="operator" if operator else "customer", state=state,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.warning("page_viewed event write failed for case %s", case.id)

    return templates.TemplateResponse(
        request,
        "recover.html",
        {
            "t": t,
            "lang": lang,
            "merchant_name": settings.merchant_name or None,
            "public_base_url": settings.public_base_url or None,
            "whatsapp": settings.support_whatsapp or None,
            "state": state,
            "detail": detail,
            "amount": _money(case.amount_at_risk),
            "recovered": _money(case.amount_recovered),
            "recovered_ref": case.recovered_ref,
            "recovered_at": (
                _ist(case.recovered_at).strftime("%d %b %Y, %H:%M")
                if case.recovered_at else None
            ),
            "order_ref": (failure.order_id if failure else None) or case.subject_ref,
            "method": failure.method if failure else None,
            "bank": (failure.bank or failure.card_issuer) if failure else None,
            "failed_at": failure.failed_at if failure else case.opened_at,
            "expires_line": (
                _format_expiry(expires_at, lang) if expires_at else None
            ),
            "recommended_rail": recommended_rail,
            "auto_refresh": auto_refresh,
            "token": token,
            # The shared shell's self-links (language toggle, the confirming
            # page's one re-check). Supplied by the route, never read off the
            # request — see base.html for why.
            "page_path": f"/recover/{token}",
            "has_link": live_link is not None,
            "hero_key": hero_key,
            "is_payment": is_payment,
            "risk_timeline_key": risk_timeline_key,
            "cart_items": cart_items,
            "sequence_attempts": sequence_attempts,
            "sequence_upcoming_when": sequence_upcoming_when,
            "voice_call_when": voice_call_when,
            "promise_due_when": promise_due_when,
            "promise_days_left": promise_days_left,
            "last_promise_broken": broken_promise is not None,
            # Invoices only: every other risk type either has no due date or
            # has one identical to "when it failed", already in the register.
            "due_when": due_when,
            "days_overdue": days_overdue,
            # The server's own cap, not a number typed into the template —
            # the form promised "2-6" and shipped 2 for exactly as long as
            # those were two independent facts.
            "max_instalments": MAX_INSTALMENTS,
            "promise_min_date": (datetime.now(UTC) + timedelta(days=1))
                .astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d"),
            "promise_max_date": (datetime.now(UTC) + timedelta(
                days=get_settings().promise_max_horizon_days
            )).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d"),
            # Autopay is offered only when it can actually be honoured: the
            # feature is on AND the amount is under the RBI unattended-debit
            # threshold. Above it the checkbox is absent rather than present
            # and refused, because a page that offers something it cannot do
            # has made its own promise it will break.
            "autopay_offerable": _mandate_offerable(_outstanding(case)),
            "autopay_amount": _money(_outstanding(case)),
            # The way to the customer's other open payments, offered only when
            # there ARE others: a link promising "everything you owe" that
            # leads to a page holding this same one payment is worse than no
            # link. Minted here rather than in the template so the template
            # cannot mint a credential.
            "mine_link": mine_link,
            "mine_count": mine_count,
        },
    )


@router.post("/recover/{token}/optout")
async def opt_out(
    request: Request, token: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """
    The customer's stop button, on the page itself.

    Compliance and trust in one control: the dunning research is explicit
    that a visible way out raises conversion for everyone else, and the
    engine already has the machinery — this wires the page to
    cases.record_opt_out, which withdraws consent AND closes every open case
    for the customer rather than just skipping one message.
    """
    _check_rate_limit(request, kind="pay", limit=_PAY_LIMIT)
    verified = recovery_link.verify_with_expiry(token)
    if verified is None:
        return RedirectResponse(f"/recover/{token}", status_code=303)

    case_id, _ = verified
    case = await session.get(RecoveryCase, case_id)
    if case is None:
        return RedirectResponse(f"/recover/{token}", status_code=303)

    if case.customer_id:
        # The full stop: withdraw consent AND close every open case this
        # customer has, not just the one on screen.
        from src.cases import record_opt_out

        await record_opt_out(session, case.customer_id)
    else:
        # No identifiable customer (the webhook carried no email/contact), so
        # there is no ledger row to opt out — but THIS case must still die.
        # Silently re-rendering the payable page here was a loophole: a
        # customer pressing "stop" and getting offered payment again is the
        # exact complaint an opt-out exists to prevent.
        from src.cases import close_case

        close_case(case, "opted_out", "customer withdrew consent (unidentified)")
        session.add(case)

    await session.commit()
    logger.info("Customer opt-out from recovery page: case=%s", case.id)
    return RedirectResponse(f"/recover/{token}", status_code=303)


@router.post("/recover/{token}/pay")
async def start_payment(
    request: Request, token: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """
    Hand the customer a payment link — reusing one if it already exists.

    Every branch re-reads the case first. The page they submitted from may be
    minutes old, may have been restored from the back button, or may be the
    second of two taps on a slow connection; none of those are evidence about
    what the case looks like now.
    """
    _check_rate_limit(request, kind="pay", limit=_PAY_LIMIT)
    loaded = await _load(session, token)
    if loaded is None:
        return RedirectResponse(f"/recover/{token}", status_code=303)

    case, failure, attempt, live_link = loaded

    def _state_of(c: Any, f: Any, a: Any) -> str:
        """View state for one (re-read) snapshot of the case."""
        if f is not None:
            d = explain(f.failure_class)
            return _view_state(c, a, d.retryable, failed_at=f.failed_at)
        from src.chasers.policy import policy_for

        pol = policy_for(c.risk_type)
        d = explain(None, risk_type=c.risk_type)
        return _view_state(
            c, a, d.retryable,
            failed_at=_aware(c.opened_at),
            window_hours=pol.consent_window_hours if pol else None,
        )

    state = _state_of(case, failure, attempt)

    # The duplicate-payment guard. Anything other than "payable" means paying
    # again is either pointless or dangerous, so the answer is the page itself,
    # which will explain which of those it is.
    if state != "payable":
        logger.info("Pay refused for case %s in state %s", case.id, state)
        return RedirectResponse(f"/recover/{token}", status_code=303)

    if live_link:
        # Reuse, never re-mint. A second link is a second payment object the
        # customer could pay separately, and nothing downstream would merge them.
        logger.info("Reusing existing payment link for case %s", case.id)
        return _payment_redirect(live_link, token)

    # Re-read everything one last time before the write-ahead row: a capture
    # can land between the load above and this line, closing the case as
    # recovered, and a concurrent engine retry can park a pending attempt.
    # Minting on the stale read is how a customer ends up with a link for a
    # payment that just confirmed. The UNIQUE constraint and the overpayment
    # path are the backstops; this re-read is what makes them rare.
    reloaded = await _load(session, token)
    if reloaded is None:
        return RedirectResponse(f"/recover/{token}", status_code=303)
    case, failure, attempt, live_link = reloaded
    state = _state_of(case, failure, attempt)
    if state != "payable":
        logger.info("Pay refused on re-read for case %s in state %s", case.id, state)
        return RedirectResponse(f"/recover/{token}", status_code=303)
    if live_link:
        return _payment_redirect(live_link, token)

    if failure is not None:
        return await _start_payment_from_failure(
            session, case, failure, token
        )
    return await _start_payment_from_case(session, case, token)


@router.post("/recover/{token}/promise")
async def promise_a_date(
    request: Request, token: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """
    "I'll pay by Friday" — captured from the page, with the same silence
    invariant a voice promise gets.

    The token is the authority (same as /pay and /optout). The date is
    validated server-side — inside the horizon cap, in the future — and the
    amount defaults to the case's outstanding. record_promise refuses past
    the per-case cap; the refusal is a redirect back with the page telling
    the truth, not an error the customer has to interpret. No amount is
    collected here: promising a date and choosing an amount are different
    anxieties, and conflating them on a payment page drops both.
    """
    _check_rate_limit(request, kind="pay", limit=_PAY_LIMIT)
    verified = recovery_link.verify_with_expiry(token)
    if verified is None:
        return RedirectResponse(f"/recover/{token}", status_code=303)

    case_id, _ = verified
    case = await session.get(RecoveryCase, case_id)
    if case is None or case.state != "open":
        return RedirectResponse(f"/recover/{token}", status_code=303)

    settings = get_settings()
    form = await request.form()

    due_at: datetime | None = None
    raw_date = str(form.get("due_date", "")).strip()
    if raw_date:
        try:
            # A date input gives YYYY-MM-DD in the customer's wall clock;
            # India is IST so the promise lands at midnight IST, coerced to
            # UTC like every other wall-clock boundary here.
            local = datetime.strptime(raw_date, "%Y-%m-%d")
            due_at = local.replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(UTC)
        except ValueError:
            due_at = None
    now = datetime.now(UTC)
    horizon = timedelta(days=settings.promise_max_horizon_days)
    if due_at is None or not (now + timedelta(hours=12) < due_at <= now + horizon):
        # Unparseable, in the past, or beyond the horizon the kept-rate
        # research supports. The page re-renders and says what a valid date
        # is; a silent accept would record noise as a commitment.
        logger.info("Promise date refused on case %s: %r", case.id, raw_date)
        return RedirectResponse(f"/recover/{token}?promise=invalid", status_code=303)

    from src.cases import outstanding_paise, record_promise

    promise = await record_promise(
        session,
        case,
        amount=outstanding_paise(case),
        due_at=due_at,
        channel="payment_link",
        source_ref=f"recovery_page:{token[:8]}",
        confidence="explicit",
    )
    if promise is None:
        # Per-case promise cap: record_promise already audited the refusal.
        return RedirectResponse(f"/recover/{token}?promise=refused", status_code=303)
    await session.commit()
    logger.info("Customer promised from recovery page: case=%s due=%s",
                case.id, due_at.isoformat())

    # The customer ticked "collect it automatically on that date". The promise
    # is already recorded, so everything below is an UPGRADE to a commitment
    # that already stands — if authorisation fails, is abandoned halfway, or is
    # not offered at all, the plain promise survives untouched and the reminder
    # goes out as it always did. That is why the promise is written first.
    if str(form.get("autopay", "")).strip() and _mandate_offerable(
        outstanding_paise(case)
    ):
        auth_url = await _authorize_mandate(session, case, promise, token)
        if auth_url:
            await session.commit()
            return RedirectResponse(auth_url, status_code=303)
        # Authorisation could not be started. Say nothing about autopay rather
        # than claiming a mandate that does not exist — the date still stands.
        logger.info("Mandate authorisation unavailable: case=%s", case.id)
    return RedirectResponse(f"/recover/{token}?promise=ok", status_code=303)


def _identity_for(case: RecoveryCase, event: Any) -> tuple[str | None, str | None]:
    """
    The payer's email and phone, for a gateway object that needs a customer.

    Prefers the risk event, which carries what the merchant actually sent.
    Falls back to the case's canonical customer key, which is `email:a@b.in` or
    `phone:+9198...` (src/cases.py:customer_key) — the prefix says which, so a
    case opened without a risk event still yields one usable identifier rather
    than none.
    """
    email = getattr(event, "customer_email", None)
    contact = getattr(event, "customer_contact", None)
    if not email and not contact:
        key = case.customer_id or ""
        if key.startswith("email:"):
            email = key.split(":", 1)[1] or None
        elif key.startswith("phone:"):
            contact = key.split(":", 1)[1] or None
    return email, contact


def _outstanding(case: RecoveryCase) -> int:
    """The case's unpaid balance. Imported locally, as cases.py is elsewhere."""
    from src.cases import outstanding_paise

    return outstanding_paise(case)


def _mandate_offerable(amount_paise: int) -> bool:
    """
    Whether autopay may be offered for this amount at all.

    Two gates, and the amount one is regulatory rather than cautious: RBI
    exempts unattended e-mandate debits from per-transaction authentication
    only below a threshold, and above it every debit needs the customer
    present — which is not something a sweep can arrange at 9 AM on the
    promised date. So above the cap the option is not SHOWN, rather than shown
    and then refused: a page that offers a customer something it cannot do has
    made its own promise it will break.
    """
    settings = get_settings()
    return (
        settings.promise_mandate_enabled
        and 0 < amount_paise <= settings.mandate_max_auto_debit_paise
    )


async def _authorize_mandate(
    session: AsyncSession,
    case: RecoveryCase,
    promise: Any,
    token: str,
) -> str | None:
    """
    Start UPI Autopay authorisation for a promise. Returns the URL to send the
    customer to, or None if the gateway could not be reached.

    The mandate is authorised for the PROMISE's amount and expires just after
    its date. Both bounds are deliberate: a mandate authorised for more than
    the promise, or for longer than it, is consent the customer did not give,
    and it would be invisible to them afterwards.
    """
    from src.cases import log_event
    from src.executor.retry_executor import RetryExecutor

    # Razorpay needs a customer to attach a token to. Passing None here minted
    # an authorisation with no payer identity — the customer object could not
    # be created, so nothing was ever chargeable. The identity is the same one
    # every other outbound call uses: the risk event that fed this case.
    from src.orchestrator import get_orchestrator

    event = await get_orchestrator()._latest_risk_event(case, session)
    email, contact = _identity_for(case, event)

    try:
        result = await RetryExecutor().create_mandate_authorization(
            amount_paise=promise.amount_promised,
            customer_email=email,
            customer_contact=contact,
            # A day past the promise, so a debit on the date itself is inside
            # the mandate and a forgotten authorisation cannot outlive it.
            expire_at=promise.due_at + timedelta(days=1),
            idempotency_key=f"mandate_auth_{promise.id}",
            description=f"Autopay on {promise.due_at.date().isoformat()}",
        )
    except Exception:
        logger.exception("Mandate authorisation failed for case %s", case.id)
        return None

    promise.mandate_token = result.get("mandate_token")
    promise.mandate_customer_ref = result.get("gateway_customer_id")
    promise.mandate_authorization_ref = result.get("authorization_ref")
    # PENDING, not active. The customer has not authorised anything yet — they
    # are about to be sent to their UPI app to do it. Only the authorisation
    # webhook may promote this to `active`, because only it knows the customer
    # actually approved. Marking it active here would let the sweep debit a
    # mandate nobody ever agreed to.
    promise.mandate_status = "pending"
    promise.mandate_registered_at = datetime.now(UTC)
    log_event(
        session,
        case,
        "promise_made",
        actor="customer",
        promise_id=str(promise.id),
        mandate="authorization_started",
        amount=promise.amount_promised,
    )
    url = result.get("short_url")
    return str(url) if url else None


@router.post("/recover/{token}/dispute")
async def raise_a_dispute(
    request: Request, token: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """
    "This invoice is wrong" — the customer answer that quiets the chase.

    Freezes all automated contact on THIS case (an open dispute excludes the
    case from every consolidated statement and every per-case chase until a
    human resolves it) and alerts the merchant the moment it opens. Chasing
    an invoice the customer says is wrong is how a commercial relationship
    ends up in a legal escalation; the freeze is total, the resolution is
    the merchant's judgement.

    Same discipline as /promise: token is the authority, the reason is
    validated server-side (present, bounded), the refusal is a redirect with
    the page telling the truth. Idempotent — a double-tap lands on the
    already-open dispute, not a second one.
    """
    from src.receivables.disputes import open_dispute

    _check_rate_limit(request, kind="pay", limit=_PAY_LIMIT)
    verified = recovery_link.verify_with_expiry(token)
    if verified is None:
        return RedirectResponse(f"/recover/{token}", status_code=303)

    case_id, _ = verified
    case = await session.get(RecoveryCase, case_id)
    if case is None or case.state != "open":
        return RedirectResponse(f"/recover/{token}", status_code=303)

    form = await request.form()
    reason = str(form.get("reason", "")).strip()
    dispute = await open_dispute(session, case, reason=reason)
    if dispute is None:
        # Empty reason, a terminal case, or a double-tap that found the
        # existing open dispute — the page tells the truth either way.
        return RedirectResponse(f"/recover/{token}?dispute=invalid", status_code=303)

    await session.commit()
    logger.info("Customer disputed from recovery page: case=%s", case.id)
    return RedirectResponse(f"/recover/{token}?dispute=ok", status_code=303)


@router.post("/recover/{token}/plan")
async def request_a_plan(
    request: Request, token: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """
    "I can pay in parts" — an instalment plan over one case's outstanding.

    Each instalment is a promise (the existing pause/break/audit machinery
    runs on it unchanged), so a plan is quiet until an instalment is missed —
    and a missed instalment is broken on the clock by the promise sweep, with
    the ladder's ratchet resuming the chase at the next firmer rung.

    The form posts instalment dates and amounts the customer chose; the
    server validates the shape (sum, order, horizon) exactly as the merchant
    API would — the page is a convenience for the same law, not a separate
    one. A one-instalment "plan" is refused: that is a promise, and the
    /promise form already exists for it.
    """
    from src.receivables.plans import create_plan

    _check_rate_limit(request, kind="pay", limit=_PAY_LIMIT)
    verified = recovery_link.verify_with_expiry(token)
    if verified is None:
        return RedirectResponse(f"/recover/{token}", status_code=303)

    case_id, _ = verified
    case = await session.get(RecoveryCase, case_id)
    if case is None or case.state != "open":
        return RedirectResponse(f"/recover/{token}", status_code=303)

    form = await request.form()

    # Parse up to MAX_INSTALMENTS (date, amount) pairs from the form. The
    # page sends instalment_1_date / instalment_1_amount, ... — pairs with a
    # missing half are refused rather than defaulted, because a plan is a
    # commitment and half a commitment is noise.
    from src.receivables.plans import MAX_INSTALMENTS

    amounts: list[int] = []
    dates: list[datetime] = []
    for i in range(1, MAX_INSTALMENTS + 1):
        raw_date = str(form.get(f"instalment_{i}_date", "")).strip()
        raw_amount = str(form.get(f"instalment_{i}_amount", "")).strip()
        if not raw_date and not raw_amount:
            continue
        if not raw_date or not raw_amount:
            return RedirectResponse(
                f"/recover/{token}?plan=invalid", status_code=303
            )
        try:
            local = datetime.strptime(raw_date, "%Y-%m-%d")
            due = local.replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(UTC)
            rupees = float(raw_amount)
            amount_paise = int(round(rupees * 100))
        except ValueError:
            return RedirectResponse(
                f"/recover/{token}?plan=invalid", status_code=303
            )
        if amount_paise <= 0:
            return RedirectResponse(
                f"/recover/{token}?plan=invalid", status_code=303
            )
        amounts.append(amount_paise)
        dates.append(due)

    plan = await create_plan(
        session, case, amounts_paise=amounts, due_dates=dates
    )
    if plan is None:
        # Shape refused (sum ≠ outstanding, one instalment, out of horizon,
        # duplicate active plan) or the case is terminal — validate the
        # shape law is one law everywhere; the page re-renders the truth.
        return RedirectResponse(f"/recover/{token}?plan=refused", status_code=303)

    await session.commit()
    logger.info(
        "Customer plan from recovery page: case=%s instalments=%d",
        case.id, len(amounts),
    )
    return RedirectResponse(f"/recover/{token}?plan=ok", status_code=303)


async def _self_serve_mint(
    session: AsyncSession,
    case: Any,
    token: str,
    context: Any,
    rail: Any,
    execute: Any,
    *,
    payment_failure_id: Any = None,
    payment_id: Any = None,
) -> Any:
    """
    Shared self-serve pay pipeline — both rails walk the same steps, because
    they are the same money-safety discipline:

    1. Guardrail subset BEFORE writing anything (validate_self_serve: schema,
       hard-decline blocklist, attempt budget, idempotency — customer-initiated
       does not mean unvalidated, but the outreach rules are skipped because
       this is the customer acting, not us chasing).
    2. Deterministic idempotency key + WRITE-AHEAD attempt row, committed
       BEFORE the Razorpay call — the UNIQUE constraint on idempotency_key is
       what closes the double-tap race (two taps a second apart each finding
       no live link and each minting one), and it needs a ROW to bite on.
       Recording the attempt also makes attribution true and spends one unit
       of the case's budget honestly.
    3. Execute, then resolve the row: success bumps the customer's rolling
       retry tally exactly as an engine-initiated retry does (the guardrail's
       per-customer limit reads this ledger; a self-serve attempt it cannot
       see is a slot the next webhook undercounts). A failed mint contacted
       nobody, so only a success bumps it.

    `execute(idem) -> dict` mints the link (payment rail or case rail);
    `rail` is the enforced target recorded on the row. The payment rail
    passes its failure/payment ids; the case rail leaves both NULL — there
    is no payment behind it, and the columns are nullable for exactly this.
    """
    from src.agent.actions import RetryAction
    from src.cases import attach_attempt
    from src.guardrail.gate import GuardrailGate
    from src.models import RetryAttempt

    action = RetryAction(
        action="retry_now",
        rail=rail,
        reason="Customer-initiated from the recovery page",
    )
    idem = f"selfserve_{case.subject_ref}_{case.attempts_used}"
    gate_result = GuardrailGate().validate_self_serve(
        action, context, idem, case.attempts_used
    )
    if not gate_result.passed:
        logger.warning(
            "Self-serve pay rejected by guardrail for case %s: %s",
            case.id, gate_result.rejection_reasons,
        )
        return RedirectResponse(f"/recover/{token}?error=1", status_code=303)

    attempt_row = RetryAttempt(
        payment_failure_id=payment_failure_id,
        payment_id=payment_id,
        idempotency_key=idem,
        attempt_number=case.attempts_used + 1,
        action_type="retry_now",
        target_rail=rail,
        agent_type="customer",
        agent_reasoning="Customer-initiated from the recovery page",
        guardrail_passed=gate_result.passed,
        result="pending",
        channel="payment_link",
        created_at=datetime.now(UTC),
    )
    session.add(attempt_row)
    try:
        await session.flush()
    except IntegrityError:
        # Lost the race to another tap of the same button. Their row owns the
        # slot; redirecting re-renders the page, which will now read the
        # winner's pending attempt as "confirming".
        await session.rollback()
        logger.info("Self-serve race lost on %s — the other tap owns it", idem)
        return RedirectResponse(f"/recover/{token}", status_code=303)
    attach_attempt(case, attempt_row)
    await session.commit()

    try:
        result = await execute(idem)
    except Exception:
        logger.exception("Self-serve link creation failed for case %s", case.id)
        attempt_row.result = "failed"
        attempt_row.result_details = {"error": "Self-serve link creation failed"}
        attempt_row.executed_at = datetime.now(UTC)
        session.add(attempt_row)
        await session.commit()
        return RedirectResponse(f"/recover/{token}?error=1", status_code=303)

    url = result.get("short_url") if result.get("success") else None
    attempt_row.executed_at = datetime.now(UTC)
    if url:
        attempt_row.result = "success"
        attempt_row.external_ref = result.get("payment_link_id")
        attempt_row.result_details = {
            "success": True,
            "short_url": str(url),
            "self_serve": True,
        }
        if case.customer_id:
            from src.orchestrator import get_orchestrator

            await get_orchestrator()._update_retry_ledger(
                case.customer_id, action, session
            )
    else:
        attempt_row.result = "failed"
        attempt_row.result_details = {"error": result.get("error", "link failed")}
    session.add(attempt_row)
    await session.commit()

    if not url:
        return RedirectResponse(f"/recover/{token}?error=1", status_code=303)
    return _payment_redirect(str(url), token)


async def _start_payment_from_failure(
    session: AsyncSession, case: Any, failure: Any, token: str
) -> Any:
    """Self-serve pay for the payment rail — a gateway failure row exists."""
    from src.agent.actions import FailureContext
    from src.executor.retry_executor import RetryExecutor

    detail = explain(failure.failure_class)

    # The recommended rail is ENFORCED here, not just suggested: a card
    # drop-off gets a UPI-ONLY link (upi_link=True in the executor), so the
    # page's primary verb and the payment object agree. A generic link for
    # everything else keeps every method available.
    recommended = _recommended_rail(failure.failure_class)

    now = datetime.now(UTC)
    local_now = now.astimezone(ZoneInfo("Asia/Kolkata"))
    context = FailureContext(
        payment_id=failure.payment_id,
        order_id=failure.order_id,
        failure_class=failure.failure_class,
        error_code=failure.error_code or "UNKNOWN",
        amount=failure.amount,
        currency=failure.currency,
        method=failure.method,
        bank=failure.bank or failure.card_issuer,
        customer_id=case.customer_id,
        failed_at=failure.failed_at,
        current_time=now,
        hour_of_day=local_now.hour,
        day_of_week=local_now.weekday(),
        is_retryable=detail.retryable,
        original_failure_id=str(failure.id),
    )

    async def execute(idem: str) -> Any:
        return await RetryExecutor().execute_retry(
            payment_failure=failure,
            action_type="retry_now",
            target_rail=recommended,
            idempotency_key=idem,
        )

    return await _self_serve_mint(
        session, case, token, context, recommended, execute,
        payment_failure_id=failure.id,
        payment_id=failure.payment_id,
    )


async def _start_payment_from_case(
    session: AsyncSession, case: Any, token: str
) -> Any:
    """
    Self-serve pay for a chaser-driven case — no payment was (necessarily)
    attempted, so there is no failure row: the amount, currency and customer
    come from the case, and the link is minted by the case-driven executor
    path.
    """
    from src.agent.actions import FailureContext
    from src.chasers.policy import policy_for
    from src.orchestrator import get_orchestrator

    policy = policy_for(case.risk_type)
    detail = explain(None, risk_type=case.risk_type)
    recommended = policy.recommended_rail if policy else None
    failure_class = policy.failure_class if policy else case.risk_type
    window_hours = policy.consent_window_hours if policy else None

    now = datetime.now(UTC)
    local_now = now.astimezone(ZoneInfo("Asia/Kolkata"))
    context = FailureContext(
        risk_type=case.risk_type,
        payment_id=case.subject_ref,
        failure_class=failure_class,
        error_code=failure_class.upper(),
        amount=case.amount_at_risk,
        currency=case.currency,
        method="unknown",
        customer_id=case.customer_id,
        failed_at=_aware(case.opened_at),
        current_time=now,
        hour_of_day=local_now.hour,
        day_of_week=local_now.weekday(),
        consent_window_hours=window_hours,
        is_retryable=detail.retryable,
    )

    orchestrator = get_orchestrator()
    # The customer's email/contact for the link: the newest risk event that
    # fed this case. Without one the link is still minted — Razorpay only
    # needs the amount — it just carries no prefilled customer.
    event = await orchestrator._latest_risk_event(case, session)

    async def execute(idem: str) -> Any:
        return await orchestrator._executor.execute_case_action(
            case=case,
            action_type="retry_now",
            target_rail=recommended,
            idempotency_key=idem,
            customer_email=event.customer_email if event else None,
            customer_contact=event.customer_contact if event else None,
            # The customer is already at the checkout — we redirect them to the
            # link ourselves. Notifying them about it too would be a nudge
            # they never asked for, from their own click.
            notify_customer=False,
        )

    return await _self_serve_mint(
        session, case, token, context, recommended, execute
    )


# ── The account statement: every open invoice for one buyer ──────────────
#
# A buyer with four overdue invoices has had ONE consolidated message per
# ladder rung since the receivables layer landed — and every link in it
# still resolved to one carrier invoice's isolated page. There was nowhere
# they could see what they actually owe. src/receivables/statement.py's own
# SMS copy has promised "the account statement page" the whole time, and
# scheduler.py passed statement_link="" because the page did not exist.
#
# DESIGN RULE: this page SHOWS and LINKS. It processes no payment, no
# dispute, no plan. Every action deep-links to that invoice's own
# /recover/{token} page, which is the tested money path. A second surface
# with its own pay handling would be a second thing to get wrong on the one
# path where being wrong means double-charging someone.


# ── The customer home ───────────────────────────────────────────────────────
#
# A B2B buyer has had /statement since the receivables layer landed: one link,
# every open invoice, a deep link into each. A consumer had nothing of the
# kind. Someone whose card failed on three orders got three SMS, three links
# and three isolated pages, with no way to see what they owed in total or to
# find yesterday's link again — the commonest support question there is
# ("how much do I actually owe you?") had no answer on any page we served.
#
# SAME DESIGN RULE AS THE STATEMENT, for the same reason: this page SHOWS and
# LINKS. It takes no payment, records no promise, opens no dispute. Every
# action deep-links to that case's own /recover page, which is the tested
# money path. A second surface with its own pay handling would be a second
# thing to get wrong on the one path where being wrong means charging someone
# twice.


@router.get("/mine/{token}", response_class=HTMLResponse)
async def customer_home(
    request: Request, token: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """Everything one person has open with this merchant, in one place."""
    _check_rate_limit(request, kind="page", limit=_PAGE_LIMIT)

    settings = get_settings()
    lang = pick(request.query_params.get("lang"), request.headers.get("accept-language"))
    t = Translator(lang)

    def gone() -> Any:
        # One response for expired, forged, wrong-scope and unknown alike —
        # see recovery_page() for why they must not be distinguishable.
        return templates.TemplateResponse(
            request, "expired.html",
            {"t": t, "lang": lang, "merchant_name": settings.merchant_name},
            status_code=404,
        )

    anchor_case_id = recovery_link.verify_customer(token)
    if anchor_case_id is None:
        return gone()
    anchor_case = await session.get(RecoveryCase, anchor_case_id)
    if anchor_case is None:
        return gone()

    # The token names a case; the page is about that case's customer. An
    # anonymous case (no customer_id — a merchant risk event can be pushed
    # without one) has no "everything you owe" to show, so it is not a page
    # with an empty table, it is a page that does not exist.
    keys = ledger_keys(anchor_case.customer_id)
    if not keys:
        return gone()

    cases = list((await session.execute(
        select(RecoveryCase)
        .where(RecoveryCase.customer_id.in_(keys))
        .order_by(RecoveryCase.opened_at.desc())
    )).scalars().all())
    if not cases:
        return gone()

    case_ids = [c.id for c in cases]
    # One query each rather than one per row: a customer with eight open
    # payments should not cost twenty-four round trips to render.
    disputed = set((await session.execute(
        select(CaseDispute.case_id).where(
            CaseDispute.case_id.in_(case_ids), CaseDispute.status == "open"
        )
    )).scalars().all())

    promises: dict[Any, Any] = {}
    for row in (await session.execute(
        select(PromiseToPay)
        .where(
            PromiseToPay.recovery_case_id.in_(case_ids),
            PromiseToPay.status == "pending",
        )
        .order_by(PromiseToPay.promised_at.desc())
    )).scalars().all():
        promises.setdefault(row.recovery_case_id, row)

    # PaymentPlan is deliberately thin — each instalment is a PromiseToPay
    # grouped by a plan_instalments row — so the count the customer cares
    # about ("paying in 3 parts") is a join, not a column.
    plan_rows = (await session.execute(
        select(PaymentPlan.case_id, func.count(PlanInstalment.id))
        .join(PlanInstalment, PlanInstalment.plan_id == PaymentPlan.id, isouter=True)
        .where(
            PaymentPlan.case_id.in_(case_ids),
            PaymentPlan.status == "active",
        )
        .group_by(PaymentPlan.case_id)
    )).all()
    plans = {row[0]: row[1] for row in plan_rows}

    open_rows: list[dict[str, Any]] = []
    settled_rows: list[dict[str, Any]] = []
    outstanding = 0
    for case in cases:
        owed = max(0, case.amount_at_risk - case.amount_recovered)
        settled = case.state in ("recovered", "opted_out", "abandoned", "expired") or (
            owed == 0
        )
        due_when, days_overdue = _aging(case.due_at)
        promise = promises.get(case.id)
        instalments = plans.get(case.id)
        # Each row hands out the same credential that row's own SMS would
        # have carried, minted the same way. This page grants no authority of
        # its own over any case.
        case_token = recovery_link.mint(case.id)
        entry = {
            "ref": case.subject_ref,
            "risk_type": case.risk_type,
            "state": case.state,
            "amount": _money(owed if not settled else case.amount_recovered),
            "part_paid": (
                _money(case.amount_recovered)
                if case.amount_recovered and not settled else None
            ),
            "opened": _ist(_aware(case.opened_at)).strftime("%d %b %Y"),
            "due_when": due_when,
            "days_overdue": days_overdue,
            "disputed": case.id in disputed,
            "promised": (
                _format_promise_due(_aware(promise.due_at)) if promise else None
            ),
            "instalments": instalments or None,
            "link": f"/recover/{case_token}" if case_token else None,
        }
        if settled:
            settled_rows.append(entry)
        else:
            outstanding += owed
            open_rows.append(entry)

    verified = recovery_link.verify_customer_with_expiry(token)
    return templates.TemplateResponse(
        request, "mine.html",
        {
            "t": t,
            "lang": lang,
            "merchant_name": settings.merchant_name,
            "public_base_url": settings.public_base_url or None,
            "whatsapp": settings.support_whatsapp or None,
            "open_rows": open_rows,
            "settled_rows": settled_rows,
            "outstanding": _money(outstanding),
            "page_path": f"/mine/{token}",
            "expires_line": (
                _format_expiry(verified[1], lang) if verified else None
            ),
            # Same reasoning as the statement's: record_opt_out withdraws
            # consent for the customer and closes every open case of theirs,
            # so opting out from one case already covers this whole page.
            # Minting a customer-scoped write endpoint to reach the same
            # outcome would be new attack surface for no new behaviour.
            "optout_token": recovery_link.mint(cases[0].id),
        },
    )


@router.get("/statement/{token}", response_class=HTMLResponse)
async def statement_page(
    request: Request, token: str, session: AsyncSession = Depends(get_session)
) -> Any:
    """Show one buyer every invoice they have open, and where to pay each."""
    _check_rate_limit(request, kind="page", limit=_PAGE_LIMIT)

    settings = get_settings()
    lang = pick(request.query_params.get("lang"), request.headers.get("accept-language"))
    t = Translator(lang)

    account_id = recovery_link.verify_account(token)
    if account_id is None:
        # Same single response as /recover for expired, forged, wrong-scope
        # and unknown alike — see recovery_page() for why they must not be
        # distinguishable.
        return templates.TemplateResponse(
            request, "expired.html",
            {"t": t, "lang": lang, "merchant_name": settings.merchant_name},
            status_code=404,
        )

    account = await session.get(ArAccount, account_id)
    if account is None:
        return templates.TemplateResponse(
            request, "expired.html",
            {"t": t, "lang": lang, "merchant_name": settings.merchant_name},
            status_code=404,
        )

    cases = (
        (
            await session.execute(
                select(RecoveryCase)
                .where(
                    RecoveryCase.account_id == account_id,
                    RecoveryCase.risk_type == "invoice_overdue",
                    RecoveryCase.state == "open",
                )
                .order_by(RecoveryCase.due_at, RecoveryCase.opened_at)
            )
        )
        .scalars()
        .all()
    )

    # One query for every open dispute on this account, not one per invoice:
    # a buyer disputing three of eight invoices should cost three fewer round
    # trips than the loop would. A disputed invoice still appears — hiding it
    # would make the total unexplainable — but says so, the same fact the
    # console's disputed table and the ladder's chase exclusion both act on.
    disputed_ids = set(
        (
            await session.execute(
                select(CaseDispute.case_id).where(
                    CaseDispute.case_id.in_([c.id for c in cases]),
                    CaseDispute.status == "open",
                )
            )
        )
        .scalars()
        .all()
    ) if cases else set()

    invoices = []
    outstanding = 0
    for case in cases:
        owed = max(0, case.amount_at_risk - case.amount_recovered)
        outstanding += owed
        due_when, days_overdue = _aging(case.due_at)
        # Each row's link is that invoice's OWN case token, minted the same
        # way its per-case nudge mints one. The statement grants no authority
        # of its own over any invoice — it hands out the same credential the
        # SMS for that invoice would have carried.
        case_token = recovery_link.mint(case.id)
        invoices.append({
            "ref": case.subject_ref,
            "amount": _money(owed),
            "part_paid": _money(case.amount_recovered) if case.amount_recovered else None,
            "due_when": due_when,
            "days_overdue": days_overdue,
            "disputed": case.id in disputed_ids,
            "link": f"/recover/{case_token}" if case_token else None,
        })

    return templates.TemplateResponse(
        request, "statement.html",
        {
            "t": t,
            "lang": lang,
            "merchant_name": settings.merchant_name,
            "public_base_url": settings.public_base_url or None,
            "account_name": account.display_name or account.account_ref,
            "invoices": invoices,
            "outstanding": _money(outstanding),
            "page_path": f"/statement/{token}",
            "whatsapp": settings.support_whatsapp or None,
            # The opt-out form posts to the OLDEST open invoice's own case
            # route. Not a shortcut: cases.record_opt_out withdraws consent
            # for the customer and closes every open case of theirs, so
            # opting out from one invoice already opts out of the account.
            # Minting an account-scoped write endpoint to reach the same
            # outcome would be new attack surface for no new behaviour.
            "optout_token": (
                recovery_link.mint(cases[0].id) if cases else None
            ),
        },
    )
