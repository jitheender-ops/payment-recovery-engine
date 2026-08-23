"""
Business rules for the guardrail gate.

Each rule is a pure function that returns (passed, rejection_reason).
All thresholds are loaded from Settings (configurable via env vars).
This is the answer to "what stops it from doing something stupid with real money."
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.classifier.taxonomy import FailureClass
from src.config import get_settings

logger = logging.getLogger(__name__)


class GuardrailRules:
    """Collection of deterministic business rules. No LLM here."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def check_hard_decline_blocklist(
        self, failure_class_str: str
    ) -> tuple[bool, Optional[str]]:
        """Hard declines and fraud blocks must NEVER be retried."""
        try:
            fc = FailureClass(failure_class_str)
        except ValueError:
            return True, None  # Unknown class — let other rules decide

        if fc.is_hard_decline:
            return False, f"Hard decline blocklist: {fc.value} is non-retryable"
        return True, None

    def check_max_retries_per_payment(
        self, payment_id: str, current_attempts: int
    ) -> tuple[bool, Optional[str]]:
        """No more than N retries per payment."""
        limit = self._settings.max_retries_per_payment
        if current_attempts >= limit:
            return False, (
                f"Max retries per payment exceeded: "
                f"{current_attempts} >= {limit} for {payment_id}"
            )
        return True, None

    def check_max_retries_per_customer(
        self, customer_retries_24h: int
    ) -> tuple[bool, Optional[str]]:
        """No more than N retries per customer per 24h."""
        limit = self._settings.max_retries_per_customer_24h
        if customer_retries_24h >= limit:
            return False, (
                f"Max retries per customer (24h) exceeded: "
                f"{customer_retries_24h} >= {limit}"
            )
        return True, None

    def check_amount_ceiling(self, amount_paise: int) -> tuple[bool, Optional[str]]:
        """No retry for amounts above the ceiling without explicit consent."""
        ceiling = self._settings.amount_ceiling_inr
        if amount_paise > ceiling:
            return False, (
                f"Amount ceiling exceeded: ₹{amount_paise / 100:,.2f} > "
                f"₹{ceiling / 100:,.2f}"
            )
        return True, None

    def check_consent_window(
        self, failed_at: datetime, current_time: datetime
    ) -> tuple[bool, Optional[str]]:
        """No retry after the consent window has expired."""
        window_hours = self._settings.consent_window_hours
        deadline = failed_at + timedelta(hours=window_hours)

        # Ensure timezone-aware comparison
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        if failed_at.tzinfo is None:
            failed_at = failed_at.replace(tzinfo=timezone.utc)
            deadline = failed_at + timedelta(hours=window_hours)

        if current_time > deadline:
            hours_elapsed = (current_time - failed_at).total_seconds() / 3600
            return False, (
                f"Consent window expired: {hours_elapsed:.1f}h > {window_hours}h"
            )
        return True, None

    def check_customer_nudge_rate_limit(
        self, nudges_24h: int
    ) -> tuple[bool, Optional[str]]:
        """Max nudge messages per customer per 24h."""
        limit = self._settings.max_nudges_per_customer_24h
        if nudges_24h >= limit:
            return False, (
                f"Nudge rate limit exceeded: {nudges_24h} >= {limit} nudges in 24h"
            )
        return True, None

    def check_time_of_day_blackout(
        self, current_hour: int
    ) -> tuple[bool, Optional[str]]:
        """No retries during the blackout window (bank success rates crater)."""
        start = self._settings.retry_blackout_start_hour
        end = self._settings.retry_blackout_end_hour

        # Handle overnight range (e.g., 23-7)
        if start > end:
            in_blackout = current_hour >= start or current_hour < end
        else:
            in_blackout = start <= current_hour < end

        if in_blackout:
            return False, (
                f"Time-of-day blackout: hour {current_hour} is within "
                f"{start:02d}:00-{end:02d}:00 IST"
            )
        return True, None

    def check_idempotency_key(
        self, idempotency_key: Optional[str]
    ) -> tuple[bool, Optional[str]]:
        """Every retry attempt MUST have an idempotency key."""
        if not idempotency_key or not idempotency_key.strip():
            return False, "Missing idempotency key — every retry must be idempotent"
        return True, None
