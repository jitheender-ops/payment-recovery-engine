"""
Promise capture from the recovery page and the merchant API, the reminder
sweep, and the kept-rate score — the digital half of the promise tracker.

The silence invariant is asserted at every write: a promise that does not
quiet the case is worse than no tracker, because now the audit shows we
knew.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.cases import (
    customer_promise_score,
    open_case,
    record_promise,
    resolve_promises,
    stop_reason,
)
from src.config import get_settings
from src.customer.routes import router as customer_router
from src.ingestion.risk_router import router as risk_router
from src.models import PromiseToPay, RecoveryCase
from src.recovery_link import mint

RISK_SECRET = "risk-test-secret"
LINK_SECRET = "recovery-link-test-secret"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: Any) -> Any:
    get_settings.cache_clear()
    monkeypatch.setenv("RECOVERY_LINK_SECRET", LINK_SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pay.example.in")
    yield
    get_settings.cache_clear()


@pytest.fixture
def app(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> FastAPI:
    """Both routers over the throwaway DB — mirrors test_customer_recovery."""
    app = FastAPI()
    app.include_router(customer_router, include_in_schema=False)
    app.include_router(risk_router, prefix="/risks", include_in_schema=False)

    from src.database import get_session

    async def _session() -> Any:
        async with db_sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(
        "src.ingestion.risk_router.async_session_factory", db_sessionmaker
    )
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


async def _open_invoice(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    subject: str = "inv_page_1",
    customer: str = "page@buyer.example",
) -> tuple[RecoveryCase, str]:
    async with db_sessionmaker() as s:
        case = await open_case(
            s,
            risk_type="invoice_overdue",
            subject_ref=subject,
            customer_id=customer,
            amount_at_risk=250_000,
        )
        await s.commit()
        token = mint(case.id)
    assert token is not None
    return case, token


def _risk_sig(body: bytes) -> dict[str, str]:
    return {
        "x-risk-signature": hmac.new(
            RISK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
    }


# ── The recovery-page promise ────────────────────────────────────────────


async def test_a_page_promise_silences_the_case(
    client: TestClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    case, token = await _open_invoice(db_sessionmaker)
    due = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d")

    r = client.post(
        f"/recover/{token}/promise", data={"due_date": due},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "promise=ok" in r.headers["location"]

    async with db_sessionmaker() as s:
        fresh = await s.get(RecoveryCase, case.id)
        assert fresh is not None
        promise = (
            await s.execute(
                select(PromiseToPay).where(
                    PromiseToPay.recovery_case_id == case.id
                )
            )
        ).scalar_one()
        assert promise.status == "pending"
        assert promise.channel == "payment_link"
        assert promise.confidence == "explicit"
        stop = stop_reason(fresh)
        assert stop is not None and "not due until" in stop


async def test_a_page_promise_refuses_a_past_date(
    client: TestClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    case, token = await _open_invoice(db_sessionmaker)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    r = client.post(
        f"/recover/{token}/promise", data={"due_date": yesterday},
        follow_redirects=False,
    )
    assert "promise=invalid" in r.headers["location"]
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                select(PromiseToPay).where(
                    PromiseToPay.recovery_case_id == case.id
                )
            )
        ).scalars().all()
        assert rows == []


async def test_a_page_promise_refuses_a_horizon_busting_date(
    client: TestClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, token = await _open_invoice(db_sessionmaker)
    far = (datetime.now(UTC) + timedelta(days=90)).strftime("%Y-%m-%d")
    r = client.post(
        f"/recover/{token}/promise", data={"due_date": far}, follow_redirects=False,
    )
    assert "promise=invalid" in r.headers["location"]


# ── The persistent tracker (GET-time visibility, not just a flash) ────────


async def test_the_page_remembers_a_pending_promise(
    client: TestClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Reload the page days later — the promise is still visible, not forgotten.

    Seeded directly (not via POST /promise, which is already covered by
    test_a_page_promise_silences_the_case) so this test doesn't burn shared
    rate-limit budget it doesn't need — that budget is process-global, not
    per-test, and other tests in the suite depend on it staying available.
    """
    case, token = await _open_invoice(db_sessionmaker)
    async with db_sessionmaker() as s:
        fresh = await s.get(RecoveryCase, case.id)
        assert fresh is not None
        await record_promise(
            s, fresh, amount=250_000, due_at=datetime.now(UTC) + timedelta(days=5),
            channel="payment_link", confidence="explicit",
        )
        await s.commit()

    page = client.get(f"/recover/{token}")
    assert "You said you" in page.text
    assert ("days left" in page.text) or ("Due today" in page.text)


