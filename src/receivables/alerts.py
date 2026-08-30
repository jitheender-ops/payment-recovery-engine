"""Merchant alerts — the writeback queue that makes chasing fully automated.

The merchant's AR workflow cannot be "watch a dashboard". A promise made, a
dispute opened, a plan defaulting, a chase exhausting its budget — each is
a fact their ERP or their inbox must hear about at the moment it happens.
This module is the queue AND the drain: every receivables transition appends
one alert row (raise_alert), and deliver_pending_alerts POSTs HMAC-signed
payloads to the merchant's configured URL on the scheduler's tick.

Closed vocabulary (AlertType) so merchant automation can branch safely on
the type without a changelog breaking their integration. Detail payloads
are PII-free by construction: refs and amounts, never emails or phones.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings, reveal
from src.receivables.models import MerchantAlert

logger = logging.getLogger(__name__)

# The delivery re-arm cap, mirroring EVENT_RECONCILE_MAX_ATTEMPTS: one number,
# one meaning, for every retry-the-merchant's-copy discipline in the codebase.
_MAX_DELIVERY_ATTEMPTS = 3

AlertType = Literal[
    "promise_made",
    "promise_broken",
    "dispute_opened",
    "dispute_upheld",
    "dispute_rejected",
    "plan_requested",
    "plan_instalment_missed",
    "plan_completed",
    "plan_defaulted",
    "chase_exhausted",
    "external_payment_recorded",
    "call_task_raised",
]


async def raise_alert(
    session: AsyncSession,
    *,
    event_type: AlertType,
    account_ref: str | None = None,
    case_ref: str | None = None,
    detail: dict[str, object] | None = None,
) -> MerchantAlert:
    """
    Append one alert to the queue. In-memory append (session.add), the caller
    owns the commit — same contract as cases.log_event, and for the same
    reason: the alert lands in the same transaction as the transition it
    describes, so an alert can never claim something that rolled back.
    """
    alert = MerchantAlert(
        event_type=event_type,
        account_ref=account_ref,
        case_ref=case_ref,
        detail=detail or {},
    )
    session.add(alert)
    await session.flush()
    logger.info(
        "Merchant alert queued: %s case=%s account=%s",
        event_type, case_ref, account_ref,
    )
    return alert


# ── Outbound delivery ───────────────────────────────────────────────────────
# The writeback half: alerts are facts the merchant's systems must hear at the
# moment they happen, not a dashboard to watch. Delivery is an HMAC-signed
# POST to the merchant's configured URL — the mirror image of POST /risks:
# they sign their pushes with RISK_WEBHOOK_SECRET, we sign ours with the same
# secret, one identity in both directions.
#
# Undelivered rows stay queued (delivered=False) and surface on the console's
# alerts panel, so an unconfigured or broken webhook degrades to a visible
# queue — never a silent drop.


async def deliver_pending_alerts(
    session: AsyncSession, *, limit: int = 20, timeout: float = 3.0
) -> int:
    """
    Drain queued alerts to the merchant's webhook. Returns how many delivered.

    Fail-soft by design: a delivery failure counts one attempt, records the
    error, and the row stays queued for the next pass. The shared re-arm cap
    (3 attempts, mirroring EVENT_RECONCILE_MAX_ATTEMPTS) stops a dead endpoint
    from being hammered forever — past it, the alert rests visible on the
    console for a human to read.
    """
    import hmac as _hmac
    import json
    from hashlib import sha256

    import httpx
    from sqlalchemy import select

    settings = get_settings()
    url = settings.merchant_webhook_url
    if not url:
        return 0
    secret = reveal(settings.risk_webhook_secret)
    if not secret:
        # The queue still fills (raise_alert always appends); only the
        # outbound leg is off. Fail closed on signing, same rule as inbound.
        return 0

    rows = (
        await session.execute(
            select(MerchantAlert)
            .where(
                MerchantAlert.delivered.is_(False),
                MerchantAlert.delivery_attempts < _MAX_DELIVERY_ATTEMPTS,
            )
            .order_by(MerchantAlert.created_at)
            .limit(limit)
        )
    ).scalars().all()

    delivered = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        for alert in rows:
            body = json.dumps(
                {
                    "event_type": alert.event_type,
                    "account_ref": alert.account_ref,
                    "case_ref": alert.case_ref,
                    "detail": alert.detail,
                    "occurred_at": alert.created_at.isoformat(),
                },
                separators=(",", ":"),
            ).encode()
            signature = _hmac.new(secret.encode(), body, sha256).hexdigest()
            try:
                response = await client.post(
                    url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Alert-Signature": signature,
                    },
                )
                if response.status_code < 300:
                    alert.delivered = True
                    alert.delivered_at = datetime.now(UTC)
                    delivered += 1
                else:
                    raise RuntimeError(f"status {response.status_code}")
            except Exception as exc:
                alert.delivery_attempts += 1
                alert.last_error = f"{type(exc).__name__}: {exc}"[:500]
                if alert.delivery_attempts >= _MAX_DELIVERY_ATTEMPTS:
                    logger.error(
                        "Alert %s rests after %d delivery attempts: %s",
                        alert.id, alert.delivery_attempts, alert.last_error,
                    )
                else:
                    logger.warning(
                        "Alert delivery failed (will retry): %s",
                        alert.last_error,
                    )
    if rows:
        await session.commit()
    return delivered
