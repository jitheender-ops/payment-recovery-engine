"""Pytest fixtures for the payment recovery engine tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from src.agent.actions import FailureContext, RetryAction


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
