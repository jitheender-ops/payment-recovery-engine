"""
Retry executor — executes recovery actions via Razorpay API.

Creates Payment Links for retry/switch-rail actions, sends notifications
for nudge actions. Every API call includes an idempotency key.
In test mode, no real money moves.
"""

from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import razorpay
import requests

from src.config import get_settings, reveal
from src.models import PaymentFailure, RecoveryCase

logger = logging.getLogger(__name__)

# How many Razorpay calls may run concurrently on the dedicated pool. The
# default asyncio executor is shared with everything else in the process
# (min(32, cpu+4) workers); the money path deserves its own budget so a burst
# of link creations cannot starve — or be starved by — unrelated thread work.
_MAX_RAZORPAY_WORKERS = 8

# One pool for the whole process, not one per RetryExecutor. The self-serve
# pay route constructs an executor per request; a fresh ThreadPoolExecutor
# each time spawned a fresh set of worker threads that outlived the request,
# so a busy day leaked threads linearly with traffic. The pool is created
# lazily on first use (ThreadPoolExecutor starts no threads at construction,
# but deferring keeps import-time side effects at zero).
_SHARED_POOL: ThreadPoolExecutor | None = None


def _shared_pool() -> ThreadPoolExecutor:
    global _SHARED_POOL
    if _SHARED_POOL is None:
        _SHARED_POOL = ThreadPoolExecutor(
            max_workers=_MAX_RAZORPAY_WORKERS,
            thread_name_prefix="razorpay",
        )
    return _SHARED_POOL


# Shape checks, not RFC validation: just "Razorpay will not 400 on this".
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CONTACT_SHAPE = re.compile(r"^\+?\d{8,15}$")


def sanitize_customer_email(email: str | None) -> str | None:
    """
    Merchant-typed email from the risk rail, unvalidated at the boundary.

    Razorpay rejects a misshapen one with a 400, which would fail the whole
    link call — and a chase that fails at link creation burns the case's
    attempt budget on a typo. Drop what cannot be real; the link mints fine
    without a prefilled customer. The payment rail needs no such guard: its
    emails come from Razorpay's own webhook, already shaped by Razorpay.
    """
    if email is None:
        return None
    candidate = email.strip()
    if _EMAIL_SHAPE.match(candidate):
        return candidate
    logger.warning(
        "Dropping misshapen customer email %r — the link mints without it", email
    )
    return None


def sanitize_customer_contact(contact: str | None) -> str | None:
    """Same guard for the phone number: strip formatting, keep digit shapes."""
    if contact is None:
        return None
    candidate = re.sub(r"[\s\-()]", "", contact.strip())
    if _CONTACT_SHAPE.match(candidate):
        return candidate
    logger.warning(
        "Dropping misshapen customer contact %r — the link mints without it",
        contact,
    )
    return None


class _TimeoutSession(requests.Session):
    """
    A requests Session that applies a default timeout to every request.

    requests has no default timeout and razorpay-python never sets one, so a
    hung connection blocks its caller forever. The default belongs here rather
    than at each call site: Client.request() dispatches through
    getattr(self.session, verb) and every requests verb funnels into
    Session.request, so overriding this one method covers every SDK resource —
    including ones a future SDK version adds. A per-call timeout would have to
    be re-remembered by whoever writes the next API call.
    """

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    def request(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self._timeout)
        return super().request(*args, **kwargs)


