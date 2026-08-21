"""Pytest fixtures for the payment recovery engine tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import src.models  # noqa: F401  — defining the classes is what registers the tables
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
