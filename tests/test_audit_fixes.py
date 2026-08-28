"""
Regression tests for the audit round: one runnable check per hole closed.

Each test names the exploit it prevents rather than the function it calls —
these exist so a later refactor that quietly reopens one of these fails here
instead of in production.

The holes, in the order the audit found them:

1. Superseded payment links stayed live while a case was open, so a customer
   holding two of our messages could pay both.
2. Customer identity was derived two different ways and never normalised, so
   one person held two ledgers, two contact budgets and a partial opt-out.
3. The dashboard password took unlimited guesses.
4. A deferred retry_at had no upper bound, so a case could be parked past the
   end of the world and never close.
5. The recovery page shipped no framing or caching headers.
6. Per-IP limits assumed exactly one proxy hop.
7. The signed ingestion surfaces read a body of any size.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agent.actions import FailureContext, RetryAction
from src.cases import canonical_key, customer_key, ledger_keys, record_opt_out
from src.config import get_settings
from src.guardrail.gate import GuardrailGate
from src.models import RecoveryCase, RetryAttempt, RetryLedger

# ══════════════════════════════════════════════════════════════════════════
# 1. Superseded links on an OPEN case
# ══════════════════════════════════════════════════════════════════════════


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def cancel_payment_link(self, link_id: str) -> bool:
        self.calls.append(link_id)
        return True


async def _case_with_links(
    sm: async_sessionmaker[AsyncSession], refs: list[str]
) -> RecoveryCase:
    """An OPEN case carrying one link-bearing attempt per ref, oldest first."""
    from src.cases import open_case

    async with sm() as session:
        case = await open_case(
            session,
            risk_type="payment_failure",
            subject_ref="pay_supersede",
            amount_at_risk=50_000,
            customer_id="c@example.com",
        )
        await session.commit()
        for i, ref in enumerate(refs):
            session.add(
                RetryAttempt(
                    payment_id="pay_supersede",
                    recovery_case_id=case.id,
                    idempotency_key=f"retry_supersede_{i}",
                    attempt_number=i + 1,
                    action_type="retry_now",
                    guardrail_passed=True,
                    result="success",
                    external_ref=ref,
                    result_details={"short_url": f"https://rzp.io/{ref}"},
                    # Distinct, ordered timestamps: the sweep spares the NEWEST
                    # and a tie would (deliberately) spare both.
                    created_at=datetime.now(UTC) - timedelta(hours=len(refs) - i),
                )
            )
        await session.commit()
        return case


async def test_an_open_cases_older_links_are_cancelled(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    EXPLOIT: two retries on one open case leave two payable links for the same
    money. The customer pays both; attribute_capture catches the second only
    after it settles, as an overpayment needing a manual refund.
    """
    from src import scheduler
    from src.orchestrator import get_orchestrator

    await _case_with_links(db_sessionmaker, ["plink_OLD", "plink_MID", "plink_NEW"])
    fake = _FakeExecutor()
    orch = get_orchestrator()
    monkeypatch.setattr(orch, "_executor", fake)

    async with db_sessionmaker() as session:
        cancelled = await scheduler.cancel_superseded_links(session, orch)

    assert cancelled == 2
    assert fake.calls == ["plink_OLD", "plink_MID"]
    assert "plink_NEW" not in fake.calls, "the newest link is the live one"


