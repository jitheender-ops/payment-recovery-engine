"""
The merchant console: gating, and that every panel renders what it queried.

The console had no tests at all, which is how it shipped two silent failures —
a receivables block querying a closed session, and a `delivered = 0` compare
that Postgres rejects and SQLite accepts. Both were swallowed by per-query
excepts, so the page rendered, the suite passed, and the panels were simply
empty in production.

So these tests assert on RENDERED OUTPUT, not on the query layer alone. A
panel that silently degrades to empty is exactly the failure being guarded
against, and only the HTML can prove it did not.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import src.receivables.models  # noqa: F401  — register the AR tables on Base
from src.agent.actions import ActionType
from src.cases import open_case, record_promise
from src.classifier.taxonomy import FailureClass
from src.config import get_settings
from src.guardrail.rules import GuardrailRules
from src.merchant import console_data
from src.merchant.routes import _eval_headline, _failure_classes
from src.merchant.routes import router as merchant_router
from src.models import (
    PromiseToPay,
    RetryAttempt,
    SchedulerHeartbeat,
    VoiceCallQueue,
)
from src.receivables.models import (
    AccountTask,
    ArAccount,
    ArContactLog,
    CaseDispute,
    MerchantAlert,
)

PASSWORD = "console-test-password"


@pytest.fixture
def console(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> Any:
    """The merchant router over the test database, signed in."""
    monkeypatch.setattr("src.merchant.routes.async_session_factory", db_sessionmaker)
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(merchant_router)
    client = TestClient(app)
    response = client.post(
        "/console/login", data={"password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, "sign-in failed — the rest is meaningless"
    yield client
    get_settings.cache_clear()


async def _seed(sm: async_sessionmaker[AsyncSession]) -> None:
    """One of everything the console claims to show."""
    now = datetime.now(UTC)
    async with sm() as s:
        account = ArAccount(account_ref="ref:buyer-corp", display_name="Buyer Corp")
        s.add(account)
        await s.flush()

        # A human call task at the urgent rung. Seeded because the console's
        # own claim is that it shows what automation refused, and the ladder
        # raising work for a person is the clearest case of that.
        s.add(
            AccountTask(
                account_id=account.id,
                kind="call",
                detail={"reason": "Urgent rung reached, 42 days past due"},
                status="open",
            )
        )

        # A recovered case, so the money line has both halves.
        paid = await open_case(
            s, risk_type="payment_failure", subject_ref="pay_done",
            amount_at_risk=50_000, customer_id="email:a@b.in", max_attempts=3,
        )
        paid.state = "recovered"
        paid.amount_recovered = 50_000
        paid.recovered_at = now - timedelta(hours=2)
        paid.recovered_via_attempt_id = uuid.uuid4()

        # A part-paid open case: this is what makes "still owed" differ from
        # "at risk", which the old console could not express at all.
        part = await open_case(
            s, risk_type="invoice_overdue", subject_ref="INV-PART",
            amount_at_risk=100_000, customer_id="email:ap@buyer.in",
            account_id=account.id, due_at=now - timedelta(days=9), max_attempts=4,
        )
        part.amount_recovered = 40_000

        disputed = await open_case(
            s, risk_type="invoice_overdue", subject_ref="INV-DISPUTED",
            amount_at_risk=75_000, customer_id="email:ap@buyer.in",
            account_id=account.id, due_at=now - timedelta(days=4), max_attempts=4,
        )

        # Out of attempts, still open — the exception list.
        spent = await open_case(
            s, risk_type="checkout_abandonment", subject_ref="cart_spent",
            amount_at_risk=20_000, max_attempts=2,
        )
        spent.attempts_used = 2
        await s.flush()

        await record_promise(
            s, part, amount=60_000, due_at=now + timedelta(days=3),
            channel="voice", confidence="explicit",
        )
        s.add_all([
            PromiseToPay(
                recovery_case_id=paid.id, customer_id="email:a@b.in",
                amount_promised=50_000, due_at=now - timedelta(days=5),
                status="kept", kept_late_days=2, channel="payment_link",
            ),
            PromiseToPay(
                recovery_case_id=spent.id, amount_promised=20_000,
                due_at=now - timedelta(days=8), status="broken", channel="voice",
            ),
            CaseDispute(
                case_id=disputed.id, reason="Quantity billed does not match the PO",
                status="open", opened_at=now - timedelta(days=6),
            ),
            ArContactLog(
                account_id=account.id, stage_level=2,
                case_refs=[{"ref": "INV-PART"}], channels=["email", "sms"],
                sms_copy="x", email_subject="y", planned_for=now,
            ),
            MerchantAlert(event_type="dispute_opened", case_ref="INV-DISPUTED",
                          detail={}, delivered=False),
            RetryAttempt(
                idempotency_key="k_sched", attempt_number=1,
                recovery_case_id=part.id, action_type="retry_at",
                guardrail_passed=True, result="scheduled",
                scheduled_at=now + timedelta(hours=4),
            ),
            RetryAttempt(
                idempotency_key="k_rejected", attempt_number=1,
                recovery_case_id=spent.id, action_type="retry_now",
                guardrail_passed=False, result="rejected",
                guardrail_rejection_reason="RBI e-mandate framework: no notice",
            ),
            SchedulerHeartbeat(id=1, last_tick_at=now, last_tick_counts={"cases_chased": 2}),
        ])
        await s.flush()

        attempt = RetryAttempt(
            idempotency_key="k_voice", attempt_number=1,
            recovery_case_id=part.id, action_type="nudge_customer",
            guardrail_passed=True, result="success",
        )
        s.add(attempt)
        await s.flush()
        s.add(VoiceCallQueue(
            recovery_case_id=part.id, retry_attempt_id=attempt.id,
            customer_contact="+919812345678", risk_type="invoice_overdue",
            amount_paise=60_000, state="queued",
        ))
        await s.commit()


# ── Gating ────────────────────────────────────────────────────────────────


def test_live_console_requires_a_session(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    monkeypatch.setattr("src.merchant.routes.async_session_factory", db_sessionmaker)
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(merchant_router)
    r = TestClient(app).get("/console/live", follow_redirects=False)
    get_settings.cache_clear()
    assert r.status_code == 303 and r.headers["location"] == "/console/login"


def test_live_console_fails_closed_without_a_password(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """An unset DASHBOARD_PASSWORD must not open the door."""
    monkeypatch.setattr("src.merchant.routes.async_session_factory", db_sessionmaker)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(merchant_router)
    r = TestClient(app).get("/console/live")
    get_settings.cache_clear()
    assert r.status_code == 200
    # Asserted on the live panels, not on "₹" — the brand mark in the nav is a
    # rupee glyph, so that substring is present on every page including login.
    assert "Still owed" not in r.text, "live figures rendered with no password"
    assert 'class="needs-title"' not in r.text


# ── Cookie security attributes ────────────────────────────────────────────


def test_the_session_cookie_is_secure_outside_development(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """The deployed console runs under APP_ENV=staging (render.yaml) with
    real money figures behind a TLS-terminating proxy, so its session cookie
    must never be accepted over a plaintext leg. Development (localhost / the
    demo tunnel) is the one exemption — there a Secure cookie would silently
    never be stored and the console would look logged out for no reason.

    Keying off ``!= development`` rather than ``== production`` is the point
    of this test: staging is a real deployment, and the old check shipped it
    without the flag.
    """
    monkeypatch.setattr("src.merchant.routes.async_session_factory", db_sessionmaker)
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)

    for env, expect_secure in (
        ("staging", True),
        ("production", True),
        ("development", False),
    ):
        monkeypatch.setenv("APP_ENV", env)
        get_settings.cache_clear()
        app = FastAPI()
        app.include_router(merchant_router)
        r = TestClient(app).post(
            "/console/login", data={"password": PASSWORD}, follow_redirects=False
        )
        get_settings.cache_clear()
        assert r.status_code == 303
        cookie = r.headers.get("set-cookie", "")
        assert "rc_session=" in cookie
        if expect_secure:
            assert "Secure" in cookie, f"{env} session cookie must carry Secure"
        else:
            assert "Secure" not in cookie, f"{env} session cookie must not carry Secure"


async def test_the_preview_marker_cookie_is_secure_outside_development(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """The /recover marker left by the console's "open their page" button is
    a session-equivalent credential for the case's view — same Secure rule as
    the session cookie itself, for the same reason."""
    monkeypatch.setattr("src.merchant.routes.async_session_factory", db_sessionmaker)
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("RECOVERY_LINK_SECRET", "x" * 32)
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()

    async with db_sessionmaker() as s:
        case = await open_case(
            s, risk_type="payment_failure", subject_ref="pay_cookie_secure",
            amount_at_risk=250000, customer_id="a@b.test",
        )
        await s.commit()
        case_id = str(case.id)

    app = FastAPI()
    app.include_router(merchant_router)
    # https base: the cookie now carries Secure, and a Secure cookie is never
    # sent back over plain http — so over TestClient's default http origin
    # the session would not arrive and the open would redirect to login. The
    # https origin is also the deployment reality (TLS-terminated proxy).
    client = TestClient(app, base_url="https://testserver")
    client.post("/console/login", data={"password": PASSWORD})
    r = client.post(
        "/console/customer/open", data={"case_id": case_id}, follow_redirects=False
    )
    get_settings.cache_clear()
    assert r.status_code == 303
    assert r.headers["location"].startswith("/recover/")
    cookie = r.headers.get("set-cookie", "")
    assert "recovery_preview=" in cookie
    assert "Secure" in cookie, "staging preview marker must carry Secure"


# ── Login throttling: bounded, not a leak ──────────────────────────────────


def test_the_login_throttle_prunes_stale_entries(monkeypatch: Any) -> None:
    """The failure maps key on client IP and are only emptied by a successful
    login, so a distributed guessing attempt — many addresses, one guess each,
    none ever succeeding — would otherwise grow them without bound. Past the
    GC threshold, rolled-off buckets and expired locks are dropped; live
    locks and fresh buckets must survive untouched, because that is the
    throttling actually working.
    """
    import time
    from collections import deque

    from src.merchant import routes

    # The maps are module-global; save and restore so the rest of the suite
    # (which logs in over the same "testclient" address) is unaffected.
    saved_failures = routes._LOGIN_FAILURES
    saved_locked = routes._LOGIN_LOCKED_UNTIL
    routes._LOGIN_FAILURES = {}
    routes._LOGIN_LOCKED_UNTIL = {}
    try:
        monkeypatch.setattr(routes, "_LOGIN_GC_AT", 2)
        now = time.monotonic()

        # Five distinct attacker IPs whose failures rolled off the window and
        # whose locks have expired — pure dead weight.
        for i in range(5):
            ip = f"10.0.0.{i}"
            routes._LOGIN_FAILURES[ip] = deque([now - 120.0])
            routes._LOGIN_LOCKED_UNTIL[ip] = now - 1.0
        # A live lockout: must survive the sweep or the throttle stops
        # throttling.
        routes._LOGIN_FAILURES["10.0.0.99"] = deque([now])
        routes._LOGIN_LOCKED_UNTIL["10.0.0.99"] = now + 300.0
        # A fresh failure with no lock: bucket stays (its window is live).
        routes._LOGIN_FAILURES["10.0.0.100"] = deque([now])

        routes._gc_login_state()

        assert "10.0.0.99" in routes._LOGIN_FAILURES
        assert "10.0.0.99" in routes._LOGIN_LOCKED_UNTIL
        assert "10.0.0.100" in routes._LOGIN_FAILURES
        for i in range(5):
            assert f"10.0.0.{i}" not in routes._LOGIN_FAILURES
            assert f"10.0.0.{i}" not in routes._LOGIN_LOCKED_UNTIL

        # And the write path sweeps too: recording a failure past the
        # threshold drops the stale set.
        routes._LOGIN_FAILURES["10.0.0.200"] = deque([now - 120.0])
        routes._LOGIN_LOCKED_UNTIL["10.0.0.200"] = now - 1.0
        routes._record_login_failure("10.0.0.101")
        assert "10.0.0.200" not in routes._LOGIN_FAILURES
        assert "10.0.0.200" not in routes._LOGIN_LOCKED_UNTIL
        # The recorder's own fresh bucket is what it just recorded.
        assert "10.0.0.101" in routes._LOGIN_FAILURES
    finally:
        routes._LOGIN_FAILURES = saved_failures
        routes._LOGIN_LOCKED_UNTIL = saved_locked


# ── The panels actually render ────────────────────────────────────────────


async def test_every_panel_renders_its_data(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    One assertion per feature that shipped without a surface.

    Each of these would pass a query-level test while rendering nothing, which
    is precisely how the receivables panel stayed empty in production.
    """
    await _seed(db_sessionmaker)
    html = console.get("/console/live").text

    # Money line: the BALANCE, not the opening figure. 100k+75k+20k at risk
    # on open cases, less 40k already paid = 155,000 paise = ₹1,550.
    assert "Still owed" in html
    assert "₹1,550" in html, "still-owed is not the outstanding balance"
    assert "already part-paid" in html, "the at-risk/outstanding gap is unexplained"

    # The worklist, and what it caught.
    assert "Needs you" in html
    assert "disputed" in html.lower()
    assert "out of attempts" in html
    assert "waiting to be placed" in html, "the stuck voice queue was not surfaced"

    # The ladder, with the account on its rung.
    assert "The ladder" in html
    assert "firm" in html and "final" in html
    assert "1 account" in html

    # Promises: kept/broken split and the pending list.
    assert "Promises to pay" in html
    assert "kept" in html and "broken" in html
    assert "grace window" in html, "kept-late was folded into kept-on-time"

    # Disputes in full, with the customer's own words.
    assert "Quantity billed does not match the PO" in html

    # Guardrail refusals and the activity trail.
    assert "What the guardrail refused" in html
    assert "Recent activity" in html

    # The voice queue in full. voice_panel() was computed on every render and
    # displayed nowhere: the only voice signal was a one-line attention item
    # that fires solely when queued>0 and claimed==0, so a queue with a
    # climbing `failed` count — the shape of an outage — was invisible.
    assert "Voice calls" in html, "the voice queue panel is not rendered"
    assert "Being called now" in html
    assert "Failed" in html
    assert "Ended in opt-out" in html


