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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src import recovery_link
from src.config import get_settings
from src.customer.explain import explain
from src.customer.i18n import Translator, pick
from src.database import get_session
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


def _recommended_rail(failure_class: str | None) -> str | None:
    return "upi" if (failure_class or "") in _UPI_RECOMMENDED_CLASSES else None


def _ist(dt: datetime) -> datetime:
    """The page speaks IST — the timezone the blackout and the bank run on."""
    return dt.astimezone(ZoneInfo("Asia/Kolkata"))


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


def _client_ip(request: Request) -> str:
    # Render terminates TLS at its proxy and forwards the real client in this
    # header; falling back to the socket peer keeps it honest elsewhere.
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown"
    )


def _check_rate_limit(request: Request, *, kind: str, limit: int) -> None:
    """Raise 429 once an IP exhausts its window budget for this kind of call."""
    key = f"{kind}:{_client_ip(request)}"
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

# How long an attempt may sit unresolved before "we are confirming" stops being
# a fair thing to say. Razorpay's capture webhook normally lands in seconds; a
# quarter of an hour means something is wrong, and the customer deserves the
# honest "we don't know yet" rather than a spinner forever.
CONFIRMING_WINDOW = timedelta(minutes=15)


def _money(paise: int) -> str:
    """Rupees, grouped the Indian way: 12,34,567 not 1,234,567."""
    whole = abs(int(paise)) // 100
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"₹{s}"


async def _load(session: AsyncSession, token: str) -> tuple[Any, Any, Any] | None:
    """(case, failure, latest_attempt) for a valid token, else None."""
    verified = recovery_link.verify_with_expiry(token)
    if verified is None:
        return None
    case_id, _expires_at = verified
    case = await session.get(RecoveryCase, case_id)
    if case is None:
        return None

    failure = (
        await session.execute(
            select(PaymentFailure).where(PaymentFailure.payment_id == case.subject_ref)
        )
    ).scalars().first()
    attempt = (
        await session.execute(
            select(RetryAttempt)
            .where(RetryAttempt.recovery_case_id == case.id)
            .order_by(desc(RetryAttempt.created_at))
        )
    ).scalars().first()
    return case, failure, attempt


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


def _view_state(case: Any, attempt: Any, retryable: bool) -> str:
    """
    The single customer-visible state. Derived here so the template branches
    once and the ordering of these checks is reviewable in one place.
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
    if not retryable:
        return "not_retryable"
    return "payable"


def _live_link(attempt: Any) -> str | None:
    """An existing payment link we can send them back to, if there is one."""
    if attempt is None or not isinstance(attempt.result_details, dict):
        return None
    url = attempt.result_details.get("short_url")
    return str(url) if url else None


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

    case, failure, attempt = loaded

    # The token's REAL deadline, surfaced as honest urgency: it is the exact
    # instant the consent window closes and the link stops working. No fake
    # countdown anywhere on this page.
    verified = recovery_link.verify_with_expiry(token)
    expires_at = verified[1] if verified else None

    detail = explain(failure.failure_class if failure else None)
    state = _view_state(case, attempt, detail.retryable)

    # Confirming resolves itself: one automatic re-check after a few seconds,
    # then the honest "we'll message you" instead of a spinner that can spin
    # for a minute. The ?r=1 flag stops the loop after the single re-check.
    auto_refresh = state == "confirming" and not request.query_params.get("r")

    recommended_rail = _recommended_rail(failure.failure_class if failure else None)

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
            "has_link": _live_link(attempt) is not None,
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

    case, failure, attempt = loaded
    detail = explain(failure.failure_class if failure else None)
    state = _view_state(case, attempt, detail.retryable)

    # The duplicate-payment guard. Anything other than "payable" means paying
    # again is either pointless or dangerous, so the answer is the page itself,
    # which will explain which of those it is.
    if state != "payable":
        logger.info("Pay refused for case %s in state %s", case.id, state)
        return RedirectResponse(f"/recover/{token}", status_code=303)

    existing = _live_link(attempt)
    if existing:
        # Reuse, never re-mint. A second link is a second payment object the
        # customer could pay separately, and nothing downstream would merge them.
        logger.info("Reusing existing payment link for case %s", case.id)
        return RedirectResponse(existing, status_code=303)

    if failure is None:  # pragma: no cover — a case with no failure row
        return RedirectResponse(f"/recover/{token}", status_code=303)

    from src.cases import attach_attempt
    from src.executor.retry_executor import RetryExecutor
    from src.models import RetryAttempt

    # The recommended rail is ENFORCED here, not just suggested: a card
    # drop-off gets a UPI-ONLY link (upi_link=True in the executor), so the
    # page's primary verb and the payment object agree. A generic link for
    # everything else keeps every method available.
    recommended = _recommended_rail(failure.failure_class)

    # Deterministic key, WRITE-AHEAD of the Razorpay call — same discipline as
    # the orchestrator's money block, and for the first version of this route
    # the row was simply missing: two taps a second apart each found no
    # "live link" (the check reads attempts, and there was none), each minted
    # its own link, and the customer could pay both. The UNIQUE constraint on
    # idempotency_key is what actually closes that race — but it needs a ROW
    # to bite on. Recording the attempt also makes attribution true (a
    # capture on this link credits the case instead of reading as
    # self-recovery) and spends one unit of the case's budget honestly.
    idem = f"selfserve_{case.subject_ref}_{case.attempts_used}"
    attempt_row = RetryAttempt(
        payment_failure_id=failure.id,
        payment_id=failure.payment_id,
        idempotency_key=idem,
        attempt_number=case.attempts_used + 1,
        action_type="retry_now",
        target_rail=recommended,
        agent_type="customer",
        agent_reasoning="Customer-initiated from the recovery page",
        guardrail_passed=True,
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
    else:
        attempt_row.result = "failed"
        attempt_row.result_details = {"error": result.get("error", "link failed")}
    session.add(attempt_row)
    await session.commit()

    if not url:
        return RedirectResponse(f"/recover/{token}?error=1", status_code=303)
    return RedirectResponse(str(url), status_code=303)
