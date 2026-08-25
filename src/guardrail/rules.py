"""
Business rules for the guardrail gate.

Each rule is a pure function that returns (passed, rejection_reason).
All thresholds are loaded from Settings (configurable via env vars).
This is the answer to "what stops it from doing something stupid with real money."
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.classifier.taxonomy import FailureClass
from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

# The single source of truth for "IST" in this codebase. Every wall-clock
# decision (blackout checks, hour_of_day context) resolves through this zone —
# India is UTC+5:30, so an offset of whole hours is wrong for half of every
# hour, and the half-hour lands exactly inside the window that matters.
IST = ZoneInfo("Asia/Kolkata")


class GuardrailRules:
    """Collection of deterministic business rules. No LLM here."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def check_hard_decline_blocklist(
        self, failure_class_str: str
    ) -> tuple[bool, str | None]:
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
    ) -> tuple[bool, str | None]:
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
    ) -> tuple[bool, str | None]:
        """No more than N retries per customer per 24h."""
        limit = self._settings.max_retries_per_customer_24h
        if customer_retries_24h >= limit:
            return False, (
                f"Max retries per customer (24h) exceeded: "
                f"{customer_retries_24h} >= {limit}"
            )
        return True, None

    def check_amount_ceiling(self, amount_paise: int) -> tuple[bool, str | None]:
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
    ) -> tuple[bool, str | None]:
        """No retry after the consent window has expired."""
        window_hours = self._settings.consent_window_hours
        deadline = failed_at + timedelta(hours=window_hours)

        # Ensure timezone-aware comparison
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        if failed_at.tzinfo is None:
            failed_at = failed_at.replace(tzinfo=UTC)
            deadline = failed_at + timedelta(hours=window_hours)

        if current_time > deadline:
            hours_elapsed = (current_time - failed_at).total_seconds() / 3600
            return False, (
                f"Consent window expired: {hours_elapsed:.1f}h > {window_hours}h"
            )
        return True, None

    def check_customer_nudge_rate_limit(
        self, nudges_24h: int
    ) -> tuple[bool, str | None]:
        """Max nudge messages per customer per 24h."""
        limit = self._settings.max_nudges_per_customer_24h
        if nudges_24h >= limit:
            return False, (
                f"Nudge rate limit exceeded: {nudges_24h} >= {limit} nudges in 24h"
            )
        return True, None

    def check_time_of_day_blackout(
        self, current_hour: int
    ) -> tuple[bool, str | None]:
        """No retries during the blackout window (bank success rates crater)."""
        start = self._settings.retry_blackout_start_hour
        end = self._settings.retry_blackout_end_hour

        if is_in_blackout(current_hour, self._settings):
            return False, (
                f"Time-of-day blackout: hour {current_hour} is within "
                f"{start:02d}:00-{end:02d}:00 IST"
            )
        return True, None

    def check_idempotency_key(
        self, idempotency_key: str | None
    ) -> tuple[bool, str | None]:
        """Every retry attempt MUST have an idempotency key."""
        if not idempotency_key or not idempotency_key.strip():
            return False, "Missing idempotency key — every retry must be idempotent"
        return True, None


def is_in_blackout(hour: int, settings: Settings | None = None) -> bool:
    """
    Whether an IST hour falls inside the retry blackout window.

    Shared by the decision-time rule and the retry_at clamp below, so the two
    can never disagree about where the window's edges are.
    """
    s = settings or get_settings()
    start = s.retry_blackout_start_hour
    end = s.retry_blackout_end_hour
    if start > end:  # overnight range (e.g. 23-7)
        return hour >= start or hour < end
    return start <= hour < end


def clamp_retry_at_out_of_blackout(retry_at: datetime) -> datetime:
    """
    Shift a deferred `retry_at` forward until it lands outside the blackout.

    Why this exists: the guardrail validates the CURRENT hour at decision time,
    so a +30min deferral approved at 22:30 sails through — and then the
    scheduler's fire-time re-validation rejects it for being 23:05. The attempt
    slot is already spent (attach_attempt ran), so every such decision burns
    budget on a retry that could never fire. Clamping at decision time makes the
    scheduled time one the fire-time check will actually approve.

    Forward-only, never earlier: waiting longer is always compliant; pulling a
    contact sooner than the agent chose is not ours to decide.
    """
    local = retry_at.astimezone(IST)
    if not is_in_blackout(local.hour):
        return retry_at

    end = get_settings().retry_blackout_end_hour
    # Jump straight to the window's edge rather than stepping hourly. +5min
    # clears the boundary itself: a retry landing at exactly 07:00 is inside
    # `hour < end`'s shadow only in rounding, and sitting on the edge risks the
    # clock reading 06:59:xx at fire time after tz/db round-trips.
    wake = local.replace(hour=end % 24, minute=5, second=0, microsecond=0)
    if wake <= local:
        wake += timedelta(days=1)

    clamped = wake.astimezone(UTC)
    logger.info(
        "retry_at shifted out of the IST blackout: %s → %s",
        retry_at.isoformat(),
        clamped.isoformat(),
    )
    return clamped
