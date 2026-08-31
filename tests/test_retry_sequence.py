"""
The retry-sequence panel on the recovery page (subscription/mandate) and
the honest "we called you" voice trace (any risk type).

The one rule that matters for the sequence panel: the upcoming row states
WHEN, never WHAT. retry_now, retry_at, switch_rail and nudge_customer are
all decided live by the agent at the moment it next acts, not stored ahead
of time, so a label predicting the verb would eventually be wrong — which
is the one thing this page's honesty rules never allow. Most tests below
are really testing that one boundary from a different angle.

The voice-call tests exist because RetryAttempt.channel is never actually
set to "voice" by any code path — VoiceCallQueue.state == "done" is the
real signal, and a query against the wrong field would silently never
render, which is worse than not having the feature at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.cases import open_case
from src.config import get_settings
from src.customer.routes import router as customer_router
from src.database import get_session
from src.models import RetryAttempt, VoiceCallQueue
from src.recovery_link import mint

LINK_SECRET = "recovery-link-test-secret"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: Any) -> Any:
    get_settings.cache_clear()
    monkeypatch.setenv("RECOVERY_LINK_SECRET", LINK_SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pay.example.in")
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


async def _open(
    sm: async_sessionmaker[AsyncSession],
    *,
    risk_type: str,
    subject: str,
    max_attempts: int | None = None,
    attempts_used: int = 0,
    next_action_at: datetime | None = None,
) -> tuple[uuid.UUID, str]:
    async with sm() as s:
        case = await open_case(
            s, risk_type=risk_type, subject_ref=subject,  # type: ignore[arg-type]
            customer_id="cust@example.com", amount_at_risk=99900,
            max_attempts=max_attempts, next_action_at=next_action_at,
        )
        case.attempts_used = attempts_used
        s.add(case)
        await s.commit()
        case_id = case.id
    token = mint(case_id)
    assert token is not None
    return case_id, token


async def _add_attempt(
    sm: async_sessionmaker[AsyncSession],
    case_id: uuid.UUID,
    *,
    action_type: str,
    executed_at: datetime | None,
    attempt_number: int,
    result: str | None = "success",
) -> None:
    async with sm() as s:
        s.add(RetryAttempt(
            recovery_case_id=case_id,
            idempotency_key=f"seq_{case_id}_{attempt_number}",
            attempt_number=attempt_number,
            action_type=action_type,
            agent_type="xgboost",
            guardrail_passed=True,
            executed_at=executed_at,
            result=result,
        ))
        await s.commit()


async def test_past_attempts_render_in_order_with_honest_labels(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    case_id, token = await _open(
        db_sessionmaker, risk_type="mandate_failure", subject="mandate_seq_1",
    )
    await _add_attempt(
        db_sessionmaker, case_id, action_type="nudge_customer",
        executed_at=datetime.now(UTC) - timedelta(days=2), attempt_number=1,
    )
    await _add_attempt(
        db_sessionmaker, case_id, action_type="retry_now",
        executed_at=datetime.now(UTC) - timedelta(days=1), attempt_number=2,
    )

    page = client.get(f"/recover/{token}")
    text = page.text
    assert "We sent you a reminder." in text
    assert "We tried to collect payment." in text
    # Order: the reminder (older) must appear before the retry (newer).
    assert text.index("We sent you a reminder.") < text.index("We tried to collect payment.")


async def test_switch_rail_gets_its_own_honest_label(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    case_id, token = await _open(
        db_sessionmaker, risk_type="subscription_failure", subject="sub_seq_1",
    )
    await _add_attempt(
        db_sessionmaker, case_id, action_type="switch_rail",
        executed_at=datetime.now(UTC) - timedelta(hours=6), attempt_number=1,
    )
    page = client.get(f"/recover/{token}")
    assert "We suggested a different way to pay." in page.text


async def test_an_unexecuted_decision_never_renders(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A rejected/pending decision never reached the customer — no row for it."""
    case_id, token = await _open(
        db_sessionmaker, risk_type="mandate_failure", subject="mandate_seq_2",
    )
    await _add_attempt(
        db_sessionmaker, case_id, action_type="retry_now",
        executed_at=None, attempt_number=1, result=None,
    )
    page = client.get(f"/recover/{token}")
    assert "We tried to collect payment." not in page.text


