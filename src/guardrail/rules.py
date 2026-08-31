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
        ceiling = self._settings.amount_ceiling_paise
        if amount_paise > ceiling:
            return False, (
                f"Amount ceiling exceeded: ₹{amount_paise / 100:,.2f} > "
                f"₹{ceiling / 100:,.2f}"
            )
        return True, None

    def check_consent_window(
        self,
        failed_at: datetime,
        current_time: datetime,
        window_hours: int | None = None,
    ) -> tuple[bool, str | None]:
        """
        No retry after the consent window has expired.

        `window_hours` overrides the global setting for chaser-driven risk
        types, whose windows are per-type (a cold cart is stale in two days,
        a receivable is chaseable for a month — see src/chasers/policy.py).
        None keeps the global consent_window_hours, which is every
        pre-existing caller's behaviour.
        """
        hours = window_hours or self._settings.consent_window_hours
        deadline = failed_at + timedelta(hours=hours)

        # Ensure timezone-aware comparison
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        if failed_at.tzinfo is None:
            failed_at = failed_at.replace(tzinfo=UTC)
            deadline = failed_at + timedelta(hours=hours)

        if current_time > deadline:
            hours_elapsed = (current_time - failed_at).total_seconds() / 3600
            return False, (
                f"Consent window expired: {hours_elapsed:.1f}h > {hours}h"
            )
        return True, None

    def check_retry_at_within_window(
        self,
        retry_at: datetime | None,
        failed_at: datetime,
        window_hours: int | None = None,
    ) -> tuple[bool, str | None]:
        """
        A deferred retry must land inside the window we are still allowed to act in.

        `retry_at` comes straight from the agent and had no upper bound at all.
        A far-future value parked the attempt as `scheduled` forever: the fire
        sweep only picks up rows whose `scheduled_at <= now`, and the stale
        sweep only looks at `pending` rows, so nothing ever touched it again —
        and because the orchestrator sets `case.next_action_at` to the same
        instant, the CASE sat `open` for good. Never chased, never expired,
        never counted; one bad decision and a case leaks out of the ledger.

        Rejecting rather than silently retiming is deliberate and matches every
        other rule here: the clamp we already apply (out of the blackout) only
        moves a time the agent could still have meant, while a retry past the
        consent deadline is a decision we have no authority to carry out. The
        fire-time re-validation would refuse it anyway — this refuses it now,
        while the rejection is still visible in the audit trail.
        """
        if retry_at is None:
            return True, None

        hours = window_hours or self._settings.consent_window_hours
        if failed_at.tzinfo is None:
            failed_at = failed_at.replace(tzinfo=UTC)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)

        deadline = failed_at + timedelta(hours=hours)
        if retry_at > deadline:
            return False, (
                f"Scheduled retry falls outside the consent window: "
                f"{retry_at.isoformat()} > {deadline.isoformat()} ({hours}h)"
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

    def check_expected_value(
        self, action_type: str, confidence: float | None, amount_paise: int
    ) -> tuple[bool, str | None]:
        """
        Stop while confidence * amount > cost_of_attempt + annoyance_cost.

        Only applies to retry_now and switch_rail — the two actions that
        actually attempt a charge; a nudge or a deferred retry does not spend
        the same kind of money. Only enforced when the agent supplied a
        confidence: RetryAction.confidence is optional, and an agent that
        gave no estimate is not asserting a false one — silently defaulting
        one in here would let this rule reject on a number the agent never
        claimed. When present, this is an ADDITIVE check alongside the fixed
        caps above, not a replacement for them: a well-funded attempt with
        high confidence still has to clear every other rule too.
        """
        if action_type not in ("retry_now", "switch_rail"):
            return True, None
        if confidence is None:
            return True, None

        cost = self._settings.retry_attempt_cost_paise + self._settings.retry_annoyance_cost_paise
        expected_value = confidence * amount_paise
        if expected_value <= cost:
            return False, (
                f"Expected value below attempt cost: confidence {confidence:.2f} × "
                f"₹{amount_paise / 100:,.2f} = ₹{expected_value / 100:,.2f} "
                f"≤ cost ₹{cost / 100:,.2f} — stop and escalate"
            )
        return True, None

    def check_mandate_predebit_notification(
        self,
        risk_type: str,
        action_type: str,
        last_notification_sent_at: datetime | None,
        current_time: datetime,
    ) -> tuple[bool, str | None]:
        """
        RBI Digital Payments E-mandate Framework, 2026: a pre-transaction
        notification must reach the customer at least 24 hours before a
        mandate is charged. Applies across cards, UPI and prepaid instruments.

        Only relevant here for risk_type=mandate_failure and action=retry_now
        — that is the one action that re-presents the mandate for collection.
        Every other action (nudge_customer, retry_at, switch_rail, abandon)
        does not move money against the mandate and is unaffected; a
        nudge_customer IS how the notification gets sent in the first place,
        so this rule cannot block the notification itself, only a collection
        attempt that skipped it.

        The framework's notice is per-debit, not per-mandate: one notification
        must not authorize unlimited re-presentations forever. A notification
        older than mandate_predebit_notification_valid_hours (7 days by
        default) is treated as no notification — it must be re-sent before
        the next retry_now is allowed.
        """
        if risk_type != "mandate_failure" or action_type != "retry_now":
            return True, None

        if last_notification_sent_at is None:
            return False, (
                "RBI e-mandate framework: no pre-debit notification on record "
                "for this mandate — send nudge_customer at least 24h before "
                "re-presenting the charge"
            )

        if last_notification_sent_at.tzinfo is None:
            last_notification_sent_at = last_notification_sent_at.replace(tzinfo=UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)

        elapsed_hours = (current_time - last_notification_sent_at).total_seconds() / 3600
        if elapsed_hours < 24:
            return False, (
                f"RBI e-mandate framework: pre-debit notification sent only "
                f"{elapsed_hours:.1f}h ago — 24h notice is required before "
                f"re-presenting the charge"
            )
        valid_hours = self._settings.mandate_predebit_notification_valid_hours
        if elapsed_hours > valid_hours:
            return False, (
                f"RBI e-mandate framework: pre-debit notification is stale — "
                f"sent {elapsed_hours:.0f}h ago, older than the "
                f"{valid_hours}h validity window. The notice is per-debit: "
                f"re-send nudge_customer at least 24h before re-presenting "
                f"the charge"
            )
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