async def test_the_superseded_sweep_is_idempotent(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """A second pass must not re-cancel — Razorpay is charged a call per try."""
    from src import scheduler
    from src.orchestrator import get_orchestrator

    await _case_with_links(db_sessionmaker, ["plink_A", "plink_B"])
    fake = _FakeExecutor()
    orch = get_orchestrator()
    monkeypatch.setattr(orch, "_executor", fake)

    async with db_sessionmaker() as session:
        assert await scheduler.cancel_superseded_links(session, orch) == 1
    async with db_sessionmaker() as session:
        assert await scheduler.cancel_superseded_links(session, orch) == 0
    assert fake.calls == ["plink_A"]


async def test_a_cancelled_link_is_never_offered_to_the_customer(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The page must not send someone back to a link we have just killed. It
    reads every attempt now, so a stamped-cancelled row has to be skipped
    rather than picked as "the newest link with a short_url".
    """
    from src.customer.routes import _live_link

    live = RetryAttempt(
        idempotency_key="k_live", attempt_number=2, action_type="retry_now",
        guardrail_passed=True, result="success",
        result_details={"short_url": "https://rzp.io/live"},
    )
    dead = RetryAttempt(
        idempotency_key="k_dead", attempt_number=1, action_type="retry_now",
        guardrail_passed=True, result="success",
        result_details={
            "short_url": "https://rzp.io/dead",
            "link_cancelled_at": "2026-08-28T00:00:00+00:00",
        },
    )
    assert _live_link([dead, live]) == "https://rzp.io/live"
    assert _live_link([dead]) is None


def test_a_pending_attempt_is_found_behind_a_newer_row() -> None:
    """
    EXPLOIT: the engine parks a `scheduled` row (or records a nudge) after a
    retry is already in flight. Reading only the NEWEST attempt hid the
    pending one, and the page offered to pay again while the first payment
    might still land — the double-charge guard silently stopped firing.
    """
    from src.customer.routes import _blocking_attempt

    newest_scheduled = RetryAttempt(
        idempotency_key="k_sched", attempt_number=2, action_type="retry_at",
        guardrail_passed=True, result="scheduled",
    )
    older_pending = RetryAttempt(
        idempotency_key="k_pend", attempt_number=1, action_type="retry_now",
        guardrail_passed=True, result="pending",
    )
    found = _blocking_attempt([newest_scheduled, older_pending])
    assert found is older_pending, "a newer row must not hide an in-flight one"


# ══════════════════════════════════════════════════════════════════════════
# 2. One canonical customer identity
# ══════════════════════════════════════════════════════════════════════════


def test_the_two_rails_agree_on_who_a_customer_is() -> None:
    """
    EXPLOIT: the payment rail keyed on email-or-phone off the webhook, the
    risk rail preferred the merchant's own customer_id. One person hit by a
    card decline AND an overdue invoice was two customers — two contact
    budgets, and an opt-out on one that left the other chasing.
    """
    payment_rail = customer_key(email="Accounts@Acme.in", contact="+919876543210")
    risk_rail = customer_key(
        email="accounts@acme.in",
        contact="+91 98765-43210",
        external_id="acme_cust_1",
    )
    assert payment_rail == risk_rail == "email:accounts@acme.in"


def test_formatting_differences_are_not_different_customers() -> None:
    assert customer_key(email="A@B.com") == customer_key(email="  a@b.com ")
    assert customer_key(contact="+91 98765-43210") == customer_key(
        contact="(+91)9876543210"
    )
    # A bare 10-digit number has no country code to compare against, so it
    # stays its own key rather than having one guessed for it.
    assert customer_key(contact="9876543210") != customer_key(
        contact="+919876543210"
    )


def test_an_unidentifiable_customer_has_no_key() -> None:
    """No key means the caller skips the ledger — never a shared bucket."""
    assert customer_key() is None
    assert customer_key(email="not-an-email") is None
    assert customer_key(contact="123") is None
    assert ledger_keys(None) == []


def test_a_legacy_row_is_still_found_after_the_prefix_arrives() -> None:
    """
    Migration 0006 rewrites persisted rows, but during the deploy window a row
    written by an older process still holds the raw address. Missing it would
    hand the customer a FRESH contact budget and lose their consent status.
    """
    keys = ledger_keys("email:a@b.com")
    assert keys[0] == "email:a@b.com", "canonical must be tried first"
    assert "a@b.com" in keys, "the pre-migration spelling must still match"
    assert canonical_key("a@b.com") == canonical_key("email:a@b.com")


async def test_opt_out_reaches_a_case_stored_under_the_legacy_key(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    EXPLOIT: a customer presses "stop" and keeps being chased, because the
    case predates the canonical key and no longer matches it.
    """
    async with db_sessionmaker() as session:
        session.add(
            RecoveryCase(
                risk_type="payment_failure",
                subject_ref="pay_legacy",
                amount_at_risk=10_000,
                currency="INR",
                customer_id="legacy@acme.in",  # pre-migration spelling
                state="open",
                max_attempts=3,
                opened_at=datetime.now(UTC),
            )
        )
        await session.commit()

    async with db_sessionmaker() as session:
        closed = await record_opt_out(session, "email:legacy@acme.in")
        await session.commit()
    assert closed == 1, "the opt-out missed a case under the legacy key"

    async with db_sessionmaker() as reader:
        case = (
            await reader.execute(
                select(RecoveryCase).where(RecoveryCase.subject_ref == "pay_legacy")
            )
        ).scalar_one()
        assert case.state == "opted_out"
        ledger = (
            await reader.execute(
                select(RetryLedger).where(
                    RetryLedger.customer_id == "email:legacy@acme.in"
                )
            )
        ).scalar_one()
        assert ledger.consent_status == "opted_out"


# ══════════════════════════════════════════════════════════════════════════
# 3. Dashboard guessing
# ══════════════════════════════════════════════════════════════════════════


def test_the_dashboard_stops_answering_after_repeated_wrong_passwords(
    monkeypatch: Any,
) -> None:
    """
    EXPLOIT: one static shared password with unlimited tries is a password a
    script walks a wordlist against. The dashboard renders live payment data.
    """
    from dashboard import auth

    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct-horse")
    auth.reset_throttle()
    try:
        for _ in range(auth._MAX_FAILURES):
            assert auth.password_is_correct("wrong") is False

        assert auth.lockout_seconds_remaining() > 0
        # The RIGHT password is refused too while locked out — otherwise the
        # lockout still answers the question the attacker is asking.
        assert auth.password_is_correct("correct-horse") is False
    finally:
        auth.reset_throttle()

    assert auth.lockout_seconds_remaining() == 0
    assert auth.password_is_correct("correct-horse") is True


def test_a_successful_sign_in_clears_the_failure_history(monkeypatch: Any) -> None:
    """A typo before the right password must not count toward a lockout."""
    from dashboard import auth

    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct-horse")
    auth.reset_throttle()
    try:
        assert auth.password_is_correct("typo") is False
        assert auth.password_is_correct("correct-horse") is True
        assert len(auth._FAILURES) == 0
    finally:
        auth.reset_throttle()


# ══════════════════════════════════════════════════════════════════════════
# 4. A deferred retry must land inside the consent window
# ══════════════════════════════════════════════════════════════════════════


def _context(failed_at: datetime, now: datetime) -> FailureContext:
    return FailureContext(
        payment_id="pay_defer",
        failure_class="insufficient_funds",
        error_code="BAD_FUNDS",
        amount=10_000,
        method="card",
        failed_at=failed_at,
        current_time=now,
        hour_of_day=11,
        day_of_week=2,
    )


def test_a_retry_parked_past_the_consent_window_is_rejected() -> None:
    """
    EXPLOIT: the agent returns retry_at far in the future. The attempt parks
    as `scheduled` forever — the fire sweep only picks up rows already due,
    the stale sweep only looks at `pending` — and because the orchestrator
    copies the same instant onto case.next_action_at, the CASE stays open for
    good. Never chased, never expired, never counted.
    """
    now = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)
    failed_at = now - timedelta(hours=1)
    action = RetryAction(
        action="retry_at",
        retry_at=datetime(2099, 1, 1, 10, 0, tzinfo=UTC),
        reason="parked past the end of the world",
    )
    result = GuardrailGate().validate(action, _context(failed_at, now), "idem_1", 0)

    assert result.passed is False
    assert any("consent window" in r for r in result.rejection_reasons)


def test_a_retry_inside_the_window_still_passes() -> None:
    """The bound must not reject the deferrals the agent legitimately makes."""
    now = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)
    failed_at = now - timedelta(hours=1)
    action = RetryAction(
        action="retry_at",
        retry_at=now + timedelta(hours=4),
        reason="bank downtime, wait for the window to clear",
    )
    result = GuardrailGate().validate(action, _context(failed_at, now), "idem_2", 0)

    assert result.passed is True, result.rejection_reasons