async def test_the_upcoming_row_never_predicts_what_only_when(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The core honesty rule: state WHEN, never WHAT — for either risk type."""
    when = datetime.now(UTC) + timedelta(days=2)
    case_id, token = await _open(
        db_sessionmaker, risk_type="mandate_failure", subject="mandate_seq_3",
        max_attempts=3, attempts_used=1, next_action_at=when,
    )
    page = client.get(f"/recover/{token}")
    assert "in touch again around" in page.text
    assert "next attempt" not in page.text.lower()
    assert "next reminder" not in page.text.lower()


async def test_no_upcoming_row_once_attempts_are_exhausted(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    case_id, token = await _open(
        db_sessionmaker, risk_type="subscription_failure", subject="sub_seq_2",
        max_attempts=2, attempts_used=2,
        next_action_at=datetime.now(UTC) + timedelta(days=1),
    )
    page = client.get(f"/recover/{token}")
    assert "in touch again" not in page.text


async def test_no_upcoming_row_without_a_scheduled_next_action(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    case_id, token = await _open(
        db_sessionmaker, risk_type="mandate_failure", subject="mandate_seq_4",
        max_attempts=3, attempts_used=1, next_action_at=None,
    )
    page = client.get(f"/recover/{token}")
    assert "in touch again" not in page.text


async def test_cart_and_invoice_pages_get_no_sequence_rows(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The sequence panel is scoped to subscription/mandate only."""
    case_id, token = await _open(
        db_sessionmaker, risk_type="checkout_abandonment", subject="cart_seq_1",
        max_attempts=2, attempts_used=0,
        next_action_at=datetime.now(UTC) + timedelta(days=1),
    )
    page = client.get(f"/recover/{token}")
    assert "in touch again" not in page.text


# ── The "we called you" voice trace (any risk type) ────────────────────────


async def _add_voice_call(
    sm: async_sessionmaker[AsyncSession],
    case_id: uuid.UUID,
    *,
    state: str,
    claimed_at: datetime | None,
) -> None:
    async with sm() as s:
        s.add(VoiceCallQueue(
            recovery_case_id=case_id,
            retry_attempt_id=uuid.uuid4(),
            customer_contact="+919812345678",
            risk_type="mandate_failure",
            amount_paise=99900,
            state=state,
            claimed_at=claimed_at,
        ))
        await s.commit()


async def test_a_completed_call_renders_honestly(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    case_id, token = await _open(
        db_sessionmaker, risk_type="mandate_failure", subject="mandate_call_1",
    )
    await _add_voice_call(
        db_sessionmaker, case_id, state="done",
        claimed_at=datetime.now(UTC) - timedelta(hours=3),
    )
    page = client.get(f"/recover/{token}")
    assert "We called you on" in page.text


async def test_a_call_still_queued_or_claimed_says_nothing_yet(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing definite has happened yet — don't claim it did."""
    case_id, token = await _open(
        db_sessionmaker, risk_type="mandate_failure", subject="mandate_call_2",
    )
    await _add_voice_call(db_sessionmaker, case_id, state="queued", claimed_at=None)
    page = client.get(f"/recover/{token}")
    assert "We called you on" not in page.text


async def test_a_failed_call_is_not_reported_as_a_completed_one(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    case_id, token = await _open(
        db_sessionmaker, risk_type="mandate_failure", subject="mandate_call_3",
    )
    await _add_voice_call(
        db_sessionmaker, case_id, state="failed",
        claimed_at=datetime.now(UTC) - timedelta(hours=1),
    )
    page = client.get(f"/recover/{token}")
    assert "We called you on" not in page.text


async def test_a_completed_call_renders_on_the_payment_rail_page_too(
    client: TestClient, db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The trace isn't scoped to subscription/mandate — any risk type."""
    case_id, token = await _open(
        db_sessionmaker, risk_type="checkout_abandonment", subject="cart_call_1",
        max_attempts=2, attempts_used=0,
    )
    await _add_voice_call(
        db_sessionmaker, case_id, state="done",
        claimed_at=datetime.now(UTC) - timedelta(hours=5),
    )
    page = client.get(f"/recover/{token}")
    assert "We called you on" in page.text
