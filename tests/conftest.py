"""Pytest fixtures for the payment recovery engine tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import src.models  # noqa: F401  — defining the classes is what registers the tables

# The AR tables (case_disputes, ar_accounts, payment_plans, ...) live in their
# own module, and create_all only builds what has been imported. Production
# never has this problem — `alembic upgrade head` creates every table
# unconditionally — so leaving this out makes the TEST schema a subset of the
# real one, and the failure is both remote from its cause and order-dependent:
# chase_case queries case_disputes on every chase, so a module that did not
# import this one passed only when some earlier test module happened to.
import src.receivables.models  # noqa: F401,E402  — same reason, other module
from src.agent.actions import FailureContext, RetryAction
from src.database import Base


# ── Database harness ─────────────────────────────────────────────────────
# The models declare Postgres JSONB, which the SQLite type compiler cannot
# render. This one shim makes the whole schema portable so DB-touching code can
# be tested without a Postgres server. UUID columns need no shim: SQLAlchemy
# 2.x's postgresql.UUID subclasses the generic Uuid type.
@compiles(JSONB, "sqlite")
def _compile_jsonb_on_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


class RealTimingRules(NamedTuple):
    """The unpatched clock rules, handed back by ``chaseable_clock``."""

    is_b2b_contact_time: Any
    is_in_blackout: Any


@pytest.fixture(autouse=True)
def chaseable_clock(monkeypatch: Any) -> RealTimingRules:
    """
    Hold chase_case's two wall-clock gates open for every test.

    chase_case consults two clocks before it does any work: the Mon–Fri
    09:30–18:30 IST B2B contact window (invoice_overdue only) and the
    23:00–07:00 IST quiet-hours blackout (every risk type). Both DEFER rather
    than reject, so forgetting one never raises — the case comes back
    untouched and some later assertion about the attempt budget or the ladder
    fails without naming a clock.

    This is session-wide and autouse because the exposure is: nine tests were
    pinned for the weekday axis and none for the hour, so the suite was green
    all day and sixteen tests across three modules went red at 23:00 IST — a
    failure CI would have hit on any overnight run, reported as sixteen
    unrelated assertion errors. Per-module fixtures had already been written
    twice; the third module proved it was the wrong place for them.

    The tempting alternative — pin `now` into the call — does not work, and
    that is worth recording: stop_reason() reads datetime.now(UTC) directly
    and ignores any injected `now`, so a future `now` makes every case read
    "next action not due yet" and nothing is chased at all. In production both
    clocks are the same instant, so this is a test-only seam; freezing the
    RULES keeps each test about the wiring it names.

    Both names are patched on src.orchestrator, never on the modules that
    define them, so the rules stay real for the tests that exercise them
    directly — patching ladder.is_b2b_contact_time also rewires
    next_b2b_window(), which calls it, and that test then asserts against the
    stub instead of the rule.

    Tests that are ABOUT one of these rules restore it from the returned
    tuple and pin their own `now`.
    """
    import src.orchestrator as orchestrator
    import src.scheduler as scheduler

    real = RealTimingRules(
        orchestrator.is_b2b_contact_time, orchestrator.is_in_blackout
    )
    # Two consumers of the B2B window, and a test that exercises the tick goes
    # through BOTH: chase_due_accounts consolidates the account, chase_case
    # delivers the rung. Patching one leaves the other on the wall clock.
    monkeypatch.setattr(orchestrator, "is_b2b_contact_time", lambda _dt: True)
    monkeypatch.setattr(scheduler, "is_b2b_contact_time", lambda _dt: True)
    monkeypatch.setattr(orchestrator, "is_in_blackout", lambda _hour: False)
    return real


@pytest.fixture
async def db_sessionmaker(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """
    A real async sessionmaker over a throwaway SQLite file.

    File-backed rather than :memory: deliberately. SQLAlchemy gives a memory
    SQLite engine a single shared connection, so two sessions would sit inside
    the same transaction and a test could not distinguish a flush from a
    commit — which is exactly the distinction the write-ahead tests rest on.

    expire_on_commit=False mirrors src/database.py.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def webhook_secret() -> str:
    return "test_secret_key_12345"


@pytest.fixture
def sample_webhook_payload() -> dict[str, Any]:
    """Realistic payment.failed webhook payload."""
    return {
        "entity": "event",
        "account_id": "acc_test123",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_abc123",
                    "entity": "payment",
                    "amount": 50000,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": "order_test_001",
                    "method": "card",
                    "bank": None,
                    "card": {
                        "network": "Visa",
                        "type": "credit",
                        "issuer": "HDFC",
                    },
                    "email": "test@example.com",
                    "contact": "+919876543210",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Payment declined by bank due to insufficient funds",
                    "error_source": "customer",
                    "error_step": "payment_authorization",
                    "error_reason": "insufficient_funds",
                    "created_at": int(time.time()),
                }
            }
        },
        "created_at": int(time.time()),
    }


@pytest.fixture
def sample_captured_payload() -> dict[str, Any]:
    return {
        "entity": "event",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_captured_123",
                    "amount": 50000,
                    "currency": "INR",
                    "status": "captured",
                    "method": "upi",
                    "created_at": int(time.time()),
                }
            }
        },
        "created_at": int(time.time()),
    }


@pytest.fixture
def signed_payload(
    webhook_secret: str, sample_webhook_payload: dict[str, Any]
) -> tuple[bytes, str]:
    """Returns (raw_body_bytes, valid_signature)."""
    raw = json.dumps(sample_webhook_payload).encode("utf-8")
    sig = hmac.new(webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, sig


@pytest.fixture
def sample_failure_context() -> FailureContext:
    now = datetime.now(UTC)
    return FailureContext(
        payment_id="pay_test_ctx_001",
        order_id="order_test_001",
        failure_class="insufficient_funds",
        error_code="BAD_REQUEST_ERROR",
        error_description="Insufficient funds",
        error_source="customer",
        error_reason="insufficient_funds",
        amount=50000,
        currency="INR",
        method="card",
        bank="HDFC",
        card_network="Visa",
        card_type="credit",
        customer_id="test@example.com",
        retry_count_24h=0,
        nudge_count_24h=0,
        previous_retry_outcomes=[],
        failed_at=now,
        current_time=now,
        hour_of_day=14,
        day_of_week=2,
        is_retryable=True,
    )


@pytest.fixture
def sample_retry_action() -> RetryAction:
    return RetryAction(
        action="retry_now",
        reason="Test retry action",
        confidence=0.8,
    )