# ══════════════════════════════════════════════════════════════════════════
# 6. Per-IP limits and the proxy hop count
# ══════════════════════════════════════════════════════════════════════════


class _Req:
    def __init__(self, xff: str, host: str = "10.0.0.1") -> None:
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.client = type("C", (), {"host": host})()


def test_the_client_is_read_hops_from_the_right(monkeypatch: Any) -> None:
    """
    EXPLOIT (too few hops): with a CDN in front of the platform LB, the
    rightmost entry is a constant internal address — every visitor lands in
    one bucket and the limit becomes a denial of service for everybody.
    EXPLOIT (too many): you read an entry the client wrote and it walks
    around the limit one header value at a time.
    """
    from src.customer import routes

    get_settings.cache_clear()
    monkeypatch.setenv("BEHIND_TRUSTED_PROXY", "true")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    # client-spoofed , real client , cdn-added
    xff = "1.2.3.4, 203.0.113.9, 10.1.1.1"
    assert routes._client_ip(_Req(xff)) == "203.0.113.9"  # type: ignore[arg-type]
    get_settings.cache_clear()


def test_a_short_header_falls_back_to_the_socket_peer(monkeypatch: Any) -> None:
    """Fewer entries than trusted hops means the chain is not the one we
    expect — reading it anyway would key on a value the client supplied."""
    from src.customer import routes

    get_settings.cache_clear()
    monkeypatch.setenv("BEHIND_TRUSTED_PROXY", "true")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    assert routes._client_ip(_Req("1.2.3.4", host="198.51.100.7")) == "198.51.100.7"  # type: ignore[arg-type]
    get_settings.cache_clear()


