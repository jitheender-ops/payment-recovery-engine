"""
The account statement page, its account-scoped token, and the two
single-invoice fixes that shipped alongside it.

The token tests carry most of the weight. There are now two token scopes
signed with one secret, and the failure that matters is not forgery — it is
scope confusion: an account id read as a case id, or the reverse, would
serve a stranger the wrong page with a perfectly valid signature. The
payload's field count is what makes that impossible, so it is what these
pin.

The route-level dispute/plan tests exist because there were none: both
POST handlers were only ever exercised through their underlying functions,
which is not a regression net for the form markup those handlers parse.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import recovery_link
from src.cases import open_case
from src.config import get_settings
from src.customer.routes import router as customer_router
from src.database import get_session
from src.receivables.models import ArAccount, CaseDispute

LINK_SECRET = "statement-test-secret"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: Any) -> Any:
    get_settings.cache_clear()
    monkeypatch.setenv("RECOVERY_LINK_SECRET", LINK_SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pay.example.in")
    monkeypatch.setenv("MERCHANT_NAME", "Kirana Supply Co")
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(db_sessionmaker: async_sessionmaker[AsyncSession]) -> TestClient:
    app = FastAPI()
    app.include_router(customer_router)

    async def override() -> Any:
        async with db_sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = override
    return TestClient(app)


async def _account_with_invoices(
    sm: async_sessionmaker[AsyncSession],
    *,
    refs: list[tuple[str, int, int]],
    account_ref: str = "ref:buyer-corp",
    disputed: str | None = None,
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """(account_id, {invoice ref: case id}) — refs are (ref, at_risk, recovered)."""
    now = datetime.now(UTC)
    async with sm() as s:
        account = ArAccount(account_ref=account_ref, display_name="Buyer Corp")
        s.add(account)
        await s.flush()
        case_ids = {}
        for i, (ref, at_risk, recovered) in enumerate(refs):
            case = await open_case(
                s, risk_type="invoice_overdue", subject_ref=ref,
                customer_id="email:ap@buyer.in", amount_at_risk=at_risk,
                account_id=account.id, due_at=now - timedelta(days=10 - i),
                max_attempts=4,
            )
            case.amount_recovered = recovered
            await s.flush()
            case_ids[ref] = case.id
            if disputed == ref:
                s.add(CaseDispute(
                    case_id=case.id, reason="Quantity does not match the PO",
                    status="open", opened_at=now - timedelta(days=1),
                ))
        account_id = account.id
        await s.commit()
    return account_id, case_ids


# ── Two scopes, one secret: the confusion that must be impossible ────────


def test_an_account_token_is_not_a_case_token() -> None:
    """The whole reason the account payload carries a scope marker."""
    an_id = uuid.uuid4()
    token = recovery_link.mint_account(an_id)
    assert token is not None
    assert recovery_link.verify_account(token) == an_id
    assert recovery_link.verify(token) is None, "account token read as a case"


def test_a_case_token_is_not_an_account_token() -> None:
    a_case = uuid.uuid4()
    token = recovery_link.mint(a_case)
    assert token is not None
    assert recovery_link.verify(token) == a_case
    assert recovery_link.verify_account(token) is None, "case token read as an account"


def test_a_forged_or_tampered_account_token_is_refused() -> None:
    token = recovery_link.mint_account(uuid.uuid4())
    assert token is not None
    payload, _, signature = token.partition(".")
    assert recovery_link.verify_account(f"{payload}.{'A' * len(signature)}") is None
    assert recovery_link.verify_account("not-a-token") is None
    assert recovery_link.verify_account("") is None


def test_an_expired_account_token_is_refused(monkeypatch: Any) -> None:
    token = recovery_link.mint_account(uuid.uuid4(), ttl_hours=1)
    assert token is not None
    monkeypatch.setattr("src.recovery_link.time.time", lambda: 9_999_999_999)
    assert recovery_link.verify_account(token) is None


def test_no_secret_means_no_account_token(monkeypatch: Any) -> None:
    """Fail closed, same rule as every other guard in this config."""
    monkeypatch.delenv("RECOVERY_LINK_SECRET", raising=False)
    get_settings.cache_clear()
    assert recovery_link.mint_account(uuid.uuid4()) is None
    assert recovery_link.verify_account("anything.atall") is None


def test_the_account_token_carries_no_pii() -> None:
    token = recovery_link.mint_account(uuid.uuid4())
    assert token is not None
    decoded = recovery_link.unb64(token.split(".")[0]).decode()
    assert "@" not in decoded and "buyer" not in decoded.lower()


# ── The page ─────────────────────────────────────────────────────────────


async def test_the_statement_totals_and_lists_every_open_invoice(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    account_id, _ = await _account_with_invoices(
        db_sessionmaker,
        refs=[("INV-201", 100_000, 0), ("INV-202", 75_000, 0), ("INV-203", 50_000, 0)],
    )
    token = recovery_link.mint_account(account_id)
    assert token is not None
    body = client.get(f"/statement/{token}").text
    assert "₹2,250" in body, "the total is the figure they opened this for"
    for ref in ("INV-201", "INV-202", "INV-203"):
        assert ref in body
    assert "Open across 3 invoices" in body


async def test_the_total_subtracts_what_was_already_paid(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A part-paid invoice must not be re-billed in full by the statement."""
    account_id, _ = await _account_with_invoices(
        db_sessionmaker, refs=[("INV-PART", 100_000, 40_000)]
    )
    token = recovery_link.mint_account(account_id)
    assert token is not None
    body = client.get(f"/statement/{token}").text
    assert "₹600" in body, "still owed, not gross billed"
    assert "₹400 already paid" in body, "and the payment is not hidden either"


