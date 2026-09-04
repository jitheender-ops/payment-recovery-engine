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

_FOLDED = ["/console/pipeline", "/console/routing", "/console/cases",
           "/console/ops", "/console/evidence", "/console/messages",
           "/console/accounts"]


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
