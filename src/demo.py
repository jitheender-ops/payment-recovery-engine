"""
Demo mode: the payment gateway, faked in-process, so the whole engine runs
on a laptop with no credentials and no network.

WHY THIS EXISTS. Every other part of this engine already runs locally — the
API, the scheduler, the console, the customer page. The one thing that does
not is the money path: `RetryExecutor` calls Razorpay to mint a Payment
Link, and without real keys that call 401s. So the single most important
thing to demonstrate — a failed payment being chased, paid, attributed, and
turned into a recovered rupee on the dashboard — was the one thing you could
not show without a Razorpay account.

WHAT IT DOES NOT DO. It does not shortcut the engine. The fake replaces
exactly one object: the Razorpay SDK client. Everything downstream is the
real code — the executor's link handling, the webhook endpoint's signature
check, the idempotency guard, the event store, the background attribution
task, the guardrail. The stub checkout page fires a genuinely HMAC-signed
`payment.captured` at the real `/webhooks/razorpay` endpoint over loopback,
because a demo that skipped that path would be demonstrating something other
than the product.

WHAT IT FAKES, AND FAITHFULLY. The response shapes here are Razorpay's
documented ones, not invented ones (razorpay.com/docs, via llms.txt):
Payment Link objects carry `id`/`short_url`/`status`, and failures use the
documented error envelope — `error.code/description/source/step/reason` —
which is the same 5-tuple `src/classifier/mapper.py` classifies on. That is
what makes turning demo mode off a config change rather than a code change.

SAFETY. `Settings._demo_mode_is_development_only` refuses this outside
development, and the stub page says plainly that no money is moving. A fake
gateway that always succeeds looks exactly like a healthy business from
every downstream signal; it must never be able to run where someone would
believe it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.config import get_settings, reveal

logger = logging.getLogger(__name__)

router = APIRouter()

# The links this fake has minted, by id. In-process and deliberately not
# persisted: demo mode is a single local process, and a restart losing its
# fake links is correct — the cases that own them are in the database and
# a fresh chase mints fresh ones, exactly as it would against Razorpay.
_LINKS: dict[str, dict[str, Any]] = {}


def sign(body: bytes, secret: str) -> str:
    """The X-Razorpay-Signature over a raw body — the counterpart to
    `src.ingestion.signature.verify_webhook_signature`."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def captured_payload(
    amount: int, *, idempotency_key: str | None = None, order_id: str | None = None
) -> dict[str, Any]:
    """
    A `payment.captured` event for a payment nobody has seen before — which
    is the whole difficulty this engine exists to solve.

    With `idempotency_key`, this is one of our links being paid: Razorpay
    copies a link's notes onto the payment, so the breadcrumb the executor
    wrote comes back and the money is credited to the attempt that earned
    it. With only `order_id`, the customer paid the original order
    themselves — real revenue, credited to the case but explicitly NOT to
    us.

    Lives here rather than in scripts/ because it is one fact — the shape
    Razorpay sends — and both the demo gateway and simulate_webhooks.py
    need it. Two copies would be free to drift.
    """
    entity: dict[str, Any] = {
        "id": f"pay_demo_{uuid.uuid4().hex[:12]}",  # deliberately a NEW id
        "entity": "payment",
        "amount": amount,
        "currency": "INR",
        "status": "captured",
        "method": "upi",
        "created_at": int(time.time()),
        "notes": {"retry_idempotency_key": idempotency_key} if idempotency_key else {},
    }
    if order_id:
        entity["order_id"] = order_id
    return {
        "entity": "event",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
        "created_at": int(time.time()),
    }


class DemoBadRequestError(Exception):
    """
    Stands in for `razorpay.errors.BadRequestError`.

    The real SDK raises that type and `RetryExecutor._create_link` catches
    it to handle Razorpay refusing the UPI-only flag. Demo mode never
    refuses, so this is never raised today — it exists so the fake's
    contract is honest about what the real client can do, rather than
    quietly having no failure mode at all.
    """


class _FakePaymentLink:
    """`client.payment_link` — the only SDK surface this engine touches."""

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        link_id = f"plink_demo_{uuid.uuid4().hex[:10]}"
        base = get_settings().public_base_url.rstrip("/") or "http://127.0.0.1:8000"
        record = {
            "id": link_id,
            "entity": "payment_link",
            "status": "created",
            "amount": data.get("amount"),
            "currency": data.get("currency", "INR"),
            "description": data.get("description"),
            "notes": data.get("notes", {}),
            # Where the customer is actually sent. The real short_url points
            # at Razorpay's hosted checkout; this one points at ours.
            "short_url": f"{base}/demo/checkout/{link_id}",
            "upi_link": bool(data.get("upi_link")),
        }
        _LINKS[link_id] = record
        logger.info(
            "DEMO gateway: minted %s for %s paise (upi_only=%s)",
            link_id, record["amount"], record["upi_link"],
        )
        return dict(record)

    def cancel(self, link_id: str) -> dict[str, Any]:
        record = _LINKS.get(link_id)
        if record is not None:
            record["status"] = "cancelled"
        logger.info("DEMO gateway: cancelled %s", link_id)
        return {"id": link_id, "status": "cancelled"}

    def notifyBy(self, link_id: str, medium: str) -> dict[str, Any]:  # noqa: N802
        # Named for the real SDK's method, which is camelCase.
        logger.info("DEMO gateway: pretended to send %s for %s", medium, link_id)
        return {"success": True}


class FakeRazorpayClient:
    """A stand-in for `razorpay.Client` covering the three calls this engine
    makes: payment_link.create, .cancel and .notifyBy."""

    def __init__(self) -> None:
        self.payment_link = _FakePaymentLink()


