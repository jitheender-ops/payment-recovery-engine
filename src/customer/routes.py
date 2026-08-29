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
import time
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src import recovery_link
from src.agent.actions import PaymentRail
from src.auth import client_ip
from src.config import get_settings
from src.customer.explain import explain
from src.customer.i18n import Translator, pick
from src.database import get_session
from src.formatting import ist as _ist
from src.formatting import money as _money
from src.models import PaymentFailure, RecoveryCase, RetryAttempt

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

# ── Rate limiting ───────────────────────────────────────────────────────────
# This is the one public, unauthenticated surface in the product (the token in
# the URL is the credential), and until now nothing throttled it: token
# guessing, /pay hammering and slow-loris loops were all free. A fixed window
# per client IP, in-process — which is exactly right for the deployment this
# ships on (one uvicorn worker; render.yaml pins WEB_CONCURRENCY=1) and
# deliberately NOT a Redis dependency. Two limits because they protect
# different things: page views are cheap reads, /pay mints Razorpay objects.
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_PAGE_LIMIT = 30          # page views per window per IP
_PAY_LIMIT = 6            # payment starts per window per IP — link creation is the expensive call
_RATE_LIMIT_BUCKETS: dict[str, deque[float]] = {}


def _check_rate_limit(request: Request, *, kind: str, limit: int) -> None:
    """Raise 429 once an IP exhausts its window budget for this kind of call."""
    key = f"{kind}:{client_ip(request)}"
    now_mono = time.monotonic()
    bucket = _RATE_LIMIT_BUCKETS.setdefault(key, deque())
    while bucket and now_mono - bucket[0] > _RATE_LIMIT_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= limit:
        logger.warning("Rate limit hit: %s (%d in window)", key, len(bucket))
        raise HTTPException(status_code=429, detail="Too many requests")
    bucket.append(now_mono)
    # GC once the map grows beyond the expected population of live clients:
    # drop every bucket whose newest hit has aged out of the window — not just
    # empty ones, or a slow drip from many IPs grows the dict without bound.
    if len(_RATE_LIMIT_BUCKETS) > 10_000:
        stale = [
            k for k, v in _RATE_LIMIT_BUCKETS.items()
            if not v or now_mono - v[-1] > _RATE_LIMIT_WINDOW_SECONDS
        ]
        for stale_key in stale:
            del _RATE_LIMIT_BUCKETS[stale_key]


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
    if _is_payment_redirect_target(url):
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

    state = _view_state(
        case, attempt, detail.retryable,
        failed_at=anchor, window_hours=window_hours,
    )

    # Confirming resolves itself: one automatic re-check after a few seconds,
    # then the honest "we'll message you" instead of a spinner that can spin
    # for a minute. The ?r=1 flag stops the loop after the single re-check.
    auto_refresh = state == "confirming" and not request.query_params.get("r")

    return templates.TemplateResponse(
        request,
        "recover.html",
        {
            "t": t,
            "lang": lang,
            "merchant_name": settings.merchant_name or None,
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
            "has_link": live_link is not None,
            "hero_key": hero_key,
            "is_payment": is_payment,
            "risk_timeline_key": risk_timeline_key,
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


async def _start_payment_from_failure(
    session: AsyncSession, case: Any, failure: Any, token: str
) -> Any:
    """Self-serve pay for the payment rail — a gateway failure row exists."""
    from src.agent.actions import FailureContext, RetryAction
    from src.cases import attach_attempt
    from src.executor.retry_executor import RetryExecutor
    from src.guardrail.gate import GuardrailGate
    from src.models import RetryAttempt

    detail = explain(failure.failure_class)

    # The recommended rail is ENFORCED here, not just suggested: a card
    # drop-off gets a UPI-ONLY link (upi_link=True in the executor), so the
    # page's primary verb and the payment object agree. A generic link for
    # everything else keeps every method available.
    recommended = _recommended_rail(failure.failure_class)

    # Run the self-serve guardrail subset BEFORE writing anything. Customer-
    # initiated does not mean unvalidated: the previous code wrote
    # guardrail_passed=True without consulting a single rule. The subset
    # skips the outreach rules (blackout, contact limits — this is the
    # customer acting, not us chasing) but keeps schema, hard-decline
    # blocklist, attempt budget and idempotency. See validate_self_serve.
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
    action = RetryAction(
        action="retry_now",
        rail=recommended,
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

    # Deterministic key, WRITE-AHEAD of the Razorpay call — same discipline as
    # the orchestrator's money block, and for the first version of this route
    # the row was simply missing: two taps a second apart each found no
    # "live link" (the check reads attempts, and there was none), each minted
    # its own link, and the customer could pay both. The UNIQUE constraint on
    # idempotency_key is what actually closes that race — but it needs a ROW
    # to bite on. Recording the attempt also makes attribution true (a
    # capture on this link credits the case instead of reading as
    # self-recovery) and spends one unit of the case's budget honestly.
    attempt_row = RetryAttempt(
        payment_failure_id=failure.id,
        payment_id=failure.payment_id,
        idempotency_key=idem,
        attempt_number=case.attempts_used + 1,
        action_type="retry_now",
        target_rail=recommended,
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
        result = await RetryExecutor().execute_retry(
            payment_failure=failure,
            action_type="retry_now",
            target_rail=recommended,
            idempotency_key=idem,
        )
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
        # Count against the customer's rolling retry tally exactly as an
        # engine-initiated retry does: the guardrail's per-customer limit
        # reads this ledger, and a self-serve attempt it cannot see is a
        # slot the next webhook's count undercounts. A failed link mint
        # contacted nobody, so only a success bumps it.
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


async def _start_payment_from_case(
    session: AsyncSession, case: Any, token: str
) -> Any:
    """
    Self-serve pay for a chaser-driven case — no payment was (necessarily)
    attempted, so there is no failure row: the amount, currency and customer
    come from the case, and the link is minted by the case-driven executor
    path. Same safety discipline as the payment rail: guardrail subset before
    writing, deterministic key, write-ahead row, reuse-never-remint, ledger
    bump on success.
    """
    from src.agent.actions import FailureContext, RetryAction
    from src.cases import attach_attempt
    from src.chasers.policy import policy_for
    from src.guardrail.gate import GuardrailGate
    from src.models import RetryAttempt

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
    action = RetryAction(
        action="retry_now",
        rail=recommended,
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

    # Write-ahead row, same reasoning as the payment rail: the UNIQUE
    # constraint on idempotency_key closes the double-tap race, but only if
    # the row exists to bite on. payment_failure_id and payment_id stay NULL —
    # there is no payment behind this case, and the columns are nullable for
    # exactly this.
    attempt_row = RetryAttempt(
        payment_failure_id=None,
        payment_id=None,
        idempotency_key=idem,
        attempt_number=case.attempts_used + 1,
        action_type="retry_now",
        target_rail=recommended,
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
        await session.rollback()
        logger.info("Self-serve race lost on %s — the other tap owns it", idem)
        return RedirectResponse(f"/recover/{token}", status_code=303)
    attach_attempt(case, attempt_row)
    await session.commit()

    # The customer's email/contact for the link: the newest risk event that
    # fed this case. Without one the link is still minted — Razorpay only
    # needs the amount — it just carries no prefilled customer.
    from src.orchestrator import get_orchestrator

    orchestrator = get_orchestrator()
    event = await orchestrator._latest_risk_event(case, session)

    try:
        result = await orchestrator._executor.execute_case_action(
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
            await orchestrator._update_retry_ledger(
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
