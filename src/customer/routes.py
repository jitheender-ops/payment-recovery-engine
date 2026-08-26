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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src import recovery_link
from src.customer.explain import explain
from src.database import get_session
from src.models import PaymentFailure, RecoveryCase, RetryAttempt

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

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
    # Opportunistic GC: the dict holds one small deque per IP; drop idle ones
    # so a scan of many IPs cannot grow it without bound.
    if len(_RATE_LIMIT_BUCKETS) > 10_000:
        for stale_key in [k for k, v in _RATE_LIMIT_BUCKETS.items() if not v]:
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
    case_id = recovery_link.verify(token)
    if case_id is None:
        return None
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
    """Show one customer their own failed payment and what to do about it."""
    _check_rate_limit(request, kind="page", limit=_PAGE_LIMIT)
    loaded = await _load(session, token)
    if loaded is None:
        # One response for expired, forged and unknown alike. Distinguishing
        # them tells someone probing the URL which guesses are getting warmer.
        return templates.TemplateResponse(
            request, "expired.html", {}, status_code=404
        )

    case, failure, attempt = loaded
    detail = explain(failure.failure_class if failure else None)
    state = _view_state(case, attempt, detail.retryable)

    return templates.TemplateResponse(
        request,
        "recover.html",
        {
            "state": state,
            "detail": detail,
            "amount": _money(case.amount_at_risk),
            "recovered": _money(case.amount_recovered),
            "order_ref": (failure.order_id if failure else None) or case.subject_ref,
            "method": failure.method if failure else None,
            "bank": (failure.bank or failure.card_issuer) if failure else None,
            "failed_at": failure.failed_at if failure else case.opened_at,
            "token": token,
            "has_link": _live_link(attempt) is not None,
        },
    )


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

    from src.executor.retry_executor import RetryExecutor

    # Deterministic key, same construction the orchestrator uses, so a link
    # created here occupies an attempt slot rather than sitting outside the
    # budget the guardrail enforces.
    idem = f"selfserve_{case.subject_ref}_{case.attempts_used}"
    try:
        result = await RetryExecutor().execute_retry(
            payment_failure=failure,
            action_type="retry_now",
            target_rail=None,
            idempotency_key=idem,
        )
    except Exception:
        logger.exception("Self-serve link creation failed for case %s", case.id)
        return RedirectResponse(f"/recover/{token}?error=1", status_code=303)

    url = result.get("short_url") if result.get("success") else None
    if not url:
        return RedirectResponse(f"/recover/{token}?error=1", status_code=303)
    return RedirectResponse(str(url), status_code=303)