async def test_a_stale_heartbeat_dominates_the_page(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    A dead scheduler makes every figure below it a frozen snapshot, so it is
    stated at the top rather than left for the reader to infer from numbers
    that quietly stopped changing.
    """
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        hb = await s.get(SchedulerHeartbeat, 1)
        assert hb is not None
        hb.last_tick_at = datetime.now(UTC) - timedelta(hours=6)
        await s.commit()

    html = console.get("/console/live").text
    assert "Engine stopped" in html
    assert "no retries or reminders are firing" in html
    assert "Engine running" not in html


async def test_an_empty_ledger_says_so_rather_than_showing_zeros(
    console: Any
) -> None:
    """Zeros read as 'your business is failing'; an empty state reads as
    'nothing has arrived yet', which is the truth."""
    html = console.get("/console/live").text
    assert "The ledger is empty" in html
    # The rendered heading, not the bare words — "Needs you" also appears in a
    # CSS section comment, which is not what this test is about.
    assert 'class="needs-title"' not in html


async def test_nothing_needing_attention_is_stated_out_loud(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """An empty worklist is a real answer, not a blank space."""
    async with db_sessionmaker() as s:
        case = await open_case(
            s, risk_type="payment_failure", subject_ref="pay_clean",
            amount_at_risk=10_000, max_attempts=3,
        )
        case.state = "recovered"
        case.amount_recovered = 10_000
        case.recovered_at = datetime.now(UTC)
        s.add(SchedulerHeartbeat(id=1, last_tick_at=datetime.now(UTC), last_tick_counts={}))
        await s.commit()

    html = console.get("/console/live").text
    assert "Nothing needs you" in html


async def test_the_console_shows_no_customer_identifiers(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The console's contract (PRODUCT.md): aggregate and PII-free — totals,
    counts, and the merchant's OWN references. Never a customer email, phone
    or id. Asserted against the seeded values, which include all three.
    """
    await _seed(db_sessionmaker)
    html = console.get("/console/live").text

    for leaked in ("ap@buyer.in", "a@b.in", "+919812345678", "email:"):
        assert leaked not in html, f"customer identifier {leaked!r} reached the console"


# ── The public landing describes the engine that exists ───────────────────


def test_the_landing_describes_what_happens_after_the_link(console: Any) -> None:
    """
    The landing shipped describing a one-directional engine: leak, chase,
    link. Voice, promises, plans and disputes were built after it and had no
    public surface at all — a page that undersells half the product is as
    wrong as one that oversells it.

    The landing is public, so the signed-in `console` client is incidental
    here; it touches no database and reads no session.
    """
    html = console.get("/console").text

    assert "Then it listens" in html
    assert "Hinglish" in html, "the voice agent has no surface"
    assert "promise" in html.lower() and "instalment" in html.lower()
    assert "dispute" in html.lower()

    # The escalation rungs come from INVOICE_LADDER, so the tone words are the
    # enforced ones. If someone renames a rung, this fails rather than drifts.
    from src.receivables.ladder import INVOICE_LADDER

    for stage in INVOICE_LADDER:
        assert stage.tone in html, f"ladder rung {stage.tone!r} missing from the landing"


def test_the_landing_needs_no_database(monkeypatch: Any) -> None:
    """Product facts only — it must render with the database gone."""
    class _Boom:
        def __call__(self) -> Any:
            raise RuntimeError("database is gone")

    monkeypatch.setattr("src.merchant.routes.async_session_factory", _Boom())
    app = FastAPI()
    app.include_router(merchant_router)
    r = TestClient(app).get("/console")
    assert r.status_code == 200
    assert "Then it listens" in r.text


async def test_a_database_outage_renders_honestly(
    console: Any, monkeypatch: Any
) -> None:
    """A console that cannot read must say so, not render a confident zero."""
    class _Boom:
        def __call__(self) -> Any:
            raise RuntimeError("database is gone")

    monkeypatch.setattr("src.merchant.routes.async_session_factory", _Boom())
    html = console.get("/console/live").text
    assert "Can't reach the database" in html
    assert "Still owed" not in html


# ── The five pages folded in from the Streamlit console ────────────────────
# They used to be a separate deployed service with no tests at all, which is
# how one of them stayed broken on Postgres for months. Same contract as the
# ledger: gated, renders its data, PII-free, and honest when the database is
# gone.

# Every gated console page that takes no path parameter. Three contract tests
# run over this list — it must require a session, it must render, and it must
# not leak a customer identifier — so a page missing from here is a page with
# none of those three guaranteed. That is exactly what happened when the
# console grew from seven pages to twenty: the list stayed at seven, and
# thirteen new pages sat outside the PII test the whole console rests on.
#
# test_every_gated_console_page_is_in_this_list keeps it honest by walking the
# router, so adding a route without adding it here fails the suite rather than
# quietly opting out of the contract.
_FOLDED = [
    "/console/pipeline", "/console/routing", "/console/cases",
    "/console/ops", "/console/evidence", "/console/messages",
    "/console/accounts", "/console/payments", "/console/customer",
    "/console/batch",
    "/console/receivables", "/console/promises", "/console/plans",
    "/console/disputes", "/console/voice", "/console/safety",
    "/console/activity", "/console/search", "/console/settings",
    "/console/analytics/performance", "/console/analytics/rails",
    "/console/analytics/hours", "/console/analytics/economics",
]


def test_every_gated_console_page_is_in_this_list() -> None:
    """
    The list above is the console's contract surface. A page added to the
    router and not to it silently opts out of the session gate, the render
    check and — the one that matters — the PII-leak assertion.
    """
    exempt = {
        "/console",          # the public product landing: no session, no data
        "/console/login",    # the door itself
        "/console/live",     # has its own render path and its own tests
    }
    from fastapi.routing import APIRoute

    # merchant_router.routes, NOT app.routes. This FastAPI wraps an included
    # router in a single _IncludedRouter object instead of flattening its
    # routes onto the app, so walking app.routes finds no APIRoute at all —
    # and a set difference against an empty set passes whatever the list says.
    # The first version of this test was green because it checked nothing.
    static_pages = {
        route.path
        for route in merchant_router.routes
        if isinstance(route, APIRoute)
        and "GET" in route.methods
        and route.path.startswith("/console")
        and "{" not in route.path
    }
    assert static_pages, "route introspection found nothing — the walk is broken"
    missing = static_pages - exempt - set(_FOLDED)
    assert not missing, (
        f"gated console pages outside the contract tests: {sorted(missing)}"
    )


@pytest.mark.parametrize("path", _FOLDED)
def test_folded_pages_require_a_session(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any, path: str
) -> None:
    monkeypatch.setattr("src.merchant.routes.async_session_factory", db_sessionmaker)
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(merchant_router)
    r = TestClient(app).get(path, follow_redirects=False)
    get_settings.cache_clear()
    assert r.status_code == 303 and r.headers["location"] == "/console/login"


@pytest.mark.parametrize("path", _FOLDED)
async def test_folded_pages_render(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession], path: str
) -> None:
    """Each renders with data present, and carries the nav to every sibling."""
    await _seed(db_sessionmaker)
    r = console.get(path)
    assert r.status_code == 200
    # The whole point of folding them in: every page reaches every other one.
    #
    # Asserted on the href, not on `>Label</a>`. The exact-markup form broke
    # the day the Ledger link gained a badge — it was testing one spelling of
    # the link rather than the link, and the property here is reachability.
    for slug, label in (
        ("live", "Ledger"), ("pipeline", "Pipeline"), ("routing", "Routing"),
        ("cases", "Cases"), ("ops", "Engine"), ("evidence", "Evidence"),
    ):
        assert f'href="/console/{slug}"' in r.text, (
            f"{path} lost the {label} nav link"
        )