class RetryExecutor:
    """Executes recovery actions through the Razorpay API."""

    def __init__(self) -> None:
        settings = get_settings()
        if settings.demo_mode:
            # The one seam demo mode needs. Everything below and after this
            # line is the real code path — see src/demo.py for why that
            # matters and for the safety rail that keeps this out of
            # production.
            from src.demo import FakeRazorpayClient

            self._client: Any = FakeRazorpayClient()
            logger.warning(
                "RetryExecutor in DEMO MODE — the payment gateway is a local "
                "fake and no money can move"
            )
        else:
            self._client = razorpay.Client(
                session=_TimeoutSession(settings.razorpay_timeout_seconds),
                auth=(settings.razorpay_key_id, reveal(settings.razorpay_key_secret)),
            )
        # The process-wide dedicated pool (see _shared_pool): one slow
        # Razorpay day can hold up to this many calls, and the rest of the
        # process keeps its own executor untouched. Shared across executor
        # instances so constructing one per request cannot leak threads.
        self._pool = _shared_pool()
        if not settings.demo_mode:
            logger.info(
                "RetryExecutor initialized (key_id=%s...)", settings.razorpay_key_id[:12]
            )

    async def _off_thread(self, fn: Any, *args: Any) -> Any:
        """Run a blocking SDK call on the dedicated Razorpay pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: fn(*args))

    async def _create_link(self, link_data: dict[str, Any]) -> dict[str, Any]:
        """
        Create the Payment Link, surviving Razorpay refusing the UPI-only flag.

        UPI Payment Links do not exist in Test Mode, and a live account can
        also lack the feature — either way the 400 is about the FLAG, not the
        payment. Failing the whole chase over it burns an attempt slot and
        leaves the money unchased when a generic link (card/UPI/netbanking)
        would have collected it just as well. So: if a UPI-only request is
        refused, retry once as a generic link and mark the result
        rail_fallback=True, so the attempt row records that the rail switch
        was downgraded from "enforced" to "preferred". Any other BadRequest
        re-raises — this fallback is for the flag, not a general retry.
        """
        try:
            result = await self._off_thread(self._client.payment_link.create, link_data)
            return {**result, "rail_fallback": False}
        except razorpay.errors.BadRequestError as e:
            if link_data.get("upi_link") and "upi" in str(e).lower():
                logger.warning(
                    "UPI-only link refused (%s) — falling back to a generic link", e
                )
                fallback = {k: v for k, v in link_data.items() if k != "upi_link"}
                result = await self._off_thread(
                    self._client.payment_link.create, fallback
                )
                return {**result, "rail_fallback": True}
            raise

    async def cancel_payment_link(self, link_id: str) -> bool:
        """
        Cancel a Payment Link so it can never be paid late.

        Every retry mints a NEW link for the full amount while the old ones
        stay live on Razorpay's side — a customer paying both an old and a new
        link double-pays, and the case credits the overpayment. When a case
        closes (or an attempt is superseded), the links it spawned must die
        with it. Cancelling an already-paid or already-cancelled link is a
        400 from Razorpay, which this treats as success: the link is inert
        either way, and the sweep that drives this must not retry it forever.

        Returns True when the link is known-inert (cancelled here, already
        paid, already cancelled), False when the call itself failed — the
        caller records that and a later sweep retries.
        """
        try:
            await self._off_thread(self._client.payment_link.cancel, link_id)
            logger.info("Payment link cancelled: id=%s", link_id)
            return True
        except razorpay.errors.BadRequestError as e:
            logger.info("Payment link %s already inert (cancel refused): %s", link_id, e)
            return True
        except Exception:
            logger.warning("Failed to cancel payment link %s — will retry", link_id)
            return False

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
            action_type: The action to execute (retry_now, retry_at,
                switch_rail, nudge_customer, abandon).
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
                # The nudge message doubles as the link's checkout description:
                # it is the one customer-visible surface Razorpay gives us, and
                # without it the personalized message the generator produced was
                # stored on the attempt row and never delivered anywhere.
                return await self._create_payment_link(
                    payment_failure, target_rail, idempotency_key,
                    description=nudge_message,
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

    async def execute_case_action(
        self,
        case: RecoveryCase,
        action_type: str,
        target_rail: str | None,
        idempotency_key: str,
        nudge_message: str | None = None,
        customer_email: str | None = None,
        customer_contact: str | None = None,
        description: str | None = None,
        notify_customer: bool = True,
        offer_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute a recovery action for a case with NO PaymentFailure behind it —
        an abandoned cart, an overdue invoice, a failed subscription charge or
        mandate debit. Same contract as execute_retry (returns success /
        payment_link_id / error), but the amount, currency and customer come
        from the case and the caller instead of a failure row.

        The recovery path is identical to the payment rail's: a Razorpay
        Payment Link the customer pays, which mints a brand-new payment id that
        attribute_capture joins back through external_ref / the idempotency
        key. So the attribution machinery needs no changes — only the source
        of the link's inputs differs.

        notify_customer: every case "retry" MINTS A LINK — unlike the payment
        rail, there is no instrument to re-present silently, so a link nobody
        tells the customer about is a dead chase that still reports success.
        All link actions therefore deliver the link, except the self-serve pay
        route, where the customer is already standing at the checkout (it
        passes notify_customer=False and redirects them itself).

        offer_id: a Razorpay offer id in the merchant's account, applied at
        link creation (options.order.offers). The caller owns the "which touch
        may carry it" rule; Razorpay owns the offer's validity and amount
        rules. A refused/expired offer fails the link call and the attempt
        records the failure — honest, and the next rung mints without it.
        """
        if action_type == "abandon":
            logger.info("Abandon action — no API call: case=%s", case.id)
            return {"success": True, "action": "abandon", "details": "No action taken"}

        # Sanitize once at the funnel so link creation AND the nudge's
        # notify-channel decision see the same surviving values.
        customer_email = sanitize_customer_email(customer_email)
        customer_contact = sanitize_customer_contact(customer_contact)

        try:
            if action_type in ("retry_now", "retry_at", "switch_rail", "nudge_customer"):
                if notify_customer:
                    return await self._send_case_nudge(
                        case, idempotency_key, nudge_message,
                        customer_email=customer_email,
                        customer_contact=customer_contact,
                        description=description,
                        target_rail=target_rail,
                        offer_id=offer_id,
                    )
                return await self._create_case_payment_link(
                    case, target_rail, idempotency_key,
                    description=description or nudge_message,
                    customer_email=customer_email,
                    customer_contact=customer_contact,
                    offer_id=offer_id,
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
            logger.exception("Unexpected error executing case action")
            return {"success": False, "error": str(e)}

    async def _create_case_payment_link(
        self,
        case: RecoveryCase,
        target_rail: str | None,
        idempotency_key: str,
        description: str | None = None,
        customer_email: str | None = None,
        customer_contact: str | None = None,
        offer_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a Payment Link from a case (no PaymentFailure). See
        _create_payment_link for the rail-enforcement and idempotency notes —
        both apply identically here."""
        from src.cases import outstanding_paise

        customer: dict[str, str] = {}
        if customer_email:
            customer["email"] = customer_email
        if customer_contact:
            customer["contact"] = customer_contact

        # The link is for what is STILL OWED, not what was owed when the case
        # opened. Those differ the moment a part payment lands — normal on a
        # B2B invoice — and the case stays open until the balance clears, so
        # every later chase used to mint a link for the full original total
        # and ask the customer a second time for money already paid.
        outstanding = outstanding_paise(case)
        if outstanding <= 0:
            # Nothing left to collect. The stopping rules should have closed
            # this case before we got here; minting a zero-amount link would
            # be refused by Razorpay anyway, and refusing it here says why.
            logger.warning(
                "Refusing to mint a link on case %s — nothing outstanding "
                "(at risk %s, recovered %s)",
                case.id, case.amount_at_risk, case.amount_recovered,
            )
            return {"success": False, "error": "nothing outstanding on this case"}

        link_data: dict[str, Any] = {
            "amount": outstanding,
            "currency": case.currency,
            "description": (
                description[:160]
                if description
                else f"Recovery payment for {case.risk_type} {case.subject_ref}"
            ),
            "customer": customer,
            "notify": {"sms": False, "email": False},
            "notes": {
                # No original payment exists for these risk types — the case's
                # natural key stands in so the link is still traceable.
                "risk_type": case.risk_type,
                "subject_ref": case.subject_ref,
                "retry_idempotency_key": idempotency_key,
                "idempotency_key": idempotency_key,
                "target_rail": target_rail or "any",
            },
        }
        if target_rail == "upi":
            link_data["upi_link"] = True
        # The merchant's incentive, relayed not computed: Razorpay applies the
        # offer's own discount/cashback rules at checkout. Rides at create
        # time — the API has no attach-later path for links.
        if offer_id:
            link_data["options"] = {"order": {"offers": [offer_id]}}

        result = await self._create_link(link_data)

        logger.info(
            "Case payment link created: id=%s, short_url=%s, case=%s",
            result.get("id"),
            result.get("short_url"),
            case.id,
        )

        return {
            "success": True,
            "payment_link_id": result.get("id"),
            "short_url": result.get("short_url"),
            "target_rail": target_rail,
            "rail_fallback": result.get("rail_fallback", False),
            "offer_id": offer_id,
        }

    async def _notify_link(
        self, link_id: str | None, customer_email: str | None, customer_contact: str | None
    ) -> list[str]:
        """
        Send Razorpay's SMS/email notification for a link and report which
        channels actually reached the customer — the attempt row records the
        channel that reached them, not the one we intended (contact limits
        are per-channel, and a nudge logged as "sent" with no channel cannot
        be counted against any of them).
        """
        channels: list[str] = []
        if not link_id:
            return channels
        try:
            # Off the event loop for the same reason as payment_link.create.
            if customer_contact:
                await self._off_thread(self._client.payment_link.notifyBy, link_id, "sms")
                channels.append("sms")
                logger.info("SMS notification sent for link %s", link_id)
            if customer_email:
                await self._off_thread(self._client.payment_link.notifyBy, link_id, "email")
                channels.append("email")
                logger.info("Email notification sent for link %s", link_id)
        except Exception:
            logger.warning("Failed to send notification for link %s", link_id)
        return channels

    async def _send_case_nudge(
        self,
        case: RecoveryCase,
        idempotency_key: str,
        message: str | None,
        customer_email: str | None = None,
        customer_contact: str | None = None,
        description: str | None = None,
        target_rail: str | None = None,
        offer_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a Payment Link and notify the customer, case-driven. Mirrors
        _send_nudge for the no-PaymentFailure risk types. target_rail rides
        through so a switch-to-UPI keeps its UPI-only link even though every
        case action now delivers via this path."""
        link_result = await self._create_case_payment_link(
            case, target_rail, idempotency_key,
            description=description or message,
            customer_email=customer_email,
            customer_contact=customer_contact,
            offer_id=offer_id,
        )

        if not link_result.get("success"):
            return link_result

        link_result["channels"] = await self._notify_link(
            link_result.get("payment_link_id"), customer_email, customer_contact
        )
        link_result["nudge_sent"] = True
        link_result["nudge_message"] = message
        return link_result

    async def _create_payment_link(
        self,
        failure: PaymentFailure,
        target_rail: str | None,
        idempotency_key: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a Razorpay Payment Link for retry.

        `description`, when given, is the customer-facing nudge message: it is
        what the payer reads on the hosted checkout, so the personalized text
        the generator produced actually reaches the customer instead of dying
        in the attempt row. Kept short — the generator caps at 160 chars.
        """
        customer: dict[str, str] = {}
        if failure.customer_email:
            customer["email"] = failure.customer_email
        if failure.customer_contact:
            customer["contact"] = failure.customer_contact

        # MAKE THE RAIL REAL. Until now target_rail was recorded in the ledger
        # and nowhere else — a "switch to UPI" decision executed as a generic
        # link the customer could pay by card, and the switch was decorative.
        # Razorpay's `upi_link: true` creates a UPI-ONLY link: no card form,
        # no netbanking, just a UPI intent/QR. That is the one rail the API
        # lets us enforce, so the executor enforces it; for the other rails a
        # generic link is still the honest best available (recorded as such).
        link_data: dict[str, Any] = {
            "amount": failure.amount,
            "currency": failure.currency,
            "description": (
                description[:160]
                if description
                else f"Retry payment for order {failure.order_id or failure.payment_id}"
            ),
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
                "target_rail": target_rail or "any",
            },
        }
        if target_rail == "upi":
            link_data["upi_link"] = True

        # razorpay-python is synchronous `requests`. Calling it directly from a
        # coroutine blocks the whole event loop: one slow Razorpay response
        # freezes every other in-flight webhook and /health along with them.
        # Runs on the executor's DEDICATED pool (see __init__), so sustained
        # concurrency beyond its workers queues Razorpay work specifically
        # rather than starving the whole process.
        result = await self._create_link(link_data)

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
            "rail_fallback": result.get("rail_fallback", False),
        }

    async def _send_nudge(
        self,
        failure: PaymentFailure,
        idempotency_key: str,
        message: str | None,
    ) -> dict[str, Any]:
        """Create a Payment Link and notify the customer.

        The personalized message is carried as the link's checkout description
        (the one customer-visible text Razorpay lets us set); notifyBy then
        sends Razorpay's own SMS/email template pointing at that link.
        """
        # First create the payment link
        link_result = await self._create_payment_link(
            failure, None, idempotency_key, description=message
        )

        if not link_result.get("success"):
            return link_result

        # Then send notification — which channels actually reached them.
        link_result["channels"] = await self._notify_link(
            link_result.get("payment_link_id"),
            failure.customer_email,
            failure.customer_contact,
        )
        link_result["nudge_sent"] = True
        link_result["nudge_message"] = message
        return link_result
