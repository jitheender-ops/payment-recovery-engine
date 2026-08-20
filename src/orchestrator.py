"""
Payment Recovery Orchestrator — ties all five layers together.

Flow: classify → check retryability → build context → agent decision →
guardrail validation → execute/nudge → log everything.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.actions import FailureContext, RetryAction
from src.agent.policy_agent import PolicyAgent
from src.classifier.mapper import ClassifierMapper
from src.classifier.taxonomy import FailureClass
from src.config import get_settings
from src.guardrail.gate import GuardrailGate
from src.messaging.nudge_generator import NudgeGenerator
from src.executor.retry_executor import RetryExecutor
from src.models import PaymentFailure, RetryAttempt, RetryLedger, WebhookEvent

logger = logging.getLogger(__name__)


class PaymentRecoveryOrchestrator:
    """
    Central orchestrator — Layer 1 (ingestion) calls into this after
    storing the webhook event and classifying the failure.
    """

    def __init__(self) -> None:
        self._classifier = ClassifierMapper()
        self._guardrail = GuardrailGate()
        self._nudge_gen = NudgeGenerator()
        self._executor = RetryExecutor()
        self._agent: Optional[PolicyAgent] = None

    def _get_agent(self) -> PolicyAgent:
        """Lazy-init agent (needs API keys at runtime)."""
        if self._agent is None:
            try:
                self._agent = PolicyAgent()
            except Exception:
                logger.warning("Failed to init PolicyAgent — will use XGBoost fallback")
        return self._agent

    async def process_payment_failure(
        self, event: WebhookEvent, session: AsyncSession
    ) -> None:
        """
        Main pipeline entry point.

        1. Parse payment entity from webhook payload
        2. Classify failure (deterministic)
        3. Create PaymentFailure record
        4. If hard decline → abandon (no agent call)
        5. Build FailureContext
        6. Agent decision
        7. Guardrail validation
        8. Execute (retry/nudge) or log rejection
        9. Update ledger
        """
        payload = event.payload
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})

        if not payment_entity:
            logger.error("No payment entity in webhook payload: event=%s", event.razorpay_event_id)
            return

        payment_id = payment_entity.get("id", "unknown")
        logger.info("Processing payment failure: %s", payment_id)

        # ── Step 2: Classify ──────────────────────────────────────────────
        error_code = payment_entity.get("error_code", "UNKNOWN")
        error_desc = payment_entity.get("error_description")
        error_source = payment_entity.get("error_source")
        error_step = payment_entity.get("error_step")
        error_reason = payment_entity.get("error_reason")

        failure_class, is_retryable = self._classifier.classify(
            error_code, error_desc, error_source, error_step, error_reason
        )

        # ── Step 3: Create PaymentFailure record ──────────────────────────
        card_info = payment_entity.get("card", {}) or {}
        failure_record = PaymentFailure(
            payment_id=payment_id,
            order_id=payment_entity.get("order_id"),
            amount=payment_entity.get("amount", 0),
            currency=payment_entity.get("currency", "INR"),
            method=payment_entity.get("method", "unknown"),
            bank=payment_entity.get("bank"),
            wallet=payment_entity.get("wallet"),
            vpa=payment_entity.get("vpa"),
            card_network=card_info.get("network"),
            card_type=card_info.get("type"),
            card_issuer=card_info.get("issuer"),
            error_code=error_code,
            error_description=error_desc,
            error_source=error_source,
            error_step=error_step,
            error_reason=error_reason,
            failure_class=failure_class.value,
            is_retryable=is_retryable,
            customer_email=payment_entity.get("email"),
            customer_contact=payment_entity.get("contact"),
            webhook_event_id=event.id,
            failed_at=datetime.fromtimestamp(
                payment_entity.get("created_at", 0), tz=timezone.utc
            ),
        )
        session.add(failure_record)
        await session.flush()

        logger.info(
            "Classified: payment=%s class=%s retryable=%s",
            payment_id, failure_class.value, is_retryable,
        )

        # ── Step 4: Hard decline fast path ────────────────────────────────
        if failure_class.is_hard_decline:
            logger.info("Hard decline — abandoning without agent call: %s", payment_id)
            abandon_key = f"abandon_{payment_id}"
            if await self._attempt_exists(abandon_key, session):
                logger.info("Abandon already recorded for %s — skipping", payment_id)
                await session.commit()
                return
            attempt = RetryAttempt(
                payment_failure_id=failure_record.id,
                payment_id=payment_id,
                idempotency_key=abandon_key,
                attempt_number=0,
                action_type="abandon",
                agent_reasoning=f"Hard decline: {failure_class.value}",
                agent_type="deterministic",
                guardrail_passed=True,
                result="skipped",
            )
            session.add(attempt)
            await session.commit()
            return

        # ── Step 5: Build context ─────────────────────────────────────────
        context = await self._build_failure_context(failure_record, session)

        # ── Step 6: Agent decision ────────────────────────────────────────
        agent = self._get_agent()
        if agent:
            try:
                action = await agent.decide(context)
            except Exception:
                logger.exception("Agent failed, using XGBoost fallback")
                from src.agent.xgboost_baseline import XGBoostBaseline
                action = XGBoostBaseline().predict(context)
        else:
            from src.agent.xgboost_baseline import XGBoostBaseline
            action = XGBoostBaseline().predict(context)

        # ── Step 7: Idempotency key ───────────────────────────────────────
        # Deterministic by construction: (payment, attempt number) fully
        # determines the key. A random component would make every key unique
        # and therefore make the UNIQUE constraint on retry_attempts.
        # idempotency_key unfireable — an idempotency key you cannot collide
        # is not an idempotency key.
        attempt_count = await self._get_attempt_count(payment_id, session)
        idem_key = f"retry_{payment_id}_{attempt_count}"

        # Check-before-execute. This is where the double-charge is actually
        # prevented: razorpay-python has no idempotency header, so the
        # guarantee has to be enforced at our boundary, not theirs.
        if await self._attempt_exists(idem_key, session):
            logger.info("Attempt %s already executed — skipping (idempotent replay)", idem_key)
            await session.commit()
            return

        # ── Step 8: Guardrail validation ──────────────────────────────────
        guardrail_result = self._guardrail.validate(
            action, context, idem_key, attempt_count
        )

        # ── Step 9: Create RetryAttempt record ────────────────────────────
        attempt = RetryAttempt(
            payment_failure_id=failure_record.id,
            payment_id=payment_id,
            idempotency_key=idem_key,
            attempt_number=attempt_count + 1,
            action_type=action.action,
            target_rail=action.rail,
            scheduled_at=action.retry_at,
            agent_reasoning=action.reason,
            agent_type="llm" if agent else "xgboost",
            agent_confidence=action.confidence,
            guardrail_passed=guardrail_result.passed,
            guardrail_rejection_reason=(
                "; ".join(guardrail_result.rejection_reasons)
                if guardrail_result.rejection_reasons else None
            ),
        )

        nudge_message: Optional[str] = None

        if guardrail_result.passed:
            # ── Step 10: Generate nudge if needed ─────────────────────────
            if action.action in ("nudge_customer", "switch_rail"):
                try:
                    nudge_message = await self._nudge_gen.generate(
                        failure_class=failure_class.value,
                        amount=failure_record.amount,
                        method=failure_record.method,
                        next_step="Please try again using a different payment method.",
                        customer_name=None,
                    )
                    attempt.nudge_message = nudge_message
                except Exception:
                    logger.warning("Nudge generation failed — proceeding without nudge")

            # ── Step 11: Execute ──────────────────────────────────────────
            if action.action != "abandon":
                try:
                    exec_result = await self._executor.execute_retry(
                        payment_failure=failure_record,
                        action_type=action.action,
                        target_rail=action.rail,
                        idempotency_key=idem_key,
                        nudge_message=nudge_message,
                    )
                    attempt.executed_at = datetime.now(timezone.utc)
                    attempt.result = "success" if exec_result.get("success") else "failed"
                    attempt.result_details = exec_result
                    if nudge_message:
                        attempt.nudge_sent = exec_result.get("nudge_sent", False)
                except Exception:
                    logger.exception("Execution failed for %s", payment_id)
                    attempt.result = "failed"
                    attempt.result_details = {"error": "Execution exception"}
            else:
                attempt.result = "skipped"
        else:
            attempt.result = "rejected"
            logger.warning(
                "Guardrail rejected: payment=%s reasons=%s",
                payment_id, guardrail_result.rejection_reasons,
            )

        session.add(attempt)

        # ── Step 12: Update ledger ────────────────────────────────────────
        customer_id = context.customer_id
        if customer_id:
            await self._update_retry_ledger(customer_id, action, session)

        await session.commit()
        logger.info(
            "Pipeline complete: payment=%s action=%s guardrail=%s result=%s",
            payment_id, action.action, guardrail_result.passed, attempt.result,
        )

    async def _build_failure_context(
        self, failure: PaymentFailure, session: AsyncSession
    ) -> FailureContext:
        """Assemble FailureContext from payment record + DB lookups."""
        now = datetime.now(timezone.utc)
        customer_id = failure.customer_email or failure.customer_contact

        # Query customer retry history
        retry_count = 0
        nudge_count = 0
        previous_outcomes: list[str] = []

        if customer_id:
            ledger = await session.execute(
                select(RetryLedger).where(RetryLedger.customer_id == customer_id)
            )
            ledger_row = ledger.scalar_one_or_none()
            if ledger_row:
                retry_count = ledger_row.total_retries_24h
                nudge_count = ledger_row.total_nudges_24h

            # Get previous outcomes for this payment
            prev_attempts = await session.execute(
                select(RetryAttempt.result).where(
                    RetryAttempt.payment_id == failure.payment_id
                ).order_by(RetryAttempt.created_at.desc()).limit(5)
            )
            previous_outcomes = [r for (r,) in prev_attempts.all() if r]

        # IST hour (UTC + 5:30)
        ist_hour = (now.hour + 5) % 24  # Simplified IST offset

        return FailureContext(
            payment_id=failure.payment_id,
            order_id=failure.order_id,
            failure_class=failure.failure_class,
            error_code=failure.error_code,
            error_description=failure.error_description,
            error_source=failure.error_source,
            error_reason=failure.error_reason,
            amount=failure.amount,
            currency=failure.currency,
            method=failure.method,
            bank=failure.bank or failure.card_issuer,
            card_network=failure.card_network,
            card_type=failure.card_type,
            customer_id=customer_id,
            customer_email=failure.customer_email,
            customer_contact=failure.customer_contact,
            retry_count_24h=retry_count,
            nudge_count_24h=nudge_count,
            previous_retry_outcomes=previous_outcomes,
            failed_at=failure.failed_at,
            current_time=now,
            hour_of_day=ist_hour,
            day_of_week=now.weekday(),
            is_retryable=failure.is_retryable,
            original_failure_id=str(failure.id),
        )

    async def _attempt_exists(self, idempotency_key: str, session: AsyncSession) -> bool:
        """True if an attempt with this exact key was already recorded."""
        result = await session.execute(
            select(RetryAttempt.id).where(RetryAttempt.idempotency_key == idempotency_key)
        )
        return result.first() is not None

    async def _get_attempt_count(self, payment_id: str, session: AsyncSession) -> int:
        """Count existing retry attempts for a payment."""
        result = await session.execute(
            select(func.count()).where(RetryAttempt.payment_id == payment_id)
        )
        return result.scalar_one()

    async def _update_retry_ledger(
        self, customer_id: str, action: RetryAction, session: AsyncSession
    ) -> None:
        """Update per-customer rate-limiting ledger."""
        result = await session.execute(
            select(RetryLedger).where(RetryLedger.customer_id == customer_id)
        )
        ledger = result.scalar_one_or_none()

        now = datetime.now(timezone.utc)

        if ledger is None:
            ledger = RetryLedger(customer_id=customer_id)
            session.add(ledger)

        if action.action in ("retry_now", "retry_at", "switch_rail"):
            ledger.total_retries_24h += 1
            ledger.last_retry_at = now

        if action.action == "nudge_customer":
            ledger.total_nudges_24h += 1
            ledger.last_nudge_at = now


# ── Module-level helpers ─────────────────────────────────────────────────

_orchestrator: PaymentRecoveryOrchestrator | None = None


def get_orchestrator() -> PaymentRecoveryOrchestrator:
    """Cached singleton orchestrator."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = PaymentRecoveryOrchestrator()
    return _orchestrator


async def process_payment_failure(event: WebhookEvent, session: AsyncSession) -> None:
    """Convenience function called by the webhook router."""
    orchestrator = get_orchestrator()
    await orchestrator.process_payment_failure(event, session)