# ══════════════════════════════════════════════════════════════════════════
# 7. Body size cap on the signed surfaces
# ══════════════════════════════════════════════════════════════════════════


def test_an_oversized_body_is_refused_before_it_is_read() -> None:
    """
    The HMAC check only runs AFTER the body is read, so an unauthenticated
    socket could make the process allocate whatever it declared.
    """
    from src.ingestion.signature import MAX_BODY_BYTES, body_too_large

    assert body_too_large(str(MAX_BODY_BYTES + 1)) is True
    assert body_too_large(str(MAX_BODY_BYTES)) is False
    # A chunked request declares nothing — measure what actually arrived.
    assert body_too_large(None, b"x" * (MAX_BODY_BYTES + 1)) is True
    assert body_too_large(None, b"x" * 10) is False
    # An unparseable Content-Length is not a size we can trust either way.
    assert body_too_large("not-a-number", b"x" * 10) is False


# ══════════════════════════════════════════════════════════════════════════
# 5. Headers on the one public HTML surface
# ══════════════════════════════════════════════════════════════════════════


def test_the_recovery_page_refuses_to_be_framed_or_cached(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    EXPLOIT: the page renders an amount and hosts a POST "Pay" button, so an
    attacker who has a token can frame it and UI-redress the pay and opt-out
    controls. And its URL IS the credential, so a shared browser or an
    intermediate cache holding a copy hands the next person the case.

    Asserted on a REJECTED token on purpose: the headers must be on every
    response from this surface, not only the happy path.
    """
    from collections.abc import AsyncIterator

    from fastapi.testclient import TestClient

    from src.database import get_session
    from src.main import app

    async def override() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = override
    try:
        # No `with`: entering the context would run the lifespan (DB init,
        # scheduler, credential check), and none of that is under test here.
        resp = TestClient(app).get("/recover/not-a-real-token")
    finally:
        app.dependency_overrides.clear()

    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert "no-store" in resp.headers["cache-control"]
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_a_recovery_token_never_reaches_the_access_log() -> None:
    """
    EXPLOIT: uvicorn writes the request line verbatim, so every page view
    printed a live bearer credential into the platform's log store — the one
    copy of it we control. recovery_link.py already reasons about SMS logs
    and browser history; this is the same leak and the only closable one.
    """
    import logging

    from src.main import _RedactRecoveryToken

    token = "eyJhbGciOi.c2lnbmF0dXJl"
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", f"/recover/{token}", "1.1", 200), None,
    )
    assert _RedactRecoveryToken().filter(record) is True
    assert record.args is not None
    line = record.args[2]  # type: ignore[index]
    assert token not in str(line), "the token survived into the log line"
    assert "redacted" in str(line)
    # Still correlatable: the same token must redact to the same digest.
    twin = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s %s %s %s %d",
        ("127.0.0.1", "GET", f"/recover/{token}", "1.1", 200), None,
    )
    _RedactRecoveryToken().filter(twin)
    assert twin.args is not None
    assert twin.args[2] == line  # type: ignore[index]


def test_unrelated_paths_pass_through_the_log_filter_untouched() -> None:
    """The filter must not rewrite request lines it has no business in."""
    import logging

    from src.main import _RedactRecoveryToken

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s %s %s %s %d",
        ("127.0.0.1", "POST", "/webhooks/razorpay", "1.1", 200), None,
    )
    _RedactRecoveryToken().filter(record)
    assert record.args is not None
    assert record.args[2] == "/webhooks/razorpay"  # type: ignore[index]
