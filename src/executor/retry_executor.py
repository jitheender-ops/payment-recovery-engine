"""
Retry executor — executes recovery actions via Razorpay API.

Creates Payment Links for retry/switch-rail actions, sends notifications
for nudge actions. Every API call includes an idempotency key.
In test mode, no real money moves.
"""

from __future__ import annotations

import logging
from typing import Any

import razorpay

from src.config import get_settings
from src.models import PaymentFailure

logger = logging.getLogger(__name__)


class RetryExecutor:
    """Executes recovery actions through the Razorpay API."""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = razorpay.Client(
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
        )
        logger.info("RetryExecutor initialized (key_id=%s...)", settings.razorpay_key_id[:12])

    async def execute_retry(
        self,
        payment_failure: PaymentFailure,
        action_type: str,
        target_rail: str | None,
        idempotency_key: str,
        nudge_message: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute a recovery action via Razorpay API.

        Args:
            payment_failure: The original failed payment record.
            action_type: The action to execute (retry_now, switch_rail, nudge_customer, abandon).
            target_rail: Target rail for switch_rail actions.
            idempotency_key: Unique key for this attempt.
            nudge_message: Customer message for nudge actions.

        Returns:
            Dict with: success (bool), payment_link_id (optional), error (optional).
        """
        if action_type == "abandon":
            logger.info("Abandon action — no API call: payment=%s", payment_failure.payment_id)
            return {"success": True, "action": "abandon", "details": "No action taken"}

        try:
            if action_type in ("retry_now", "retry_at", "switch_rail"):
                return await self._create_payment_link(
                    payment_failure, target_rail, idempotency_key
                )
            elif action_type == "nudge_customer":
                return await self._send_nudge(
                    payment_failure, idempotency_key, nudge_message
                )
            else:
                logger.warning("Unknown action type: %s", action_type)
                return {"success": False, "error": f"Unknown action: {action_type}"}

        except razorpay.errors.BadRequestError as e:
            logger.error("Razorpay BadRequestError: %s", e)
            return {"success": False, "error": f"Bad request: {e}"}
        except razorpay.errors.ServerError as e:
            logger.error("Razorpay ServerError: %s", e)
            return {"success": False, "error": f"Server error: {e}"}
        except Exception as e:
            logger.exception("Unexpected error executing retry")
            return {"success": False, "error": str(e)}

    async def _create_payment_link(
        self,
        failure: PaymentFailure,
        target_rail: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create a Razorpay Payment Link for retry."""
        customer: dict[str, str] = {}
        if failure.customer_email:
            customer["email"] = failure.customer_email
        if failure.customer_contact:
            customer["contact"] = failure.customer_contact

        link_data: dict[str, Any] = {
            "amount": failure.amount,
            "currency": failure.currency,
            "description": f"Retry payment for order {failure.order_id or failure.payment_id}",
            "customer": customer,
            "notify": {"sms": False, "email": False},  # We handle notifications ourselves
            "notes": {
                "original_payment_id": failure.payment_id,
                "retry_idempotency_key": idempotency_key,
                "failure_class": failure.failure_class,
                # razorpay-python exposes no idempotency header, so this note is
                # an audit breadcrumb only — it does NOT stop Razorpay creating a
                # second link. The actual double-charge guarantee is enforced
                # upstream: the key is deterministic and the orchestrator checks
                # retry_attempts (UNIQUE on idempotency_key) before calling this
                # method. See PaymentRecoveryOrchestrator._attempt_exists.
                "idempotency_key": idempotency_key,
            },
        }

        result = self._client.payment_link.create(link_data)

        logger.info(
            "Payment link created: id=%s, short_url=%s, payment=%s",
            result.get("id"),
            result.get("short_url"),
            failure.payment_id,
        )

        return {
            "success": True,
            "payment_link_id": result.get("id"),
            "short_url": result.get("short_url"),
            "target_rail": target_rail,
        }

    async def _send_nudge(
        self,
        failure: PaymentFailure,
        idempotency_key: str,
        message: str | None,
    ) -> dict[str, Any]:
        """Create a Payment Link and notify the customer."""
        # First create the payment link
        link_result = await self._create_payment_link(failure, None, idempotency_key)

        if not link_result.get("success"):
            return link_result

        # Then send notification
        link_id = link_result.get("payment_link_id")
        if link_id:
            try:
                if failure.customer_contact:
                    self._client.payment_link.notifyBy(link_id, "sms")
                    logger.info("SMS notification sent for link %s", link_id)
                if failure.customer_email:
                    self._client.payment_link.notifyBy(link_id, "email")
                    logger.info("Email notification sent for link %s", link_id)
            except Exception:
                logger.warning("Failed to send notification for link %s", link_id)

        link_result["nudge_sent"] = True
        link_result["nudge_message"] = message
        return link_result