# ── The stub checkout ────────────────────────────────────────────────────


def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    """One tiny self-contained page. No shared shell on purpose: this is
    pretending to be a THIRD PARTY's checkout, and borrowing the recovery
    page's chrome would blur which surface a viewer is looking at."""
    return HTMLResponse(status_code=status, content=f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
 body{{font:16px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
   margin:0;background:#f6f7f9;color:#16191d;
   display:flex;align-items:center;justify-content:center;min-height:100vh}}
 .card{{background:#fff;border:1px solid #e3e6ea;border-radius:10px;
   padding:28px;max-width:26rem;width:calc(100% - 2rem)}}
 .flag{{background:#fff4d6;border:1px solid #e8c765;border-radius:6px;
   padding:10px 12px;font-size:.84rem;margin:0 0 18px}}
 h1{{font-size:1.1rem;margin:0 0 4px}}
 .amt{{font-size:2.2rem;font-weight:600;letter-spacing:-.02em;margin:14px 0 2px;
   font-variant-numeric:tabular-nums}}
 .sub{{color:#666e78;font-size:.88rem;margin:0 0 20px}}
 button{{width:100%;padding:14px;font-size:1rem;font-weight:600;color:#fff;
   background:#16191d;border:0;border-radius:6px;cursor:pointer}}
 code{{background:#f0f2f4;padding:1px 5px;border-radius:3px;font-size:.82rem}}
</style></head><body><div class="card">
<p class="flag"><strong>Demo gateway — not Razorpay.</strong> No money moves
here. This page stands in for Razorpay's hosted checkout so the recovery
loop can be shown end to end offline.</p>
{body}
</div></body></html>""")


@router.get("/demo/checkout/{link_id}", response_class=HTMLResponse)
async def demo_checkout(link_id: str) -> HTMLResponse:
    """Where a demo Payment Link's short_url lands."""
    record = _LINKS.get(link_id)
    if record is None:
        # A link minted before a restart. Say so plainly rather than 404ing
        # into a dead end — this is the one thing that surprises people.
        return _page(
            "Link not found",
            "<h1>This demo link is gone</h1><p class='sub'>Demo links live in "
            "memory only, so a server restart clears them. Re-run the seed to "
            "mint fresh ones.</p>",
            status=404,
        )
    if record["status"] == "paid":
        return _page(
            "Already paid",
            "<h1>Already paid</h1><p class='sub'>This demo link has been used. "
            "Go back to the recovery page — it should now read "
            "<code>recovered</code>.</p>",
        )

    rupees = f"₹{(record['amount'] or 0) / 100:,.2f}".replace(".00", "")
    return _page(
        "Demo checkout",
        f"""<h1>{record.get('description') or 'Recovery payment'}</h1>
<p class="amt">{rupees}</p>
<p class="sub">{'UPI only' if record['upi_link'] else 'Any method'} ·
  <code>{link_id}</code></p>
<form method="post" action="/demo/checkout/{link_id}/pay">
  <button type="submit">Pay (pretend)</button>
</form>""",
    )


@router.post("/demo/checkout/{link_id}/pay")
async def demo_pay(link_id: str, request: Request) -> HTMLResponse:
    """
    Pretend the customer paid, then tell the engine the way Razorpay would.

    This deliberately goes over HTTP to the real `/webhooks/razorpay`
    endpoint with a real HMAC signature, rather than calling the attribution
    function directly. Signature verification, the idempotency guard, the
    append-only event store and the background attribution task are all
    things that can break, and a demo that bypassed them would be
    demonstrating a code path that does not ship.
    """
    record = _LINKS.get(link_id)
    if record is None:
        return _page(
            "Link not found",
            "<h1>This demo link is gone</h1><p class='sub'>Demo links live in "
            "memory only, so a server restart clears them.</p>",
            status=404,
        )
    if record["status"] == "paid":
        return _page(
            "Already paid",
            "<h1>Already paid</h1><p class='sub'>Nothing further happened — the "
            "engine's idempotency guard would have caught a repeat anyway.</p>",
        )

    settings = get_settings()
    payload = captured_payload(
        int(record["amount"] or 0),
        idempotency_key=record.get("notes", {}).get("retry_idempotency_key"),
    )
    body = json.dumps(payload).encode()
    # Loopback: this process talking to itself, the way the gateway would
    # talk to us from outside. base_url comes from the live request, so a
    # non-default port needs no configuration of its own.
    target = f"{request.base_url}".rstrip("/") + "/webhooks/razorpay"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                target,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": sign(
                        body, reveal(settings.razorpay_webhook_secret)
                    ),
                },
            )
        delivered = resp.status_code == 200
    except Exception:
        logger.exception("DEMO gateway: could not deliver the capture webhook")
        delivered = False

    if not delivered:
        return _page(
            "Capture not delivered",
            "<h1>The capture webhook did not land</h1><p class='sub'>The pretend "
            "payment happened but the engine was not told, so nothing was "
            "attributed. Check the server log — this is a real failure, not a "
            "staged one.</p>",
            status=502,
        )

    record["status"] = "paid"
    logger.info("DEMO gateway: %s paid, capture delivered", link_id)
    return _page(
        "Payment complete",
        "<h1>Paid</h1><p class='sub'>The engine has been sent a signed "
        "<code>payment.captured</code>, exactly as Razorpay would. Go back to "
        "the recovery page: it should now read <strong>recovered</strong> with "
        "a receipt, and the dashboard's recovered figure should have moved.</p>",
    )
