"""
Guardrail gate — deterministic validation layer between agent and execution.

Runs AFTER the agent decision, BEFORE any money moves.
Validates schema + all business rules. Collects ALL violations (no short-circuit).
This is the answer to "what stops it from doing something stupid with real money."
"""

from __future__ import annotations

import logging

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
    """

    def __init__(self) -> None:
        self._rules = GuardrailRules()

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

        violations: list[str] = []
        rules_checked = 0

        # 1. Schema validation
        rules_checked += 1
        is_valid, _, schema_err = validate_action_schema(action.model_dump())
        if not is_valid:
            violations.append(f"Schema: {schema_err}")

        # 2. Hard-decline blocklist
        rules_checked += 1
        passed, reason = self._rules.check_hard_decline_blocklist(context.failure_class)
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        # 3. Max retries per payment
        rules_checked += 1
        passed, reason = self._rules.check_max_retries_per_payment(
            context.payment_id, current_attempts
        )
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        # 4. Max retries per customer (24h)
        rules_checked += 1
        passed, reason = self._rules.check_max_retries_per_customer(
            context.retry_count_24h
        )
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        # 5. Amount ceiling
        rules_checked += 1
        passed, reason = self._rules.check_amount_ceiling(context.amount)
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        # 6. Consent window
        rules_checked += 1
        passed, reason = self._rules.check_consent_window(
            context.failed_at, context.current_time, context.consent_window_hours
        )
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        # 7. A deferred retry must land inside the consent window
        rules_checked += 1
        passed, reason = self._rules.check_retry_at_within_window(
            action.retry_at, context.failed_at, context.consent_window_hours
        )
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        # 8. Nudge rate limit (only for nudge actions)
        if action.action == "nudge_customer":
            rules_checked += 1
            passed, reason = self._rules.check_customer_nudge_rate_limit(
                context.nudge_count_24h
            )
            if not passed:
                violations.append(reason or "unspecified guardrail violation")

        # 9. Time-of-day blackout
        rules_checked += 1
        passed, reason = self._rules.check_time_of_day_blackout(context.hour_of_day)
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        # 10. Idempotency key
        rules_checked += 1
        passed, reason = self._rules.check_idempotency_key(idempotency_key)
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        # Final result
        all_passed = len(violations) == 0

        result = GuardrailResult(
            passed=all_passed,
            action=action if all_passed else None,
            rejection_reasons=violations,
            rules_checked=rules_checked,
            rules_failed=len(violations),
        )

        if all_passed:
            logger.info(
                "Guardrail PASSED: payment=%s action=%s rules=%d",
                context.payment_id,
                action.action,
                rules_checked,
            )
        else:
            logger.warning(
                "Guardrail REJECTED: payment=%s action=%s violations=%s",
                context.payment_id,
                action.action,
                violations,
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
        violations: list[str] = []
        rules_checked = 0

        rules_checked += 1
        is_valid, _, schema_err = validate_action_schema(action.model_dump())
        if not is_valid:
            violations.append(f"Schema: {schema_err}")

        rules_checked += 1
        passed, reason = self._rules.check_hard_decline_blocklist(context.failure_class)
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        rules_checked += 1
        passed, reason = self._rules.check_consent_window(
            context.failed_at, context.current_time, context.consent_window_hours
        )
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        rules_checked += 1
        passed, reason = self._rules.check_max_retries_per_payment(
            context.payment_id, current_attempts
        )
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        rules_checked += 1
        passed, reason = self._rules.check_idempotency_key(idempotency_key)
        if not passed:
            violations.append(reason or "unspecified guardrail violation")

        all_passed = len(violations) == 0
        result = GuardrailResult(
            passed=all_passed,
            action=action if all_passed else None,
            rejection_reasons=violations,
            rules_checked=rules_checked,
            rules_failed=len(violations),
        )
        if all_passed:
            logger.info(
                "Guardrail PASSED (self-serve): payment=%s rules=%d",
                context.payment_id,
                rules_checked,
            )
        else:
            logger.warning(
                "Guardrail REJECTED (self-serve): payment=%s violations=%s",
                context.payment_id,
                violations,
            )
        return result