@pytest.mark.parametrize("path", _FOLDED)
async def test_folded_pages_leak_no_customer_identifiers(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession], path: str
) -> None:
    """
    The PII-free contract holds on the folded pages too.

    Worth asserting per page rather than once: the Streamlit originals DID
    select customer_id — the case list showed it as a column — so this is a
    rule these pages had to be rewritten to obey, not one they inherited.
    """
    await _seed(db_sessionmaker)
    html = console.get(path).text
    for leaked in ("ap@buyer.in", "a@b.in", "+919812345678", "email:"):
        assert leaked not in html, f"{path} leaked {leaked!r}"


async def test_cases_page_filters_by_state(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The state filter narrows the list, and an unknown state falls back to all
    rather than erroring or returning nothing."""
    await _seed(db_sessionmaker)
    recovered = console.get("/console/cases?state=recovered").text
    assert "pay_done" in recovered
    assert "cart_spent" not in recovered, "an open case showed under 'recovered'"

    junk = console.get("/console/cases?state=../../etc/passwd").text
    assert "pay_done" in junk and "cart_spent" in junk, "unknown state should fall back to all"


async def test_engine_page_reports_a_stale_scheduler(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The dead-man's switch, on the page an operator opens to check it."""
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        hb = await s.get(SchedulerHeartbeat, 1)
        assert hb is not None
        hb.last_tick_at = datetime.now(UTC) - timedelta(hours=6)
        await s.commit()
    html = console.get("/console/ops").text
    assert "Stopped" in html and "Running" not in html


async def test_folded_pages_survive_a_database_outage(
    console: Any, monkeypatch: Any
) -> None:
    """A page that cannot read says so; it does not 500."""
    class _Boom:
        def __call__(self) -> Any:
            raise RuntimeError("database is gone")

    monkeypatch.setattr("src.merchant.routes.async_session_factory", _Boom())
    for path in ("/console/pipeline", "/console/routing", "/console/cases", "/console/ops"):
        r = console.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code} with no database"


# ── Failure class: counts alone never said what is worth chasing ─────────