async def test_a_promise_due_today_says_due_today_not_a_negative_number(
    client: TestClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The edge case before the sweep runs: due later today, still pending."""
    case, token = await _open_invoice(db_sessionmaker)
    async with db_sessionmaker() as s:
        fresh = await s.get(RecoveryCase, case.id)
        assert fresh is not None
        await record_promise(
            s, fresh, amount=250_000,
            due_at=datetime.now(UTC) + timedelta(hours=1),
            channel="payment_link", confidence="explicit",
        )
        await s.commit()

    page = client.get(f"/recover/{token}")
    assert "Due today" in page.text
    assert "-1 days left" not in page.text


async def test_a_kept_promise_leaves_no_stale_pending_note(
    client: TestClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    case, token = await _open_invoice(db_sessionmaker)
    async with db_sessionmaker() as s:
        fresh = await s.get(RecoveryCase, case.id)
        assert fresh is not None
        await record_promise(
            s, fresh, amount=250_000, due_at=datetime.now(UTC) + timedelta(days=3),
            channel="payment_link", confidence="explicit",
        )
        await resolve_promises(s, fresh, "kept", ref="pay_kept_1")
        await s.commit()

    page = client.get(f"/recover/{token}")
    assert "You said you" not in page.text
    assert "Your last promised date" not in page.text


async def test_a_broken_promise_shows_the_transparency_note(
    client: TestClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """No hiding: the last commitment broke, said plainly before the form again."""
    case, token = await _open_invoice(db_sessionmaker)
    async with db_sessionmaker() as s:
        fresh = await s.get(RecoveryCase, case.id)
        assert fresh is not None
        await record_promise(
            s, fresh, amount=250_000, due_at=datetime.now(UTC) - timedelta(days=1),
            channel="payment_link", confidence="explicit",
        )
        await resolve_promises(s, fresh, "broken")
        await s.commit()

    page = client.get(f"/recover/{token}")
    assert "Your last promised date" in page.text
    assert "You said you" not in page.text


# ── The merchant API promise ──────────────────────────────────────────────


async def test_a_signed_merchant_promise_records_and_reports_the_score(
    client: TestClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, _ = await _open_invoice(db_sessionmaker, subject="inv_api_1")
    monkeypatch.setenv("RISK_WEBHOOK_SECRET", RISK_SECRET)
    get_settings.cache_clear()
    try:
        body = json.dumps({
            "amount_paise": 250_000,
            "due_at": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
            "confidence": "tentative",
            "condition_note": "after salary credit",
        }).encode()
        r = client.post(
            "/risks/invoice_overdue/inv_api_1/promise",
            content=body, headers=_risk_sig(body),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "recorded"
        assert data["silenced_until"]

        async with db_sessionmaker() as s:
            promise = (
                await s.execute(
                    select(PromiseToPay).where(
                        PromiseToPay.recovery_case_id == case.id
                    )
                )
            ).scalar_one()
            assert promise.confidence == "tentative"
            assert promise.condition_note == "after salary credit"
            assert promise.channel == "merchant"
            assert promise.source_ref == "merchant_api"
    finally:
        get_settings.cache_clear()


async def test_an_unsigned_merchant_promise_is_rejected(
    client: TestClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _open_invoice(db_sessionmaker, subject="inv_api_2")
    body = json.dumps({
        "amount_paise": 250_000,
        "due_at": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
    }).encode()
    r = client.post(
        "/risks/invoice_overdue/inv_api_2/promise",
        content=body,  # no signature header
    )
    assert r.status_code == 401


async def test_a_promise_on_a_closed_case_is_a_404_not_a_reopen(
    client: TestClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, _ = await _open_invoice(db_sessionmaker, subject="inv_api_3")
    async with db_sessionmaker() as s:
        case_row = await s.get(RecoveryCase, case.id)
        case_row.state = "recovered"
        await s.commit()

    monkeypatch.setenv("RISK_WEBHOOK_SECRET", RISK_SECRET)
    get_settings.cache_clear()
    try:
        body = json.dumps({
            "amount_paise": 250_000,
            "due_at": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
        }).encode()
        r = client.post(
            "/risks/invoice_overdue/inv_api_3/promise",
            content=body, headers=_risk_sig(body),
        )
        assert r.status_code == 404
    finally:
        get_settings.cache_clear()


async def test_the_per_case_cap_refuses_a_fourth_promise(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        case = await open_case(
            s,
            risk_type="invoice_overdue",
            subject_ref="inv_cap_1",
            customer_id="cap@buyer.example",
            amount_at_risk=250_000,
        )
        await s.commit()
        # Three promises land (the cap), the fourth is refused.
        for i in range(4):
            p = await record_promise(
                s, case,
                amount=250_000,
                due_at=datetime.now(UTC) + timedelta(days=i + 1),
                channel="merchant",
            )
            if i < 3:
                assert p is not None
            else:
                assert p is None  # refused, audited promise_refused
        await s.commit()
        rows = (
            await s.execute(
                select(PromiseToPay).where(
                    PromiseToPay.recovery_case_id == case.id
                )
            )
        ).scalars().all()
        assert len(rows) == 3


async def test_plan_promise_channel_is_cap_exempt(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A payment plan's instalments are a validated set, not serial deferrals."""
    async with db_sessionmaker() as s:
        case = await open_case(
            s,
            risk_type="invoice_overdue",
            subject_ref="inv_plan_exempt",
            customer_id="plan@buyer.example",
            amount_at_risk=900_000,
        )
        await s.commit()
        for i in range(5):  # more than the cap of 3, all plan instalments
            p = await record_promise(
                s, case,
                amount=180_000,
                due_at=datetime.now(UTC) + timedelta(days=7 * (i + 1)),
                channel="payment_plan",
                source_ref=f"plan:x:{i + 1}",
            )
            assert p is not None
        await s.commit()


# ── The score ─────────────────────────────────────────────────────────────


async def test_the_kept_rate_score_reads_the_ledger(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        case = await open_case(
            s,
            risk_type="invoice_overdue",
            subject_ref="inv_score_1",
            customer_id="score@buyer.example",
            amount_at_risk=250_000,
        )
        # two kept (one late), one broken, one pending
        p1 = await record_promise(
            s, case, amount=100, due_at=datetime.now(UTC) + timedelta(days=1),
            channel="merchant",
        )
        assert p1 is not None
        p2 = await record_promise(
            s, case, amount=100,
            due_at=datetime.now(UTC) - timedelta(days=6),  # 5 days inside grace?
            channel="merchant",
        )
        assert p2 is not None
        # break it on the clock
        p2.status = "broken"
        p2.resolved_at = datetime.now(UTC)
        p3 = await record_promise(
            s, case, amount=100,
            due_at=datetime.now(UTC) - timedelta(days=3),
            channel="merchant",
        )
        assert p3 is not None
        p3.status = "kept"
        p3.resolved_at = datetime.now(UTC) - timedelta(days=1)
        p3.kept_late_days = 2
        await s.commit()

        score = await customer_promise_score(s, "score@buyer.example")
        assert score.kept == 1
        assert score.broken == 1
        assert score.pending == 1
        assert score.kept_rate == 0.5
        assert score.avg_kept_late_days == 2.0
        assert not score.serial_breaker


async def test_the_score_is_none_honest_with_no_history(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        score = await customer_promise_score(s, "nobody@buyer.example")
        assert score.kept_rate is None
        assert score.total == 0


# ── Grace ──────────────────────────────────────────────────────────────────


async def test_a_promise_inside_grace_does_not_break(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Due 2h ago, grace 24h — still pending, case still quiet."""
    from src.cases import expire_promises

    async with db_sessionmaker() as s:
        case = await open_case(
            s,
            risk_type="invoice_overdue",
            subject_ref="inv_grace_1",
            customer_id="grace@buyer.example",
            amount_at_risk=250_000,
        )
        await record_promise(
            s, case, amount=250_000,
            due_at=datetime.now(UTC) - timedelta(hours=2),
            channel="merchant",
        )
        await s.commit()
        assert await expire_promises(s) == 0
        await s.commit()
        promise = (
            await s.execute(
                select(PromiseToPay).where(
                    PromiseToPay.recovery_case_id == case.id
                )
            )
        ).scalar_one()
        assert promise.status == "pending"


# ── The reminder sweep ─────────────────────────────────────────────────────


async def test_the_reminder_sweep_fires_once_and_stamps(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.scheduler import remind_promises

    async with db_sessionmaker() as s:
        case = await open_case(
            s,
            risk_type="invoice_overdue",
            subject_ref="inv_remind_1",
            customer_id="remind@buyer.example",
            amount_at_risk=250_000,
        )
        # due in 24h — inside the 48h window
        await record_promise(
            s, case, amount=250_000,
            due_at=datetime.now(UTC) + timedelta(hours=24),
            channel="merchant",
        )
        await s.commit()

    # The reminder runs the real chase pipeline: Razorpay is not configured
    # in tests, so stub the executor's link mint to record the call shape.
    from src.orchestrator import get_orchestrator

    orch = get_orchestrator()

    async def _fake_execute(**kwargs: Any) -> dict[str, Any]:
        return {"success": True, "payment_link_id": "plink_fake",
                "short_url": "https://rzp.io/i/fake"}

    monkeypatch.setattr(orch._executor, "execute_case_action", _fake_execute)

    async with db_sessionmaker() as s:
        handled = await remind_promises(s)
        await s.commit()
    assert handled == 1

    async with db_sessionmaker() as s:
        promise = (
            await s.execute(
                select(PromiseToPay).where(
                    PromiseToPay.recovery_case_id == case.id
                )
            )
        ).scalar_one()
        assert promise.reminded_at is not None

    # Second tick: the marker holds, nothing re-fires.
    async with db_sessionmaker() as s:
        assert await remind_promises(s) == 0


async def test_no_reminder_for_a_far_future_promise(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from src.scheduler import remind_promises

    async with db_sessionmaker() as s:
        case = await open_case(
            s,
            risk_type="invoice_overdue",
            subject_ref="inv_remind_2",
            customer_id="far@buyer.example",
            amount_at_risk=250_000,
        )
        await record_promise(
            s, case, amount=250_000,
            due_at=datetime.now(UTC) + timedelta(days=10),
            channel="merchant",
        )
        await s.commit()
        assert await remind_promises(s) == 0


# ── The horizon has a floor under the three surface copies ─────────────────


async def test_record_promise_refuses_a_date_past_the_horizon(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    promise_max_horizon_days was enforced in three separate copies — the
    recovery page, the voice pipeline and the signed merchant API — while
    record_promise itself accepted anything. Any new caller silently got no
    bound at all. The surfaces keep their checks to answer in their own
    channel; this is the floor underneath them.
    """
    from src.cases import open_case, record_promise
    from src.config import get_settings

    horizon = get_settings().promise_max_horizon_days
    async with db_sessionmaker() as session:
        case = await open_case(
            session,
            risk_type="invoice_overdue",
            subject_ref="INV-HORIZON",
            amount_at_risk=100_000,
            currency="INR",
            customer_id="email:horizon@example.in",
        )
        await session.commit()

        too_far = datetime.now(UTC) + timedelta(days=horizon + 1)
        assert await record_promise(
            session, case, amount=100_000, due_at=too_far, channel="test"
        ) is None

        inside = datetime.now(UTC) + timedelta(days=horizon - 1)
        assert await record_promise(
            session, case, amount=100_000, due_at=inside, channel="test"
        ) is not None


async def test_a_payment_plan_keeps_its_own_longer_horizon(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A plan is a validated SET of instalments with a 90-day horizon of its
    own — the ceiling above is not the plan validator's business."""
    from src.cases import open_case, record_promise
    from src.config import get_settings

    horizon = get_settings().promise_max_horizon_days
    async with db_sessionmaker() as session:
        case = await open_case(
            session,
            risk_type="invoice_overdue",
            subject_ref="INV-PLAN-HORIZON",
            amount_at_risk=100_000,
            currency="INR",
            customer_id="email:planhorizon@example.in",
        )
        await session.commit()
        far = datetime.now(UTC) + timedelta(days=horizon + 30)
        assert await record_promise(
            session, case, amount=50_000, due_at=far, channel="payment_plan"
        ) is not None