async def test_every_row_links_to_that_invoices_own_recovery_page(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    The design rule: the statement grants no authority of its own. Each link
    is that case's own token, so the money path stays the tested one.
    """
    account_id, case_ids = await _account_with_invoices(
        db_sessionmaker, refs=[("INV-301", 60_000, 0)]
    )
    token = recovery_link.mint_account(account_id)
    assert token is not None
    body = client.get(f"/statement/{token}").text
    assert "/recover/" in body
    link_token = body.split('href="/recover/')[1].split('"')[0]
    assert recovery_link.verify(link_token) == case_ids["INV-301"]
    # The page itself offers no money action of its own.
    assert 'action="/recover/' not in body.replace(
        f'action="/recover/{link_token}/optout"', ""
    ) or "/pay" not in body


async def test_one_account_never_sees_anothers_invoices(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    mine, _ = await _account_with_invoices(
        db_sessionmaker, refs=[("INV-MINE", 10_000, 0)], account_ref="ref:mine"
    )
    await _account_with_invoices(
        db_sessionmaker, refs=[("INV-THEIRS", 90_000, 0)], account_ref="ref:theirs"
    )
    token = recovery_link.mint_account(mine)
    assert token is not None
    body = client.get(f"/statement/{token}").text
    assert "INV-MINE" in body
    assert "INV-THEIRS" not in body, "cross-account leak"
    assert "₹900" not in body


async def test_a_disputed_invoice_is_shown_and_labelled_not_hidden(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Dropping it would make the total above unexplainable."""
    account_id, _ = await _account_with_invoices(
        db_sessionmaker,
        refs=[("INV-OK", 20_000, 0), ("INV-DISPUTED", 30_000, 0)],
        disputed="INV-DISPUTED",
    )
    token = recovery_link.mint_account(account_id)
    assert token is not None
    body = client.get(f"/statement/{token}").text
    assert "INV-DISPUTED" in body
    assert "Under review" in body
    assert "₹500" in body, "the disputed invoice still counts toward the balance"


async def test_a_recovered_invoice_leaves_the_statement(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    account_id, case_ids = await _account_with_invoices(
        db_sessionmaker, refs=[("INV-401", 40_000, 0), ("INV-402", 10_000, 0)]
    )
    async with db_sessionmaker() as s:
        from src.models import RecoveryCase

        case = await s.get(RecoveryCase, case_ids["INV-401"])
        assert case is not None
        case.state = "recovered"
        case.amount_recovered = 40_000
        await s.commit()

    token = recovery_link.mint_account(account_id)
    assert token is not None
    body = client.get(f"/statement/{token}").text
    assert "INV-401" not in body, "a settled invoice still asking for money"
    assert "INV-402" in body
    assert "₹100" in body


async def test_an_unknown_account_looks_identical_to_a_forgery(
    client: TestClient
) -> None:
    token = recovery_link.mint_account(uuid.uuid4())
    assert token is not None
    real = client.get(f"/statement/{token}")
    forged = client.get("/statement/bogus.token")
    assert real.status_code == forged.status_code == 404
    # Identical but for the language toggle's self-link, which echoes back
    # the path the requester themselves sent — it discloses nothing they did
    # not already type. Everything the SERVER knows must read the same.
    assert real.text.replace(token, "X") == forged.text.replace("bogus.token", "X"), (
        "a probe can tell which guesses got warmer"
    )


async def test_the_statement_page_refuses_to_be_framed(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """Same four headers as /recover — same token-in-URL, same money page."""
    from src.main import app

    async def override() -> Any:
        async with db_sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = override
    try:
        r = TestClient(app).get("/statement/bogus.token")
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert r.headers["Cache-Control"] == "no-store, private"
        assert r.headers["Referrer-Policy"] == "no-referrer"
    finally:
        app.dependency_overrides.clear()


# ── The consolidated message's link finally resolves ─────────────────────


def test_the_statement_url_points_at_the_statement_page() -> None:
    account_id = uuid.uuid4()
    url = recovery_link.url_for_account(account_id)
    assert url is not None
    assert url.startswith("https://pay.example.in/statement/")
    assert recovery_link.verify_account(url.rsplit("/", 1)[1]) == account_id


# ── The instalment form: 2 rows shipped, 6 always allowed ────────────────
#
# No HTTP-level test existed for /dispute or /plan at all — only the
# functions under them. The form markup those handlers parse was therefore
# unpinned, which is exactly how it kept a 2-row cap while its own copy
# promised 2-6.


async def test_the_plan_form_offers_every_instalment_the_server_accepts(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from src.receivables.plans import MAX_INSTALMENTS

    _, case_ids = await _account_with_invoices(
        db_sessionmaker, refs=[("INV-FORM", 60_000, 0)]
    )
    token = recovery_link.mint(case_ids["INV-FORM"])
    assert token is not None
    body = client.get(f"/recover/{token}").text
    for i in range(1, MAX_INSTALMENTS + 1):
        assert f'name="instalment_{i}_amount"' in body, f"row {i} unreachable"
    assert f'name="instalment_{MAX_INSTALMENTS + 1}_amount"' not in body


async def test_only_the_first_two_instalment_rows_are_required(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """
    A hidden required input blocks submission with a validation error the
    reader can neither see nor reach, so rows 3-6 become required only when
    the script reveals them.
    """
    _, case_ids = await _account_with_invoices(
        db_sessionmaker, refs=[("INV-REQ", 60_000, 0)]
    )
    token = recovery_link.mint(case_ids["INV-REQ"])
    assert token is not None
    body = client.get(f"/recover/{token}").text
    row3 = body.split('name="instalment_3_date"')[1].split(">")[0]
    assert "required" not in row3


async def test_a_four_instalment_plan_submits_end_to_end(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """More than two rows was unreachable through the UI until now."""
    from src.receivables.models import PlanInstalment

    _, case_ids = await _account_with_invoices(
        db_sessionmaker, refs=[("INV-PLAN", 40_000, 0)]
    )
    token = recovery_link.mint(case_ids["INV-PLAN"])
    assert token is not None
    base = datetime.now(UTC) + timedelta(days=2)
    form = {}
    for i in range(4):
        form[f"instalment_{i + 1}_date"] = (base + timedelta(days=i * 7)).strftime("%Y-%m-%d")
        form[f"instalment_{i + 1}_amount"] = "100"
    r = client.post(f"/recover/{token}/plan", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?plan=ok"), r.headers

    async with db_sessionmaker() as s:
        from sqlalchemy import select

        rows = (await s.execute(select(PlanInstalment))).scalars().all()
    assert len(rows) == 4, "the third and fourth rows the UI could never send"


async def test_a_half_filled_instalment_row_is_refused_not_defaulted(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _, case_ids = await _account_with_invoices(
        db_sessionmaker, refs=[("INV-HALF", 40_000, 0)]
    )
    token = recovery_link.mint(case_ids["INV-HALF"])
    assert token is not None
    due = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%d")
    r = client.post(
        f"/recover/{token}/plan",
        data={
            "instalment_1_date": due, "instalment_1_amount": "200",
            "instalment_2_date": due, "instalment_2_amount": "200",
            "instalment_3_date": due,  # amount missing
        },
        follow_redirects=False,
    )
    assert r.headers["location"].endswith("?plan=invalid")


async def test_a_dispute_posts_and_pauses_that_invoice(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The other route that had no HTTP-level test."""
    from sqlalchemy import select

    _, case_ids = await _account_with_invoices(
        db_sessionmaker, refs=[("INV-DISP", 40_000, 0)]
    )
    token = recovery_link.mint(case_ids["INV-DISP"])
    assert token is not None
    r = client.post(
        f"/recover/{token}/dispute",
        data={"reason": "Quantity billed does not match the PO"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    async with db_sessionmaker() as s:
        row = (await s.execute(select(CaseDispute))).scalars().first()
    assert row is not None
    assert row.case_id == case_ids["INV-DISP"]
    assert row.status == "open"


# ── Aging on the single-invoice page ─────────────────────────────────────


async def test_an_overdue_invoice_shows_its_due_date_and_age(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _, case_ids = await _account_with_invoices(
        db_sessionmaker, refs=[("INV-AGE", 40_000, 0)]
    )
    token = recovery_link.mint(case_ids["INV-AGE"])
    assert token is not None
    body = client.get(f"/recover/{token}").text
    assert "Due" in body
    assert "days overdue" in body


async def test_a_not_yet_due_invoice_never_claims_it_is_overdue(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """No "0 days overdue", and certainly no negative one dressed as urgency."""
    from src.models import RecoveryCase

    _, case_ids = await _account_with_invoices(
        db_sessionmaker, refs=[("INV-FUTURE", 40_000, 0)]
    )
    async with db_sessionmaker() as s:
        case = await s.get(RecoveryCase, case_ids["INV-FUTURE"])
        assert case is not None
        case.due_at = datetime.now(UTC) + timedelta(days=5)
        await s.commit()

    token = recovery_link.mint(case_ids["INV-FUTURE"])
    assert token is not None
    body = client.get(f"/recover/{token}").text
    assert "days overdue" not in body
    assert "Due" in body, "the date itself is still worth stating"


async def test_a_card_decline_gets_no_invoice_due_line(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """due_at on a payment failure is the failure instant, already shown."""
    from src.cases import open_case as _open

    async with db_sessionmaker() as s:
        case = await _open(
            s, risk_type="payment_failure", subject_ref="pay_no_due",
            customer_id="cust@example.com", amount_at_risk=40_000, max_attempts=3,
        )
        case.due_at = datetime.now(UTC) - timedelta(days=30)
        await s.commit()
        case_id = case.id

    token = recovery_link.mint(case_id)
    assert token is not None
    body = client.get(f"/recover/{token}").text
    assert "days overdue" not in body
