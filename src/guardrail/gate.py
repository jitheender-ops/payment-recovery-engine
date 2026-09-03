"""
Guardrail gate — deterministic validation layer between agent and execution.

Runs AFTER the agent decision, BEFORE any money moves.
Validates schema + all business rules. Collects ALL violations (no short-circuit).
This is the answer to "what stops it from doing something stupid with real money."
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from src.agent.actions import FailureContext, RetryAction
from src.guardrail.rules import GuardrailRules
from src.guardrail.schemas import validate_action_schema

logger = logging.getLogger(__name__)


class GuardrailResult(BaseModel):
    """Result of guardrail validation."""

    passed: bool
    action: RetryAction | None = None
    rejection_reasons: list[str] = []
    rules_checked: int = 0
    rules_failed: int = 0


class GuardrailGate:
    """
    Deterministic guardrail gate.

    Validates every agent output against schema + business rules before
    any execution. Never short-circuits — collects ALL violations for
    complete audit logging.

    The checks are declared as (fn, args) pairs and run by _run_checks(),
    which makes the no-short-circuit discipline structural: every check in
    the list runs, every violation is collected, and rules_checked is
    derived (len) rather than hand-incremented seventeen times.
    """

    def __init__(self) -> None:
        self._rules = GuardrailRules()

    def _checks_for(
        self,
        action: RetryAction,
        context: FailureContext,
        idempotency_key: str,
        current_attempts: int,
    ) -> list[tuple[Callable[..., tuple[bool, str | None]], tuple[Any, ...]]]:
        """The business rules, in audit order (the schema check runs first in
        _run_checks and is not part of this list — different signature)."""
        r = self._rules
        checks: list[tuple[Callable[..., tuple[bool, str | None]], tuple[Any, ...]]] = [
            # 1. Hard-decline blocklist
            (r.check_hard_decline_blocklist, (context.failure_class,)),
            # 1b. Switch-only classes may not be retried on the same rail
            (r.check_switch_only_class, (context.failure_class, action.action)),
            # 2. Max retries per payment
            (r.check_max_retries_per_payment, (context.payment_id, current_attempts)),
            # 3. Max retries per customer (24h)
            (r.check_max_retries_per_customer, (context.retry_count_24h,)),
            # 4. Amount ceiling
            (r.check_amount_ceiling, (context.amount,)),
            # 5. Consent window
            (r.check_consent_window,
             (context.failed_at, context.current_time, context.consent_window_hours)),
            # 6. A deferred retry must land inside the consent window
            (r.check_retry_at_within_window,
             (action.retry_at, context.failed_at, context.consent_window_hours)),
            # 7. Time-of-day blackout
            (r.check_time_of_day_blackout, (context.hour_of_day,)),
            # 8. Idempotency key
            (r.check_idempotency_key, (idempotency_key,)),
            # 9. Expected value (only when the agent supplied a confidence)
            (r.check_expected_value,
             (action.action, action.confidence, context.amount)),
            # 10. Mandate pre-debit notification (RBI e-mandate framework, 2026)
            (r.check_mandate_predebit_notification,
             (context.risk_type, action.action,
              context.last_notification_sent_at, context.current_time,
              context.is_mandate_debit)),
        ]
        # Nudge rate limit applies only to nudge actions.
        if action.action == "nudge_customer":
            checks.append(
                (r.check_customer_nudge_rate_limit, (context.nudge_count_24h,))
            )
        return checks

    def _run_checks(
        self,
        checks: list[tuple[Callable[..., tuple[bool, str | None]], tuple[Any, ...]]],
        action: RetryAction,
    ) -> list[str]:
        """Schema first, then every business rule — no short-circuit, every
        violation collected."""
        violations: list[str] = []
        is_valid, _, schema_err = validate_action_schema(action.model_dump())
        if not is_valid:
            violations.append(f"Schema: {schema_err}")
        for fn, args in checks:
            passed, reason = fn(*args)
            if not passed:
                violations.append(reason or "unspecified guardrail violation")
        return violations

    def validate(
        self,
        action: RetryAction,
        context: FailureContext,
        idempotency_key: str,
        current_attempts: int,
    ) -> GuardrailResult:
        """
        Validate an agent's proposed action against all guardrail rules.

        Args:
            action: The proposed RetryAction from the agent.
            context: The full failure context.
            idempotency_key: Unique key for this retry attempt.
            current_attempts: Number of prior retry attempts for this payment.

        Returns:
            GuardrailResult with pass/fail and all rejection reasons.
        """
        # Abandon actions always pass — no point blocking an abandon
        if action.action == "abandon":
            logger.debug("Guardrail: abandon action — auto-pass")
            return GuardrailResult(
                passed=True,
                action=action,
                rules_checked=0,
                rules_failed=0,
            )

        checks = self._checks_for(action, context, idempotency_key, current_attempts)
        violations = self._run_checks(checks, action)

        passed = not violations
        result = GuardrailResult(
            passed=passed,
            action=action if passed else None,
            rejection_reasons=violations,
            rules_checked=len(checks) + 1,  # +1: the schema check
            rules_failed=len(violations),
        )
        if passed:
            logger.info(
                "Guardrail PASSED: payment=%s action=%s rules=%d",
                context.payment_id, action.action, len(checks),
            )
        else:
            logger.warning(
                "Guardrail REJECTED: payment=%s action=%s violations=%s",
                context.payment_id, action.action, violations,
            )
        return result

    def validate_self_serve(
        self,
        action: RetryAction,
        context: FailureContext,
        idempotency_key: str,
        current_attempts: int,
    ) -> GuardrailResult:
        """
        Validate a customer-initiated payment from the recovery page.

        A SUBSET of the full gate, and the difference is deliberate. The
        blackout and the per-customer contact limits bound OUR outreach; a
        customer choosing to pay at 3 AM is not outreach, and blocking it
        would burn money to keep a rule meant to stop nagging. The amount
        ceiling bounds retries WE initiate, not a payer's own choice. What
        still applies: the schema (the action must be well-formed), the
        hard-decline blocklist (a fraud-blocked payment will fail again —
        the page should say so, not mint a link for it), the CONSENT WINDOW
        (the token's TTL runs from ISSUANCE and can therefore outlive the
        window by up to a day — a link minted at hour 70 of a 72h window
        would otherwise stay payable past the engine's authority to act),
        the per-payment attempt budget (the case's bound is a bound whoever
        spends it), and the idempotency key (a double-tap must not
        double-mint).

        The previous code wrote guardrail_passed=True onto the attempt row
        without consulting a single rule — an audit trail claiming a
        validation that never happened. This runs the applicable rules and
        records what it ran, same no-short-circuit discipline as validate().
        """
        r = self._rules
        checks: list[tuple[Callable[..., tuple[bool, str | None]], tuple[Any, ...]]] = [
            (r.check_hard_decline_blocklist, (context.failure_class,)),
            (r.check_consent_window,
             (context.failed_at, context.current_time, context.consent_window_hours)),
            (r.check_max_retries_per_payment, (context.payment_id, current_attempts)),
            (r.check_idempotency_key, (idempotency_key,)),
        ]
        violations = self._run_checks(checks, action)

        passed = not violations
        result = GuardrailResult(
            passed=passed,
            action=action if passed else None,
            rejection_reasons=violations,
            rules_checked=len(checks) + 1,  # +1: the schema check
            rules_failed=len(violations),
        )
        if passed:
            logger.info(
                "Guardrail PASSED (self-serve): payment=%s rules=%d",
                context.payment_id, len(checks),
            )
        else:
            logger.warning(
                "Guardrail REJECTED (self-serve): payment=%s violations=%s",
                context.payment_id, violations,
            )
        return result
