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

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import src.receivables.models  # noqa: F401  — register the AR tables on Base
from src.cases import open_case, record_promise
from src.config import get_settings
from src.merchant.routes import router as merchant_router
from src.models import (
    PromiseToPay,
    RetryAttempt,
    SchedulerHeartbeat,
    VoiceCallQueue,
)
from src.receivables.models import ArAccount, ArContactLog, CaseDispute, MerchantAlert

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

_FOLDED = ["/console/pipeline", "/console/routing", "/console/cases",
           "/console/ops", "/console/evidence"]


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
    for label in ("Ledger", "Pipeline", "Routing", "Cases", "Engine", "Evidence"):
        assert f">{label}</a>" in r.text, f"{path} lost the {label} nav link"


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