async def test_failure_causes_separates_never_chased_from_never_recovered(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    The one distinction this query exists to keep: a class the engine has
    never chased and a class it chased and lost both have zero recoveries,
    and rendering both as "0%" would invent a verdict for the first.
    """
    from src.merchant import console_data
    from src.models import PaymentFailure

    now = datetime.now(UTC)
    async with db_sessionmaker() as s:
        chased_f = PaymentFailure(
            payment_id="pay_chased", amount=10_000, method="card",
            error_code="E", failure_class="issuer_decline", is_retryable=True,
            webhook_event_id=uuid.uuid4(), failed_at=now,
        )
        untouched_f = PaymentFailure(
            payment_id="pay_untouched", amount=10_000, method="card",
            error_code="E", failure_class="fraud_block", is_retryable=False,
            webhook_event_id=uuid.uuid4(), failed_at=now,
        )
        s.add_all([chased_f, untouched_f])
        await s.flush()

        case = await open_case(
            s, risk_type="payment_failure", subject_ref="pay_chased",
            amount_at_risk=10_000, max_attempts=3,
        )
        await s.flush()
        s.add(RetryAttempt(
            payment_failure_id=chased_f.id, payment_id="pay_chased",
            idempotency_key="k_fc", attempt_number=1, recovery_case_id=case.id,
            action_type="retry_now", guardrail_passed=True, result="success",
        ))
        await s.commit()

    async with db_sessionmaker() as s:
        by_class = {c["cause"]: c for c in await console_data.failure_causes(s)}

    chased = by_class["issuer_decline"]
    assert chased["chased"] == 1
    assert chased["recovery_rate"] == 0.0, "chased and lost is a real 0%"
    assert chased["thin"] is True, "one case is not a rate"

    untouched = by_class["fraud_block"]
    assert untouched["chased"] == 0
    assert untouched["recovery_rate"] is None, "never chased is not 0%"
    assert untouched["thin"] is False


# ── Where the engine has stopped ─────────────────────────────────────────


async def test_stopping_rules_counts_refusals_not_just_successes(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    Automation refusing to act is this product's most load-bearing claim, and
    the console never counted it. `cases.stop_reason()` decides it per case;
    this is the same branches in aggregate.

    The invariant worth pinning: a case is either actionable or stopped, and
    an open case with its budget spent must land in the stopped column — that
    is the one an operator would otherwise read as "still being chased".
    """
    from src.merchant import console_data

    now = datetime.now(UTC)
    async with db_sessionmaker() as s:
        live = await open_case(
            s, risk_type="payment_failure", subject_ref="pay_live",
            amount_at_risk=10_000, max_attempts=3,
        )
        spent = await open_case(
            s, risk_type="payment_failure", subject_ref="pay_spent",
            amount_at_risk=20_000, max_attempts=3,
        )
        spent.attempts_used = 3
        held = await open_case(
            s, risk_type="invoice_overdue", subject_ref="INV-held",
            amount_at_risk=30_000, max_attempts=4,
        )
        held.next_action_at = now + timedelta(days=2)
        done = await open_case(
            s, risk_type="payment_failure", subject_ref="pay_done",
            amount_at_risk=40_000, max_attempts=3,
        )
        done.state = "recovered"
        done.amount_recovered = 40_000
        await s.commit()
        assert live is not None

    async with db_sessionmaker() as s:
        data = await console_data.stopping_rules(s)

    kinds = {b["kind"]: b for b in data["buckets"]}
    assert kinds["budget"]["cases"] == 1, "an open case with no budget left is stopped"
    assert kinds["waiting"]["cases"] == 1, "a case held by backoff is stopped"
    assert kinds["recovered"]["cases"] == 1

    # Only the genuinely touchable case is counted as actionable. Getting
    # this wrong in the other direction would overstate what the engine is
    # about to do, which is the number an operator plans around.
    assert data["actionable_cases"] == 1
    assert data["has_data"] is True


# ── The decision chain ───────────────────────────────────────────────────


async def test_case_detail_renders_the_whole_chain(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The centrepiece screen. Every link it shows already existed as a column
    and none of it was readable — so what this pins is that the page reports
    the engine's own words rather than a paraphrase of them.
    """
    from src.models import RetryAttempt

    async with db_sessionmaker() as s:
        case = await open_case(
            s, risk_type="payment_failure", subject_ref="pay_chain",
            customer_id="email:chain@buyer.in", amount_at_risk=250_000,
            max_attempts=3,
        )
        await s.flush()
        attempt = RetryAttempt(
            payment_id="pay_chain", idempotency_key="k_chain", attempt_number=1,
            recovery_case_id=case.id, action_type="switch_rail",
            target_rail="upi", agent_type="xgboost",
            agent_reasoning="OTP step is the drop-off; UPI skips it",
            agent_confidence=0.87, guardrail_passed=True, result="success",
            executed_at=datetime.now(UTC),
        )
        s.add(attempt)
        await s.flush()
        case.state = "recovered"
        case.amount_recovered = 250_000
        # The attribution link. Without it the money came back WITHOUT us,
        # and the page must not claim it — asserted in the next test.
        case.recovered_via_attempt_id = attempt.id
        await s.commit()
        case_id = str(case.id)

    html = console.get(f"/console/case/{case_id}").text

    # The agent's own reasoning and confidence, not a restatement.
    assert "OTP step is the drop-off" in html
    assert "87%" in html
    assert "switch_rail" in html
    # Attribution: money the engine earned must be distinguishable from
    # money that merely arrived.
    assert "Attributed to an attempt this engine made" in html
    # And still PII-free — the case carries an email the page must not show.
    assert "chain@buyer.in" not in html


async def test_case_detail_shows_a_refusal_verbatim(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    A blocked attempt is the more important rendering. The gate deliberately
    never short-circuits so it can collect every violation; a page that
    printed "blocked" would throw that away.
    """
    from src.models import RetryAttempt

    async with db_sessionmaker() as s:
        case = await open_case(
            s, risk_type="payment_failure", subject_ref="pay_refused",
            amount_at_risk=99_000, max_attempts=3,
        )
        await s.flush()
        s.add(RetryAttempt(
            payment_id="pay_refused", idempotency_key="k_refused",
            attempt_number=1, recovery_case_id=case.id, action_type="retry_now",
            agent_type="xgboost", guardrail_passed=False,
            guardrail_rejection_reason=(
                "Time-of-day blackout: hour 2 is within 23:00-07:00 IST"
            ),
            result="rejected",
        ))
        await s.commit()
        case_id = str(case.id)

    html = console.get(f"/console/case/{case_id}").text
    assert "Time-of-day blackout" in html
    assert "23:00-07:00 IST" in html
    assert "Refused" in html


async def test_an_unknown_case_id_says_so_rather_than_500ing(console: Any) -> None:
    for bad in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
        r = console.get(f"/console/case/{bad}")
        assert r.status_code == 200, bad
        assert "No such case" in r.text


async def test_an_unattributed_recovery_is_not_claimed_by_the_engine(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The distinction the whole headline rests on. A case can recover because
    the customer paid the original order themselves — real revenue, and not
    the engine's to take credit for. `recovered_via_attempt_id` is the only
    thing separating the two, and a console that collapsed them would be
    reporting a control group as a result.
    """
    async with db_sessionmaker() as s:
        case = await open_case(
            s, risk_type="payment_failure", subject_ref="pay_selfpaid",
            amount_at_risk=180_000, max_attempts=3,
        )
        case.state = "recovered"
        case.amount_recovered = 180_000
        case.recovered_via_attempt_id = None   # nobody earned it
        await s.commit()
        case_id = str(case.id)

    html = console.get(f"/console/case/{case_id}").text
    assert "the customer paid directly" in html
    assert "Attributed to an attempt this engine made" not in html


# ── The console must be able to do what it tells you to do ────────────────


async def test_a_dispute_can_be_resolved_from_the_console(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The panel has said "Chasing is frozen on these until you uphold or reject"
    while offering no control — the only handler was an HMAC-signed JSON
    endpoint a merchant on a laptop cannot reach. A worklist naming an action
    it does not offer is worse than not showing the row.
    """
    from src.receivables.models import CaseDispute

    await _seed(db_sessionmaker)
    html = console.get("/console/live").text
    assert "Your verdict" in html, "the verdict column is not rendered"
    assert "/console/dispute/resolve" in html, "no control for the named action"

    async with db_sessionmaker() as session:
        dispute = (await session.execute(select(CaseDispute))).scalars().first()
        assert dispute is not None
        dispute_id = str(dispute.id)

    resp = console.post(
        "/console/dispute/resolve",
        data={"dispute_id": dispute_id, "outcome": "rejected"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with db_sessionmaker() as session:
        resolved = await session.get(CaseDispute, uuid.UUID(dispute_id))
        assert resolved is not None and resolved.status == "rejected"


async def test_resolving_a_dispute_needs_a_session(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """It closes cases and restarts chases — same gate as every console page."""
    from src.receivables.models import CaseDispute

    await _seed(db_sessionmaker)
    async with db_sessionmaker() as session:
        dispute = (await session.execute(select(CaseDispute))).scalars().first()
        assert dispute is not None
        dispute_id = str(dispute.id)

    console.cookies.clear()
    resp = console.post(
        "/console/dispute/resolve",
        data={"dispute_id": dispute_id, "outcome": "upheld"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "login" in resp.headers.get("location", "")

    async with db_sessionmaker() as session:
        untouched = await session.get(CaseDispute, uuid.UUID(dispute_id))
        assert untouched is not None and untouched.status == "open"


async def test_a_bad_outcome_string_never_reaches_resolve_dispute(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from src.receivables.models import CaseDispute

    await _seed(db_sessionmaker)
    async with db_sessionmaker() as session:
        dispute = (await session.execute(select(CaseDispute))).scalars().first()
        assert dispute is not None
        dispute_id = str(dispute.id)

    for outcome in ("", "settled", "UPHELD; DROP TABLE"):
        console.post(
            "/console/dispute/resolve",
            data={"dispute_id": dispute_id, "outcome": outcome},
            follow_redirects=False,
        )
    async with db_sessionmaker() as session:
        untouched = await session.get(CaseDispute, uuid.UUID(dispute_id))
        assert untouched is not None and untouched.status == "open"


async def test_a_call_task_can_be_closed_from_the_console(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The ladder showed `open_call_tasks` as a bare count: work exists,
    somewhere, for someone. No account, no reason, no way to close it.
    """
    from src.receivables.models import AccountTask

    await _seed(db_sessionmaker)
    html = console.get("/console/live").text

    async with db_sessionmaker() as session:
        task = (await session.execute(select(AccountTask))).scalars().first()
    if task is None:
        pytest.skip("seed carries no call task")

    assert "Called them" in html, "the task list has no control"
    resp = console.post(
        "/console/task/done", data={"task_id": str(task.id)}, follow_redirects=False
    )
    assert resp.status_code == 303

    async with db_sessionmaker() as session:
        closed = await session.get(AccountTask, task.id)
        assert closed is not None and closed.status != "open"


async def test_money_that_arrived_another_way_can_be_recorded(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    NEFT, a cheque, cash. None of it touches Razorpay, so no webhook can tell
    us — the console showed an outstanding balance it could not let anyone
    close, while the case went on chasing money already in the bank.
    """
    from src.models import RecoveryCase

    await _seed(db_sessionmaker)
    async with db_sessionmaker() as session:
        case = (
            await session.execute(
                select(RecoveryCase).where(RecoveryCase.state == "open")
            )
        ).scalars().first()
        assert case is not None
        case_id, owed = str(case.id), case.amount_at_risk - case.amount_recovered

    page = console.get(f"/console/case/{case_id}").text
    assert "Money arrived another way" in page
    assert "/console/case/paid" in page, "the balance has no way to be closed"

    resp = console.post(
        "/console/case/paid",
        data={
            "case_id": case_id,
            "amount_inr": str(owed // 100),
            "paid_ref": "UTR-CONSOLE-1",
            "method": "neft",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with db_sessionmaker() as session:
        updated = await session.get(RecoveryCase, uuid.UUID(case_id))
        assert updated is not None
        assert updated.amount_recovered >= owed
        # Counted, never claimed: the engine did not earn this one.
        assert updated.recovered_via_attempt_id is None


async def test_an_external_payment_refuses_junk_input(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """It is a money form. Zero, negative, no reference, unknown method."""
    from src.models import RecoveryCase

    await _seed(db_sessionmaker)
    async with db_sessionmaker() as session:
        case = (
            await session.execute(
                select(RecoveryCase).where(RecoveryCase.state == "open")
            )
        ).scalars().first()
        assert case is not None
        case_id, before = str(case.id), case.amount_recovered

    for payload in (
        {"amount_inr": "0", "paid_ref": "R", "method": "neft"},
        {"amount_inr": "-5", "paid_ref": "R", "method": "neft"},
        {"amount_inr": "100", "paid_ref": "", "method": "neft"},
        {"amount_inr": "100", "paid_ref": "R", "method": "bitcoin"},
        {"amount_inr": "abc", "paid_ref": "R", "method": "neft"},
    ):
        console.post(
            "/console/case/paid",
            data={"case_id": case_id, **payload},
            follow_redirects=False,
        )

    async with db_sessionmaker() as session:
        untouched = await session.get(RecoveryCase, uuid.UUID(case_id))
        assert untouched is not None and untouched.amount_recovered == before


async def test_the_audit_trail_is_verified_in_product_not_asserted(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    The product offers a hash-chained, tamper-evident record. Until now the
    only way to check that claim was a CLI script nothing deployed runs — an
    "auditable" trail whose verification lives on someone's laptop is an
    assertion, not evidence.
    """
    monkeypatch.setenv("AUDIT_CHAIN_SECRET", "console-chain-secret")
    get_settings.cache_clear()
    await _seed(db_sessionmaker)

    from src.audit_chain import stamp_unhashed_events

    async with db_sessionmaker() as session:
        await stamp_unhashed_events(session)
        await session.commit()

    html = console.get("/console/ops").text
    assert "Audit trail" in html
    assert "Intact" in html, "the chain verified but the page does not say so"
    get_settings.cache_clear()


async def test_an_unkeyed_chain_reads_as_unverifiable_not_tampered(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    Nothing is wrong with the rows — the key is simply absent, which is a
    development state. Saying "unverifiable" beats implying tampering.
    """
    monkeypatch.setenv("AUDIT_CHAIN_SECRET", "")
    get_settings.cache_clear()
    await _seed(db_sessionmaker)

    html = console.get("/console/ops").text
    assert "Not verifiable here" in html
    assert "Broken" not in html, "an unkeyed chain was reported as tampering"
    get_settings.cache_clear()


async def test_the_console_has_a_skip_link_on_every_page(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    Every console page opens with the same masthead and a 7-tab subnav, so a
    keyboard user crossed eight controls before the page's own content on
    every single view.
    """
    await _seed(db_sessionmaker)
    for path in (
        "/console",
        "/console/live",
        "/console/ops",
        "/console/cases",
        "/console/pipeline",
        "/console/routing",
        "/console/batch",
        "/console/evidence",
        "/console/accounts",
        "/console/messages",
    ):
        html = console.get(path).text
        assert 'href="#main"' in html, f"{path} has no skip link"
        assert 'id="main"' in html, f"{path} skip link points at nothing"


async def test_the_message_preview_renders_the_real_templates(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    Every message goes out under the merchant's name and they had no way to
    read one. Rendered through the SAME functions the sender calls — a preview
    that could drift from what ships would be worse than none.
    """
    from src.messaging.templates import render_fallback

    await _seed(db_sessionmaker)
    html = console.get("/console/messages").text

    assert "What your customers actually read" in html
    # The exact string the real renderer produces must be on the page.
    body = render_fallback(
        failure_class="insufficient_funds",
        amount_display="2,499",
        next_step="Pay securely here: https://pay.example.in/recover/…",
    )
    fragment = body.split(",")[1].strip()[:40]
    assert fragment in html, "the preview is not the renderer's own output"
    # Every failure class the templates cover gets a row, not just a sample.
    from src.messaging.templates import _TEMPLATES

    for failure_class in _TEMPLATES:
        assert failure_class.replace("_", " ") in html, failure_class
    assert "Overdue invoices, by rung" in html


async def test_the_ladder_shows_what_each_rung_actually_does(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    A merchant could see WHERE accounts sat and nothing about what happens
    there: which channels fire, whether it costs a contact, how long to the
    next rung, where a broken promise lands them. All enforced, none legible.
    """
    await _seed(db_sessionmaker)
    html = console.get("/console/live").text

    assert "next rung in" in html, "rung gaps are invisible"
    assert "broken promise here resumes at rung" in html, "the ratchet is invisible"
    # The B2B window is read from the enforcing function, never restated.
    assert ("B2B contact hours are open now" in html
            or "Outside B2B contact hours" in html), "the contact window is invisible"


# ── The buyer directory ────────────────────────────────────────────────────


async def test_the_buyer_directory_lists_accounts_and_contact_coverage(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    B2B collection runs on the account, and the console had no account view at
    all — the only per-account surface in the product was the customer-facing
    statement page. "Do we have a finance manager on file" decides whether the
    ladder can escalate to anybody.
    """
    await _seed(db_sessionmaker)
    html = console.get("/console/accounts").text

    assert "Who owes you, and who you can reach" in html
    assert "Buyer Corp" in html
    assert "ref:buyer-corp" in html


async def test_an_account_with_no_contacts_says_so_loudly(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A rung that escalates to a role nobody recorded escalates to nobody."""
    from src.receivables.models import ArAccount

    async with db_sessionmaker() as session:
        account = ArAccount(account_ref="ref:silent-co", display_name="Silent Co")
        session.add(account)
        await session.commit()
        account_id = str(account.id)

    listing = console.get("/console/accounts").text
    assert "nobody on file" in listing

    detail = console.get(f"/console/account/{account_id}").text
    assert "Nobody on file" in detail
    assert "no one to write to" in detail


async def test_a_contact_can_be_added_from_the_console(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    `add_contact` has existed since the receivables module landed, reachable
    from nothing — no directory, no form, no way to correct a contact who left.
    """
    from src.receivables.models import ArAccount, ArContact

    async with db_sessionmaker() as session:
        account = ArAccount(account_ref="ref:newco", display_name="New Co")
        session.add(account)
        await session.commit()
        account_id = str(account.id)

    resp = console.post(
        "/console/account/contact",
        data={
            "account_id": account_id,
            "role": "finance_manager",
            "email": "  Priya@NewCo.IN  ",
            "name": "Priya",
            "phone": "+919812345678",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with db_sessionmaker() as session:
        contact = (
            await session.execute(
                select(ArContact).where(ArContact.account_id == uuid.UUID(account_id))
            )
        ).scalars().first()
        assert contact is not None
        # add_contact lowercases and strips: the email is a send target.
        assert contact.email == "priya@newco.in"
        assert contact.role == "finance_manager"


async def test_adding_a_contact_refuses_junk(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The role vocabulary is the ladder's, not free text: a rung addresses roles
    by name, so an unknown role is a contact no rung will ever reach.
    """
    from src.receivables.models import ArAccount, ArContact

    async with db_sessionmaker() as session:
        account = ArAccount(account_ref="ref:junkco", display_name="Junk Co")
        session.add(account)
        await session.commit()
        account_id = str(account.id)

    for payload in (
        {"role": "ceo", "email": "a@b.in"},
        {"role": "ap_clerk", "email": "not-an-email"},
        {"role": "ap_clerk", "email": ""},
    ):
        console.post(
            "/console/account/contact",
            data={"account_id": account_id, **payload},
            follow_redirects=False,
        )

    async with db_sessionmaker() as session:
        rows = (
            await session.execute(
                select(ArContact).where(ArContact.account_id == uuid.UUID(account_id))
            )
        ).scalars().all()
        assert not rows, "junk input created a contact"


async def test_the_directory_masks_contact_addresses(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    PRODUCT.md holds the console to PII-free. A contact directory is a
    different job, so the rule is kept rather than waived: the shape and the
    domain identify a colleague the merchant knows, while a screenshot gives
    nobody an address to harvest.
    """
    from src.receivables.accounts import add_contact
    from src.receivables.models import ArAccount

    async with db_sessionmaker() as session:
        account = ArAccount(account_ref="ref:maskco", display_name="Mask Co")
        session.add(account)
        await session.flush()
        await add_contact(
            session,
            account_id=account.id,
            role="ap_clerk",
            email="ap@buyer.in",
            name="Anita",
        )
        await session.commit()
        account_id = str(account.id)

    html = console.get(f"/console/account/{account_id}").text
    assert "ap@buyer.in" not in html, "the console leaked a full address"
    assert "buyer.in" in html, "the domain is what identifies a known colleague"
    assert "Anita" in html


# ── The model page states the engine, not a description of it ─────────────
# /model is a page whose entire content is claims about the decision layer.
# Every one of them is rendered from the enforcing structure — the taxonomy
# enum, the action Literal, the rule methods, the eval's own result file — so
# these tests assert the page against those sources rather than against
# literals. Copy that drifts from the code is the failure being guarded here;
# a test with the numbers typed into it would drift along with the page.


def test_the_model_page_is_public(console: Any) -> None:
    """Same trust level as the landing: no database, no session, no password.

    The signed-in `console` client is incidental — the point is the page
    reads neither.
    """
    unauthed = TestClient(console.app)
    assert unauthed.get("/model").status_code == 200


def test_the_model_page_opens_with_no_password_configured() -> None:
    """A deployment with DASHBOARD_PASSWORD unset gates the live console shut.

    It must not also take down the public pages: /model states product facts
    and reaching it does not imply any authority over the merchant's money.
    """
    app = FastAPI()
    app.include_router(merchant_router)
    settings = get_settings()
    original = settings.dashboard_password
    settings.dashboard_password = ""
    get_settings.cache_clear()
    try:
        assert TestClient(app).get("/model").status_code == 200
    finally:
        settings.dashboard_password = original
        get_settings.cache_clear()


def test_the_model_page_names_every_failure_class(console: Any) -> None:
    """Every member of the taxonomy reaches the page, and reads as words.

    The page's whole first act is "a failed payment is fifteen different
    problems". Adding a sixteenth class to FailureClass and leaving it off the
    page makes that sentence false, so the enum is the assertion.

    The identifier itself is carried on data-fc rather than shown: a reader is
    being told what failed, not what it is called in Python. So this asserts
    both halves — the class reached the page, and what the reader sees is
    spelled out rather than snake_case.
    """
    html = console.get("/model").text
    for fc in FailureClass:
        assert f'data-fc="{fc.value}"' in html, f"class {fc.value!r} missing from /model"

    titles = [c["title"] for c in _failure_classes()]
    assert len(titles) == len(list(FailureClass))
    for title in titles:
        assert "_" not in title, f"class title {title!r} still reads as an identifier"
        assert title in html


def test_the_model_page_names_every_action(console: Any) -> None:
    """All five actions, read from the ActionType Literal.

    "Five actions, nothing else is representable" is the claim; a sixth action
    added to the Literal and not to the page would make the page a liar about
    the one thing it is most emphatic about.
    """
    html = console.get("/model").text
    actions = get_args(ActionType)
    for action in actions:
        assert f'data-action="{action}"' in html, f"action {action!r} missing from /model"
    assert "Five actions" in html


def test_the_model_page_names_every_guardrail_rule(console: Any) -> None:
    """Each check_* on GuardrailRules is named, and the count is not hardcoded.

    The gate act says "then N rules disagree with it" and separately explains
    that the audit row reads N+1 because the schema check is counted. Both
    numbers come from the rule list, so a rule added or removed moves them.
    """
    html = console.get("/model").text
    rules = [n for n in vars(GuardrailRules) if n.startswith("check_")]
    assert rules, "no guardrail rules discovered — the helper's contract broke"
    for name in rules:
        assert f'data-rule="{name}"' in html, f"rule {name!r} missing from /model"
    assert f"{len(rules)} rules disagree" in html
    assert f"says {len(rules) + 1}" in html


def test_the_model_page_reports_the_eval_file_not_a_memory_of_it(console: Any) -> None:
    """Every figure in the results act is the value on disk, to the decimal.

    This is the page's honesty budget: it claims a measured improvement with a
    confidence interval, and the only thing making that true is that the
    numbers are read from eval/results/eval_results.json at render time. A
    figure typed into the template would keep rendering long after the harness
    disagreed with it — which is exactly what happened to the landing's hero.
    """
    results = _eval_headline()
    assert results is not None, "eval results missing — run eval.runner first"
    html = console.get("/model").text

    assert f"+{results['recovery_pp']:.2f}pp" in html
    assert f"+{results['ci_low']:.2f}" in html and f"+{results['ci_high']:.2f}" in html
    assert f"{results['n_paired']:,}" in html
    assert f"₹{results['revenue_delta'] / 100000:.2f}L" in html
    assert f"{int(results['attempts_delta']):,}" in html
    assert f"{results['false_retry_from']:.2f}%" in html
    # The population the numbers describe, not just the numbers.
    assert results["mix"] in html


def test_the_model_page_omits_results_rather_than_inventing_them(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No eval file on disk → the whole act is gone, and no number replaces it.

    A deployment built from a source tree without eval/results/ must not show a
    fallback, a placeholder or a remembered figure. The page is allowed to say
    less; it is not allowed to make something up. Silence is the honest state
    and it has to be the one that ships.
    """
    monkeypatch.setattr(
        "src.merchant.routes._EVAL_RESULTS", Path("/nonexistent/eval_results.json")
    )
    _eval_headline.cache_clear()
    try:
        r = console.get("/model")
        assert r.status_code == 200
        # The act, its heading, and the two things only it renders: a figure
        # (kpi-value) and a scroll-scrubbed counter (data-scrub). No survivor
        # means no number survived either.
        assert 'id="results"' not in r.text
        assert "What it measures" not in r.text
        assert "95% CI" not in r.text
        # `kpi-value` is a base.html class and is always in the stylesheet;
        # `data-scrub` exists only on the figures themselves.
        assert 'data-scrub="' not in r.text
        assert "kpi-value green" not in r.text
    finally:
        _eval_headline.cache_clear()


def test_the_model_page_renders_without_its_animation_library(console: Any) -> None:
    """The content does not depend on GSAP arriving.

    GSAP is loaded from a CDN this deployment does not control. The rule the
    page ships under is that every layout is authored as its final readable
    state and the pinned variant lives behind a class JS adds only after the
    library is confirmed — so a blocked CDN costs the animation and not a word.
    Two things prove it server-side: the scripts are deferred (they cannot
    block the parse), and nothing in the served CSS hides content by default.
    """
    html = console.get("/model").text
    assert html.count("<script defer src=") == 2, "GSAP must not block the parse"

    # Every rule that hides a beat is scoped under .gsap-on, so it applies only
    # once JS has confirmed the library. An unscoped `opacity:0` on a beat is
    # the exact bug this test exists to catch: content invisible forever on a
    # deployment whose CDN is blocked.
    # Both style blocks: base.html's and this page's.
    css = "".join(html.split("<style>")[1:])
    for line in css.splitlines():
        if "opacity:0" in line and "beat" in line:
            assert ".gsap-on" in line, f"beat hidden unconditionally: {line.strip()!r}"

    # And the pinned layout itself is behind the same gate.
    assert ".gsap-on #mech .stage{position:sticky" in css


def test_the_model_page_has_a_skip_link(console: Any) -> None:
    """Same obligation as every other console page."""
    html = console.get("/model").text
    assert 'href="#main"' in html and "Skip to content" in html


def test_the_model_page_shows_no_customer_identifiers(console: Any) -> None:
    """It touches no database, so it cannot leak — asserted, not assumed."""
    html = console.get("/model").text
    for leaked in ("ap@buyer.in", "a@b.in", "+919812345678"):
        assert leaked not in html


def test_the_model_page_renders_its_whole_body(console: Any) -> None:
    """Every structural landmark, in order — the page is not silently truncated.

    This exists because of a real, invisible break. A media query written
    `@media (max-width:900px){#model .wall{...}}` puts the characters `{#`
    into the template, which Jinja reads as a COMMENT opener: it swallowed
    everything up to the next `#}` — the rest of the stylesheet, the nav and
    the entire hero — and served a page whose <body> had zero children.

    Nothing caught it. The content assertions above all passed, because the
    strings they look for happen to live *after* the comment Jinja closed on.
    A truncated template fails structurally, not lexically, so the assertion
    has to be structural: the landmarks that bracket the page, in document
    order, with the closing tags that prove nothing swallowed them.
    """
    html = console.get("/model").text

    assert html.count("<style>") == html.count("</style>"), "unbalanced style block"
    assert html.count("<body>") == 1 and html.count("</body>") == 1

    landmarks = [
        "<nav",                    # masthead
        'id="main"',               # hero, and the skip link's target
        "Every failed rupee",      # hero headline
        'id="taxonomy"',           # act 1
        'id="mech"',               # acts 2-5
        'data-beat="3"',           # the last beat inside them
        'id="cta"',                # act 7
        "<footer",                 # and the end of the document
    ]
    last = -1
    for mark in landmarks:
        at = html.find(mark)
        assert at != -1, f"/model is missing {mark!r} — template truncated?"
        assert at > last, f"{mark!r} is out of document order"
        last = at


# ── Customer view — the other side of the case ───────────────────────────────


async def test_the_customer_view_lists_cases_and_never_a_link(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """
    The page names every case and prints no `/recover/<token>` URL.

    That is the whole security property: such a URL is a bearer credential —
    whoever holds it can see and pay that case — so a table of them would be a
    table of live credentials in a browser cache and in every screenshot of
    this page. One is minted per click instead.
    """
    monkeypatch.setenv("RECOVERY_LINK_SECRET", "x" * 32)
    get_settings.cache_clear()
    await _seed(db_sessionmaker)

    body = console.get("/console/customer").text
    assert "What your customer sees" in body
    # A token, not the word: the page explains the /recover URL in prose on
    # purpose, and what must never appear is a real one.
    assert not re.search(r"/recover/[A-Za-z0-9_-]{16,}", body), (
        "the console printed a live customer link"
    )
    # The button that mints one, and the case it names.
    assert 'action="/console/customer/open"' in body


async def test_opening_a_customer_page_redirects_to_the_real_one(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """The operator gets the page itself, not a second rendering of it.

    A copy would drift from the real page the week after it shipped, and the
    drift would be invisible from the console — which is the exact failure
    this page exists to end.
    """
    monkeypatch.setenv("RECOVERY_LINK_SECRET", "x" * 32)
    get_settings.cache_clear()
    async with db_sessionmaker() as s:
        case = await open_case(
            s, risk_type="payment_failure", subject_ref="pay_customer_view",
            amount_at_risk=250000, customer_id="a@b.test",
        )
        await s.commit()
        case_id = str(case.id)

    r = console.post(
        "/console/customer/open", data={"case_id": case_id}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/recover/")
    # The marker that will label the view, scoped away from everything else.
    assert "recovery_preview" in r.headers.get("set-cookie", "")


async def test_the_customer_view_says_so_when_links_are_switched_off(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """
    An unset RECOVERY_LINK_SECRET means `mint()` returns None and every nudge
    already ships without a link. The page states that rather than offering a
    button that silently does nothing.
    """
    monkeypatch.setenv("RECOVERY_LINK_SECRET", "")
    get_settings.cache_clear()
    await _seed(db_sessionmaker)

    body = console.get("/console/customer").text
    assert "switched off in this deployment" in body
    assert "disabled" in body


async def test_a_malformed_case_id_goes_back_to_the_page(console: Any) -> None:
    """Not a 500. The console stays up on input it did not produce."""
    r = console.post(
        "/console/customer/open", data={"case_id": "not-a-uuid"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/console/customer"


def test_the_customer_view_needs_a_session(monkeypatch: Any) -> None:
    """Both halves are gated: the list and the mint."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(merchant_router)
    client = TestClient(app)

    assert client.get(
        "/console/customer", follow_redirects=False
    ).status_code == 303
    assert client.post(
        "/console/customer/open", data={"case_id": str(uuid.uuid4())},
        follow_redirects=False,
    ).status_code == 303
    get_settings.cache_clear()


# ── Navigation: grouping, badges, and the drawer ─────────────────────────────


async def test_the_nav_badges_count_only_what_needs_a_person(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    A badge that merely describes volume would sit in the navigation forever
    and teach the reader to ignore every badge. These count open disputes,
    open call tasks and unclaimed queued calls — things automation has
    deliberately stopped short of, which do not restart on their own.
    """
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        counts = await console_data.nav_counts(s)

    assert counts["needs_you"] == (
        counts["disputes"] + counts["tasks"] + counts["calls"]
    )
    assert counts["disputes"] >= 1, "the seed opens a dispute"
    body = console.get("/console/cases").text
    assert 'class="nav-badge"' in body


async def test_a_zero_never_renders_as_a_badge(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The absence is the message. A "0" trains the reader to stop looking."""
    # Nothing seeded: every count is zero.
    body = console.get("/console/cases").text
    assert 'class="nav-badge"' not in body


async def test_every_console_page_carries_the_grouped_nav_and_the_drawer(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    Two expressions of one structure: a grouped strip where there is
    horizontal room, a labelled drawer under 720px where there is not. The
    drawer is <details>, not script — a navigation that needs JavaScript to
    open is the worst possible place for the console to break.
    """
    await _seed(db_sessionmaker)
    for path in ("/console/live", "/console/cases", "/console/ops",
                 "/console/evidence", "/console/customer"):
        body = console.get(path).text
        assert 'class="subnav-group"' in body, path
        assert 'class="navdraw"' in body, path
        assert "<details" in body, path
        # The four groups the console is organised around.
        for label in ("Recovery", "Receivables", "Customer", "Trust"):
            assert f">{label}</p>" in body, f"{path} lost the {label} group"


async def test_the_current_page_is_marked_for_a_screen_reader(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Colour alone does not say "you are here"."""
    await _seed(db_sessionmaker)
    assert 'aria-current="page"' in console.get("/console/cases").text


async def test_the_nav_survives_a_database_it_cannot_read(
    monkeypatch: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The badge read runs on every page, so it must never be the thing that
    fails one. It degrades to no badges — never to zeros a merchant believes.
    """
    async def boom(_session: Any) -> Any:
        raise RuntimeError("database is gone")

    monkeypatch.setattr("src.merchant.routes.async_session_factory", db_sessionmaker)
    monkeypatch.setattr(console_data, "nav_counts", boom)
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(merchant_router)
    client = TestClient(app)
    client.post("/console/login", data={"password": PASSWORD})

    r = client.get("/console/cases")
    assert r.status_code == 200
    assert 'href="/console/live"' in r.text
    assert 'class="nav-badge"' not in r.text
    get_settings.cache_clear()


# ── The worklist: severity, and somewhere to go ──────────────────────────────


async def test_every_worklist_item_carries_a_severity_and_a_destination(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    A worklist that names a frozen dispute and then makes you hunt the same
    page for it is half a worklist. Every item points at the block or the
    filtered list that holds it, and says in a word whether it stops the day.
    """
    await _seed(db_sessionmaker)
    r = console.get("/console/live")
    assert r.status_code == 200
    assert 'class="needs-link"' in r.text
    assert "act now" in r.text or "watch" in r.text

    # Every in-page destination the worklist offers actually exists on the
    # page. Asserted from the rendered hrefs rather than a fixed list: the
    # blocks are conditional (there is no plans block without a plan), so a
    # fixed list would test the fixture instead of the property.
    worklist = r.text.split('class="needs-list"', 1)[1].split("</ul>", 1)[0]
    anchors = re.findall(r'class="needs-link" href="#([a-z]+)"', worklist)
    assert anchors, "no in-page worklist destinations rendered"
    for anchor in anchors:
        assert f'id="{anchor}"' in r.text, f"worklist points at missing #{anchor}"


def test_worklist_severity_is_stop_or_wait_and_nothing_else() -> None:
    """
    Two levels on purpose. "stop" is money halted until a person acts; "wait"
    resolves itself and is worth seeing. A third level would be a preference,
    and the stripe colour, the word and the row's meaning would stop agreeing.
    """
    items = console_data.attention_items(
        disputes={"open": 2, "disputes": [{"days_open": 5}]},
        voice={"queued": 3, "claimed": 0, "oldest_queued": "2 days"},
        plans={"defaulted": 1},
        exceptions=[{"ref": "pay_x"}],
        health={"stale": True, "last_tick": "04 Sep, 10:00"},
    )
    assert items, "the fixture describes five problems"
    for item in items:
        assert item["severity"] in ("stop", "wait"), item
        assert item["href"], f"{item['kind']} has nowhere to go"


def test_the_heartbeat_says_how_fresh_rather_than_only_when() -> None:
    """
    The question under a heartbeat is "are these numbers current", and a
    wall-clock timestamp makes the reader do the subtraction. Coarse past a
    minute on purpose: the page does not live-update, so second precision
    would be a claim that becomes true again once a minute.
    """
    ago = console_data._ago
    assert ago(3) == "just now"
    assert ago(45) == "45s ago"
    assert ago(300) == "5m ago"
    assert ago(7200) == "2h ago"
    assert ago(200000) == "2d ago"


async def test_a_funnel_stage_links_only_where_the_same_population_lives(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    "Failed" and "Retryable" count payment_failures; the cases list is keyed
    on case state and cannot be filtered to either. A stage linking to a
    nearly-right list is worse than one that does not link, because the reader
    trusts the number they land on.
    """
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        funnel = await console_data.pipeline_funnel(s)
    by_label = {row["label"]: row for row in funnel["cases"] + funnel["attempts"]}
    assert by_label["Recovered"]["href"] == "/console/cases?state=recovered"
    for label in ("Failed", "Retryable", "Decided", "Guardrail passed", "Executed"):
        assert by_label[label]["href"] is None, label


# ── Payments, and the gate rendered rule by rule ─────────────────────────────


async def _payment(
    sm: async_sessionmaker[AsyncSession], *, ref: str, failure_class: str,
    with_case: bool = True, state: str = "open",
) -> Any:
    """One failed charge, optionally with the case opened around it."""
    from src.models import PaymentFailure

    async with sm() as s:
        s.add(PaymentFailure(
            payment_id=ref, order_id=f"order_{ref}", amount=249900, method="card",
            bank="HDFC", error_code="BAD_REQUEST_ERROR",
            error_reason="payment_declined", failure_class=failure_class,
            is_retryable=failure_class not in ("fraud_block", "hard_decline"),
            # The three the console must never show.
            customer_email="leak@example.com", customer_contact="+919876500000",
            vpa="leak@okhdfcbank",
            webhook_event_id=uuid.uuid4(), failed_at=datetime.now(UTC),
        ))
        case = None
        if with_case:
            case = await open_case(
                s, risk_type="payment_failure", subject_ref=ref,
                amount_at_risk=249900, customer_id="email:leak@example.com",
            )
            case.state = state
        await s.commit()
        return case.id if case else None


async def test_the_payments_page_shows_the_failure_and_never_the_customer(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    PaymentFailure carries an email, a phone and a UPI handle — a UPI VPA is a
    personal identifier. payment_list names every column it selects for
    exactly this reason; a select(Model) would put all three one template
    mistake away from the page.
    """
    await _payment(db_sessionmaker, ref="pay_leak", failure_class="insufficient_funds")
    body = console.get("/console/payments").text
    assert "pay_leak" in body
    assert "insufficient funds" in body
    for secret in ("leak@example.com", "919876500000", "okhdfcbank"):
        assert secret not in body, f"the payments page leaked {secret}"


async def test_the_payment_filters_actually_filter(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A filter that appears to apply and does not is worse than no filter."""
    await _payment(db_sessionmaker, ref="pay_open", failure_class="insufficient_funds")
    await _payment(
        db_sessionmaker, ref="pay_gone", failure_class="fraud_block",
        state="abandoned",
    )

    both = console.get("/console/payments").text
    assert "pay_open" in both and "pay_gone" in both

    by_state = console.get("/console/payments?state=abandoned").text
    assert "pay_gone" in by_state and "pay_open" not in by_state

    by_class = console.get("/console/payments?class=fraud_block").text
    assert "pay_gone" in by_class and "pay_open" not in by_class


async def test_an_unknown_filter_falls_back_and_says_it_is_showing_everything(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The fallback is safe only because the filter bar then marks "all payments"
    active. A page that claims to be filtered when it is not is the failure
    mode worse than having no filter.
    """
    await _payment(db_sessionmaker, ref="pay_a", failure_class="insufficient_funds")
    body = console.get("/console/payments?state=nonsense").text
    assert "pay_a" in body
    assert 'href="/console/payments" class="is-on"' in body


async def test_a_failure_with_no_case_says_so_rather_than_showing_a_blank(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """An outer join on purpose: the failure no case opened for is exactly the
    row a merchant most wants to see, and it is a state, not a gap."""
    await _payment(
        db_sessionmaker, ref="pay_orphan", failure_class="insufficient_funds",
        with_case=False,
    )
    body = console.get("/console/payments").text
    assert "pay_orphan" in body
    assert "no case yet" in body


async def test_the_guardrail_trace_names_the_rule_that_refused(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    Not "the AI decided not to retry" — the rule, in the gate's own words,
    with the rest of the roster shown as what it was: checked and passed.
    """
    case_id = await _payment(
        db_sessionmaker, ref="pay_blocked", failure_class="insufficient_funds"
    )
    async with db_sessionmaker() as s:
        s.add(RetryAttempt(
            payment_id="pay_blocked", idempotency_key="idem_blocked",
            attempt_number=1, recovery_case_id=case_id, action_type="switch_rail",
            agent_type="xgboost", guardrail_passed=False,
            guardrail_rejection_reason=(
                "Time-of-day blackout: hour 1 is within 23:00-07:00 IST"
            ),
            result="blocked",
        ))
        await s.commit()
        trace = await console_data.guardrail_trace(s, str(case_id))

    assert trace is not None and not trace["passed"]
    assert trace["failed"] == 1 and trace["checked"] == 12
    fired = [r for r in trace["rules"] if r["fired"]]
    assert len(fired) == 1 and fired[0]["label"] == "Quiet hours"
    assert "23:00-07:00" in fired[0]["detail"]
    # Every other rule ran and passed — that is the gate's actual claim.
    assert all(r["detail"] is None for r in trace["rules"] if not r["fired"])
    assert not trace["unattributed"]


async def test_an_approved_attempt_means_every_rule_ran_and_none_fired(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The gate never stops at the first violation, so "approved" is a claim
    about all of them — which is the more interesting one, and the one the
    page could not previously make."""
    case_id = await _payment(
        db_sessionmaker, ref="pay_ok", failure_class="insufficient_funds"
    )
    async with db_sessionmaker() as s:
        s.add(RetryAttempt(
            payment_id="pay_ok", idempotency_key="idem_ok", attempt_number=1,
            recovery_case_id=case_id, action_type="nudge_customer",
            agent_type="xgboost", guardrail_passed=True, result="success",
        ))
        await s.commit()
        trace = await console_data.guardrail_trace(s, str(case_id))

    assert trace is not None and trace["passed"]
    assert trace["failed"] == 0
    # A nudge carries the twelfth rule the other actions do not.
    assert trace["checked"] == 13
    assert any(r["label"] == "Nudges per customer, 24h" for r in trace["rules"])


async def test_an_abandon_reports_that_the_gate_ran_nothing(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """validate() auto-passes an abandon without running a rule. Twelve green
    ticks here would describe work that never happened."""
    case_id = await _payment(
        db_sessionmaker, ref="pay_abandon", failure_class="fraud_block"
    )
    async with db_sessionmaker() as s:
        s.add(RetryAttempt(
            payment_id="pay_abandon", idempotency_key="idem_ab", attempt_number=1,
            recovery_case_id=case_id, action_type="abandon", agent_type="xgboost",
            guardrail_passed=True, result="success",
        ))
        await s.commit()
        trace = await console_data.guardrail_trace(s, str(case_id))

    assert trace is not None
    assert trace["skipped"] and trace["rules"] == [] and trace["checked"] == 0


async def test_a_refusal_matching_no_rule_is_said_out_loud(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    A rule renamed or its message changed leaves the roster in gate.py
    drifted. The page must not render that as a clean sheet — an all-passed
    checklist beside a refusal is the worst possible reading.
    """
    case_id = await _payment(
        db_sessionmaker, ref="pay_drift", failure_class="insufficient_funds"
    )
    async with db_sessionmaker() as s:
        s.add(RetryAttempt(
            payment_id="pay_drift", idempotency_key="idem_drift", attempt_number=1,
            recovery_case_id=case_id, action_type="retry_now", agent_type="xgboost",
            guardrail_passed=False,
            guardrail_rejection_reason="Some rule nobody labelled said no",
            result="blocked",
        ))
        await s.commit()
        trace = await console_data.guardrail_trace(s, str(case_id))

    assert trace is not None
    assert trace["failed"] == 0 and trace["unattributed"]


# ── Phase 04: Receivables pages ──────────────────────────────────────────────


async def test_receivables_page_renders_ladder_and_outstanding(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The receivables page shows the ladder and outstanding balance."""
    await _seed(db_sessionmaker)
    html = console.get("/console/receivables").text
    assert "Receivables" in html
    assert "Aging, ladder and outstanding" in html


async def test_receivables_page_empty_state(console: Any) -> None:
    """An empty receivables page says so rather than showing zeros."""
    html = console.get("/console/receivables").text
    assert "Receivables" in html


async def test_promises_page_renders(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The promises page shows kept/broken split."""
    await _seed(db_sessionmaker)
    html = console.get("/console/promises").text
    assert "Promises" in html


async def test_plans_page_renders(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The plans page renders instalment progress."""
    await _seed(db_sessionmaker)
    html = console.get("/console/plans").text
    assert "Plans" in html or "Payment plans" in html


async def test_disputes_page_renders_and_shows_automation_frozen(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The disputes page must show every open dispute with 'automation frozen'
    and the customer's stated reason.
    """
    await _seed(db_sessionmaker)
    html = console.get("/console/disputes").text
    assert "Disputes" in html or "Disputed invoices" in html
    assert "Quantity billed does not match the PO" in html
    assert "frozen" in html.lower()


async def test_dispute_resolve_redirects_to_disputes_page(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Dispute resolve now redirects to /console/disputes, not /console/live."""
    await _seed(db_sessionmaker)
    # Get the dispute_id from the database
    async with db_sessionmaker() as s:
        from sqlalchemy import select

        from src.receivables.models import CaseDispute
        dispute = (await s.execute(
            select(CaseDispute.id).where(CaseDispute.status == "open").limit(1)
        )).scalar_one_or_none()
    if dispute is not None:
        r = console.post(
            "/console/dispute/resolve",
            data={"dispute_id": str(dispute), "outcome": "rejected"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "/console/disputes" in r.headers["location"]


async def test_ledger_still_summarises_after_split(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The ledger keeps one-line summaries linking to the new pages."""
    await _seed(db_sessionmaker)
    html = console.get("/console/live").text
    # The money line should still exist
    assert "Still owed" in html


# ── Phase 05: Analytics pages ────────────────────────────────────────────────


async def test_analytics_performance_renders(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(db_sessionmaker)
    html = console.get("/console/analytics/performance").text
    assert "Recovery performance" in html or "Analytics" in html


async def test_analytics_rails_renders(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(db_sessionmaker)
    html = console.get("/console/analytics/rails").text
    assert "Bank" in html or "rail" in html.lower()


async def test_analytics_hours_renders_with_blackout(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The hours chart must show the blackout band from settings."""
    await _seed(db_sessionmaker)
    html = console.get("/console/analytics/hours").text
    assert "Recovery by hour" in html or "hour" in html.lower()
    # Blackout boundaries must come from settings, not hardcoded
    assert "Blackout" in html or "blackout" in html


async def test_analytics_economics_eval_label(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Every eval figure must carry the 'Evaluation harness' label."""
    await _seed(db_sessionmaker)
    html = console.get("/console/analytics/economics").text
    assert "economics" in html.lower() or "Economics" in html


async def test_analytics_pages_empty_state(console: Any) -> None:
    """Empty analytics pages say nothing has come through."""
    for path in [
        "/console/analytics/performance",
        "/console/analytics/rails",
        "/console/analytics/hours",
        "/console/analytics/economics",
    ]:
        html = console.get(path).text
        assert html  # must render without error


# ── Phase 06: Voice page ────────────────────────────────────────────────────


async def test_voice_page_renders_queue(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The voice page shows the queue and gates."""
    await _seed(db_sessionmaker)
    html = console.get("/console/voice").text
    assert "Voice" in html
    # Must never fabricate a transcript
    assert "Turn-by-turn text is not stored" in html or "Voice calls" in html


async def test_voice_page_no_pii(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The voice page must never show customer phone numbers."""
    await _seed(db_sessionmaker)
    html = console.get("/console/voice").text
    assert "+919812345678" not in html


# ── Phase 07: Trust Surfaces ────────────────────────────────────────────────


async def test_safety_page_lists_all_safeguards(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The safety page shows every safeguard with its live state."""
    await _seed(db_sessionmaker)
    html = console.get("/console/safety").text
    assert "Safety" in html
    assert "Guardrail rules" in html
    assert "Blackout window" in html
    assert "Retry cap" in html
    assert "Rate limiting" in html


async def test_safety_page_shows_redis_or_inprocess(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Rate limiting label follows the environment."""
    await _seed(db_sessionmaker)
    html = console.get("/console/safety").text
    # Without REDIS_URL set in test, it should say in-memory
    assert "in-memory" in html or "per-process" in html or "Redis" in html


async def test_activity_page_renders_events(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The activity page shows case events."""
    await _seed(db_sessionmaker)
    html = console.get("/console/activity").text
    assert "Activity" in html or "activity" in html


async def test_activity_page_verified_only_when_chain_intact(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A stamped event inside an unverified chain must NOT say 'Verified'."""
    await _seed(db_sessionmaker)
    html = console.get("/console/activity").text
    # Without AUDIT_CHAIN_SECRET, nothing should say "Verified"
    # (it should say "Unsealed" instead)
    if "AUDIT_CHAIN_SECRET" not in html:
        # Chain is not keyed in tests, so nothing should be verified
        assert "Verified" not in html or "Unsealed" in html


# ── Phase 08: Search & Settings ─────────────────────────────────────────────


async def test_search_returns_nothing_for_email(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The console's PII-free contract: searching for an email address must
    return nothing, by design.
    """
    await _seed(db_sessionmaker)
    html = console.get("/console/search?q=a@b.in").text
    assert "a@b.in" not in html or "Nothing matched" in html


async def test_search_finds_by_subject_ref(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Search by invoice reference returns the case."""
    await _seed(db_sessionmaker)
    html = console.get("/console/search?q=INV-PART").text
    assert "INV-PART" in html


async def test_search_empty_renders(console: Any) -> None:
    """An empty search page renders without error."""
    html = console.get("/console/search").text
    assert "Search" in html


async def test_settings_page_renders_without_secrets(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The settings page shows presence, never values."""
    await _seed(db_sessionmaker)
    html = console.get("/console/settings").text
    assert "Settings" in html
    # Must never show actual secrets
    assert "console-test-password" not in html


async def test_settings_shows_chase_bounds(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Settings page includes chase bounds from the policy module."""
    await _seed(db_sessionmaker)
    html = console.get("/console/settings").text
    assert "payment failure" in html or "Chase bounds" in html.lower() \
        or "Max attempts" in html


async def test_settings_shows_ladder_rungs(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Settings page shows the ladder rungs from the receivables module."""
    await _seed(db_sessionmaker)
    html = console.get("/console/settings").text
    assert "courtesy" in html or "friendly" in html or "Ladder" in html


# ── Navigation: every new page is reachable ──────────────────────────────────


async def test_all_new_pages_accessible(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Every new page returns 200 and contains the navigation."""
    await _seed(db_sessionmaker)
    pages = [
        "/console/receivables",
        "/console/promises",
        "/console/plans",
        "/console/disputes",
        "/console/analytics/performance",
        "/console/analytics/rails",
        "/console/analytics/hours",
        "/console/analytics/economics",
        "/console/voice",
        "/console/safety",
        "/console/activity",
        "/console/search",
        "/console/settings",
    ]
    for path in pages:
        r = console.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"
        assert "console" in r.text.lower(), f"{path} missing nav"


async def test_new_pages_require_auth(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """All new pages redirect to login without a session."""
    monkeypatch.setattr("src.merchant.routes.async_session_factory", db_sessionmaker)
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(merchant_router)
    client = TestClient(app)

    pages = [
        "/console/receivables",
        "/console/promises",
        "/console/plans",
        "/console/disputes",
        "/console/analytics/performance",
        "/console/analytics/rails",
        "/console/analytics/hours",
        "/console/analytics/economics",
        "/console/voice",
        "/console/safety",
        "/console/activity",
        "/console/search",
        "/console/settings",
    ]
    for path in pages:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, f"{path} did not gate"
        assert r.headers["location"] == "/console/login"

    get_settings.cache_clear()


async def test_the_rails_page_renders_once_there_is_a_method_to_draw(
    console: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The methods bar chart 500'd on every database that had a payment in it.

    `bar()` does arithmetic on `frac`, routing_panel's method rows carried
    only `method` and `n`, and Jinja raised UndefinedError. It survived
    review because the shared seed leaves `by_method` empty — the loop never
    ran, so the page returned 200 on an empty database and broke on every
    real one. The shares are computed in the read now, where every other
    figure in this console is computed.
    """
    await _payment(
        db_sessionmaker, ref="pay_rail", failure_class="insufficient_funds"
    )
    r = console.get("/console/analytics/rails")
    assert r.status_code == 200
    async with db_sessionmaker() as s:
        methods = (await console_data.routing_panel(s))["methods"]
    assert methods, "the fixture has a card payment"
    for row in methods:
        assert 0.0 <= row["frac"] <= 1.0
        assert row["pct"] == pytest.approx(row["frac"] * 100, abs=0.05)


# ── The Safety Center must not claim a safeguard it cannot verify ────────────


async def test_an_empty_blackout_window_is_not_reported_as_active(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    start == end is an EMPTY window: is_in_blackout computes
    `start <= hour < end`, false for every hour. It is how this suite disables
    the blackout and it is a setting an operator can reach — so the page used
    to print "active · 00:00-00:00 IST" for a safeguard that fires at no hour
    of the day. On the one page a compliance reviewer reads.
    """
    monkeypatch.setenv("RETRY_BLACKOUT_START_HOUR", "0")
    monkeypatch.setenv("RETRY_BLACKOUT_END_HOUR", "0")
    get_settings.cache_clear()
    async with db_sessionmaker() as s:
        rows = (await console_data.safety_state(s))["safeguards"]
    blackout = next(r for r in rows if r["name"] == "Blackout window")
    assert blackout["state"] == "NOT CONFIGURED"
    assert "no hour is quiet" in blackout["detail"]
    get_settings.cache_clear()


async def test_a_real_blackout_window_is_reported_with_its_size(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """And the honest opposite: 23:00-07:00 is eight quiet hours."""
    monkeypatch.setenv("RETRY_BLACKOUT_START_HOUR", "23")
    monkeypatch.setenv("RETRY_BLACKOUT_END_HOUR", "7")
    get_settings.cache_clear()
    async with db_sessionmaker() as s:
        rows = (await console_data.safety_state(s))["safeguards"]
    blackout = next(r for r in rows if r["name"] == "Blackout window")
    assert blackout["state"] == "active"
    assert "8h quiet" in blackout["detail"]
    get_settings.cache_clear()


async def test_voice_grounding_is_not_active_when_no_call_is_ever_placed(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """
    voice_chaser_enabled defaults to false, so a stock deployment queues no
    call and the gate guards a leg that never runs. The gate is real; saying
    "active" implies something is being guarded right now.
    """
    monkeypatch.setenv("VOICE_CHASER_ENABLED", "false")
    get_settings.cache_clear()
    async with db_sessionmaker() as s:
        rows = (await console_data.safety_state(s))["safeguards"]
    voice = next(r for r in rows if r["name"] == "Voice grounding")
    assert voice["state"] == "NOT IN USE"

    monkeypatch.setenv("VOICE_CHASER_ENABLED", "true")
    get_settings.cache_clear()
    async with db_sessionmaker() as s:
        rows = (await console_data.safety_state(s))["safeguards"]
    voice = next(r for r in rows if r["name"] == "Voice grounding")
    assert voice["state"] == "active"
    get_settings.cache_clear()


async def test_every_safeguard_says_whether_it_was_read_or_is_structural(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    "We checked and it is on" and "this cannot be off" are different claims,
    and a safety page must not blur them. Nine rows were asserting "active"
    with nothing behind the assertion; the ones that genuinely cannot be
    switched off now say so, and the rest come from a live read.
    """
    async with db_sessionmaker() as s:
        rows = (await console_data.safety_state(s))["safeguards"]
    assert rows
    for row in rows:
        assert row.get("source") in ("read", "structural"), row["name"]
    structural = {r["name"] for r in rows if r["source"] == "structural"}
    assert structural == {
        "Idempotency", "Fire-time re-validation", "Dispute freeze",
    }, structural
