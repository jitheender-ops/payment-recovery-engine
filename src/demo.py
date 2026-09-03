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
import os
import time
import uuid
from html import escape
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings, reveal
from src.database import get_session

logger = logging.getLogger(__name__)

router = APIRouter()

# The links this fake has minted, by id. In-process and deliberately not
# persisted: demo mode is a single local process, and a restart losing its
# fake links is correct — the cases that own them are in the database and
# a fresh chase mints fresh ones, exactly as it would against Razorpay.
_LINKS: dict[str, dict[str, Any]] = {}
# Mandates authorised by the fake registration link, by token id. Same
# in-process, deliberately-not-persisted discipline as _LINKS above.
_MANDATES: dict[str, dict[str, Any]] = {}


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


# ── The feature catalogue ────────────────────────────────────────────────
#
# Every capability this engine has, and the one case each is visible on.
#
# WHY THIS EXISTS: all of it was built and rendering, and still effectively
# invisible. The link printer grouped by PAGE STATE (payable / confirming /
# recovered) and printed one link per state, so every feature sat inside
# "PAYABLE (30 cases)" behind a single link that was usually a plain card
# decline. A state is what the page is doing; a feature is what you came to
# look at, and they are not the same index.
#
# Each entry is (label, what to look at, SQL picking a case that shows it).
# The SQL is the honest part: it selects a case genuinely exhibiting the
# feature, so a missing link means the demo has no such case rather than the
# feature being broken.
#
# Lives HERE rather than in scripts/ because it now has two readers — the
# /demo hub page below and scripts/recovery_links.py — and two copies of
# "what this product does" would drift within a week.
FEATURES: list[tuple[str, str, str]] = [
    (
        "Checkout drop-off recovery",
        "cart contents named back, honest 'nothing was charged' framing",
        "SELECT id FROM recovery_cases WHERE risk_type='checkout_abandonment' "
        "AND state='open' LIMIT 1",
    ),
    (
        "Failed-subscription recovery",
        "renewal copy + the retry-sequence panel (attempts made, one hollow "
        "'upcoming' row)",
        "SELECT id FROM recovery_cases WHERE risk_type='subscription_failure' "
        "AND state='open' LIMIT 1",
    ),
    (
        "Mandate retry sequencer",
        "same panel on the RBI e-mandate path — the upcoming row states WHEN, "
        "never WHAT",
        "SELECT id FROM recovery_cases WHERE risk_type='mandate_failure' "
        "AND state='open' LIMIT 1",
    ),
    (
        "B2B receivables chaser",
        "invoice copy, due date and computed days-overdue in the register",
        "SELECT id FROM recovery_cases WHERE risk_type='invoice_overdue' "
        "AND state='open' LIMIT 1",
    ),
    (
        "Payment degradation → root cause → action",
        "the decline explained in the customer's words, plus the rail "
        "recommendation named out loud above the CTA",
        "SELECT rc.id FROM recovery_cases rc "
        "JOIN payment_failures pf ON pf.payment_id = rc.subject_ref "
        "WHERE rc.state='open' AND pf.method <> 'upi' AND pf.failure_class IN "
        "('3ds_dropoff','card_limit_exceeded','issuer_decline',"
        "'insufficient_funds','invalid_card','expired_instrument') LIMIT 1",
    ),
    (
        "Promise-to-pay tracker",
        "'You said you'd pay by {date}. N days left.' — persistent, not a flash",
        "SELECT rc.id FROM recovery_cases rc "
        "JOIN promises_to_pay p ON p.recovery_case_id = rc.id "
        "WHERE p.status='pending' AND rc.state='open' LIMIT 1",
    ),
    (
        "Dispute → chase freeze",
        "raised through open_dispute(): the invoice reads 'under review — "
        "reminders paused' on the statement, and the console lists it",
        "SELECT case_id FROM case_disputes WHERE status='open' LIMIT 1",
    ),
    (
        "Instalment plan (4 of a possible 6)",
        "the form shipped 2 hardcoded rows until this session — open the "
        "'pay in parts' box to see all six reachable",
        "SELECT case_id FROM payment_plans LIMIT 1",
    ),
    (
        "Hinglish voice recovery",
        "'We called you on {date}' in the timeline; add ?lang=hi for the "
        "Hindi page",
        "SELECT rc.id FROM recovery_cases rc "
        "JOIN voice_call_queue v ON v.recovery_case_id = rc.id "
        "WHERE v.state='done' LIMIT 1",
    ),
]




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


class _FakeRegistrationLink:
    """`client.registration_link` — where a UPI Autopay mandate is authorised."""

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        auth_id = f"inv_demo_{uuid.uuid4().hex[:10]}"
        token_id = f"token_demo_{uuid.uuid4().hex[:10]}"
        base = get_settings().public_base_url.rstrip("/") or "http://127.0.0.1:8000"
        registration = data.get("subscription_registration") or {}
        record = {
            "id": auth_id,
            "entity": "invoice",
            "status": "issued",
            "amount": data.get("amount"),
            "currency": data.get("currency", "INR"),
            "description": data.get("description"),
            "notes": data.get("notes", {}),
            "short_url": f"{base}/demo/mandate/{auth_id}",
            # Nested exactly where the real payload nests it, so
            # RetryExecutor._parse_mandate_authorization is exercised on the
            # same shape it will meet live rather than a convenient flat one.
            "subscription_registration": {
                "method": registration.get("method", "upi"),
                "max_amount": registration.get("max_amount"),
                "token_id": token_id,
            },
        }
        _MANDATES[token_id] = record
        logger.info(
            "DEMO gateway: authorised mandate %s up to %s paise",
            token_id, registration.get("max_amount"),
        )
        return dict(record)


class _FakeOrder:
    """`client.order` — the amount envelope a recurring charge is made against."""

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        order_id = f"order_demo_{uuid.uuid4().hex[:10]}"
        logger.info("DEMO gateway: order %s for %s paise", order_id, data.get("amount"))
        return {
            "id": order_id,
            "entity": "order",
            "amount": data.get("amount"),
            "currency": data.get("currency", "INR"),
            "status": "created",
            "notes": data.get("notes", {}),
        }


class _FakePayment:
    """`client.payment` — the unattended debit against an authorised mandate."""

    def createRecurring(self, data: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        # Named for the real SDK's method, which is camelCase.
        token = data.get("token")
        if token not in _MANDATES:
            # The one failure the fake DOES raise. A debit against a token that
            # was never authorised is the exact bug this feature could ship —
            # money taken on consent that does not exist — so demo mode must
            # not be the place it looks like it works.
            raise DemoBadRequestError(f"no such mandate token: {token}")
        payment_id = f"pay_demo_{uuid.uuid4().hex[:10]}"
        logger.info(
            "DEMO gateway: debited mandate %s for %s paise -> %s",
            token, data.get("amount"), payment_id,
        )
        return {"razorpay_payment_id": payment_id, "status": "captured"}


class _FakeToken:
    """`client.token` — the authoritative answer to "is this mandate live?"."""

    def fetch(self, customer_id: str, token_id: str) -> dict[str, Any]:
        record = _MANDATES.get(token_id)
        if record is None:
            # Unknown token. Not "rejected": we genuinely do not know, and the
            # reconciler treats unknown as "ask again", never as consent.
            return {"id": token_id, "status": "created"}
        # Demo authorisations are approved the moment they are minted — there
        # is no customer to send to a UPI app — so the demo can show the whole
        # promise-to-collection arc without a second actor.
        return {
            "id": token_id,
            "entity": "token",
            "recurring_details": {"status": "confirmed"},
            "customer_id": customer_id,
        }


class FakeRazorpayClient:
    """A stand-in for `razorpay.Client` covering every call this engine makes:
    payment_link.create/.cancel/.notifyBy, registration_link.create,
    order.create, payment.createRecurring and token.fetch."""

    def __init__(self) -> None:
        self.payment_link = _FakePaymentLink()
        self.registration_link = _FakeRegistrationLink()
        self.order = _FakeOrder()
        self.payment = _FakePayment()
        self.token = _FakeToken()


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


# ── The hub ──────────────────────────────────────────────────────────────
#
# One page holding every surface this engine has. It exists because the
# alternative was what the demo shipped with: nine capability links printed
# to a terminal, a password-gated console on one port, a Streamlit app on
# another, and a statement page per B2B account. Each of those is correct on
# its own and the set of them is not a dashboard — you cannot see the
# product from any one of them.
#
# It is a DEMO surface, not a product one, and it is mounted only in demo
# mode. It deliberately hands out live customer tokens, which is exactly
# what must never happen on a real merchant console: those tokens are bearer
# credentials for one customer's own page. Here every case is synthetic.


async def _resolve_features(session: Any) -> list[dict[str, Any]]:
    """Turn the FEATURES catalogue into openable links against this database."""
    from sqlalchemy import text

    from src import recovery_link

    out: list[dict[str, Any]] = []
    for label, blurb, query in FEATURES:
        try:
            case_id = (await session.execute(text(query))).scalar()
        except Exception:
            # A table this database has not created yet. Report the feature
            # as unavailable rather than taking the whole page down.
            case_id = None
        resolved = _as_uuid(case_id)
        out.append({
            "label": label,
            "blurb": blurb,
            "url": recovery_link.url_for(resolved) if resolved else None,
        })
    return out


def _as_uuid(value: Any) -> uuid.UUID | None:
    """Raw SQL returns a UUID on Postgres and a string on SQLite."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


async def _resolve_statements(session: Any) -> list[dict[str, Any]]:
    """One statement link per AR account that actually has open invoices."""
    from sqlalchemy import text

    from src import recovery_link

    try:
        rows = (await session.execute(text("""
            SELECT a.id, a.display_name, a.account_ref, COUNT(rc.id) AS n
            FROM ar_accounts a
            JOIN recovery_cases rc ON rc.account_id = a.id
            WHERE rc.risk_type = 'invoice_overdue' AND rc.state = 'open'
            GROUP BY a.id, a.display_name, a.account_ref
            ORDER BY n DESC
        """))).all()
    except Exception:
        return []
    out = []
    for account_id, name, ref, count in rows:
        resolved = _as_uuid(account_id)
        url = recovery_link.url_for_account(resolved) if resolved else None
        if url:
            out.append({"name": name or ref, "url": url, "count": count})
    return out


_CONSOLE_PAGES = [
    ("/console/live",
     "the B2B half — ladder rung per account, promises, disputes, instalment "
     "plans, aging, voice, merchant alerts"),
    ("/console/pipeline",
     "payment degradation — where money leaves, what the gateway blamed, and "
     "what each failure class recovers once chased"),
    ("/console/routing", "which bank on which rail — the switch_rail evidence"),
    ("/console/cases", "every case, filterable by state"),
    ("/console/ops", "is the machinery running — sweeps, heartbeat, what fires next"),
    ("/console/evidence", "the audit trail behind a recovered rupee"),
]


@router.get("/demo", response_class=HTMLResponse)
async def demo_hub(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Every surface this engine has, on one page."""
    settings = get_settings()
    features = await _resolve_features(session)
    statements = await _resolve_statements(session)
    base = str(request.base_url).rstrip("/")
    password = reveal(settings.dashboard_password) or "(unset)"
    streamlit = f"http://127.0.0.1:{os.environ.get('STREAMLIT_PORT', '8501')}"

    def card(label: str, blurb: str, url: str | None) -> str:
        if url is None:
            return (f'<div class="row dim"><div><b>{escape(label)}</b>'
                    f'<span>{escape(blurb)}</span></div>'
                    f'<em>no case in this database shows it yet</em></div>')
        return (f'<a class="row" href="{escape(url)}" target="_blank" rel="noopener">'
                f'<div><b>{escape(label)}</b><span>{escape(blurb)}</span></div>'
                f'<em>open &rarr;</em></a>')

    feature_rows = "".join(card(f["label"], f["blurb"], f["url"]) for f in features)
    statement_rows = "".join(
        card(s["name"], f"{s['count']} open invoice"
             f"{'s' if s['count'] != 1 else ''} — totals, aging, per-row pay links",
             s["url"])
        for s in statements
    ) or '<div class="row dim"><div><b>No AR account has open invoices</b>'\
         '<span>run the seed again</span></div></div>'
    console_rows = "".join(
        card(path, blurb, base + path) for path, blurb in _CONSOLE_PAGES
    )

    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Recovery engine — demo</title>
<style>
 :root{{--bg:#0e1013;--card:#16191d;--line:#262b31;--ink:#e8eaed;
        --dim:#8b939c;--brass:#c9a227;--green:#3fb950}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--ink);
   font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
 .wrap{{max-width:60rem;margin:0 auto;padding:2.5rem 1.25rem 4rem}}
 h1{{font-size:1.5rem;margin:0 0 .3rem}}
 .sub{{color:var(--dim);margin:0 0 .6rem;font-size:.92rem}}
 .flag{{background:#2a2412;border:1px solid #5c4a12;border-radius:7px;
   padding:.7rem .9rem;color:#e8d9a0;font-size:.85rem;margin:1.2rem 0 2rem}}
 h2{{font-size:.72rem;text-transform:uppercase;letter-spacing:.13em;
   color:var(--dim);margin:2.2rem 0 .7rem;font-weight:600}}
 .row{{display:flex;gap:1rem;align-items:center;justify-content:space-between;
   background:var(--card);border:1px solid var(--line);border-radius:8px;
   padding:.8rem 1rem;margin-bottom:.5rem;text-decoration:none;color:inherit}}
 a.row:hover{{border-color:var(--brass)}}
 .row b{{display:block;font-size:.95rem;font-weight:600}}
 .row span{{display:block;color:var(--dim);font-size:.84rem;margin-top:.15rem}}
 .row em{{font-style:normal;color:var(--brass);font-size:.82rem;white-space:nowrap}}
 .dim{{opacity:.55}} .dim em{{color:var(--dim)}}
 code{{background:#0a0c0e;border:1px solid var(--line);border-radius:4px;
   padding:.12rem .4rem;font-size:.85rem}}
 ol{{color:var(--dim);font-size:.9rem;padding-left:1.2rem}}
 ol b{{color:var(--ink)}}
</style></head><body><div class="wrap">
<h1>Payment recovery engine</h1>
<p class="sub">Every surface, on one page. Links open in a new tab.</p>

<div class="flag"><b>Demo mode.</b> The payment gateway is a local fake and
no money can move. Every recovered rupee below is fictional. The customer
links are live capability tokens — that is what a customer receives, and it
is why a real merchant console would never list them.</div>

<h2>The customer's page, one per capability</h2>
{feature_rows}

<h2>Account statements &mdash; every open invoice for one B2B buyer</h2>
{statement_rows}

<h2>Merchant console &mdash; password <code>{escape(password)}</code></h2>
{console_rows}

<h2>Analytics dashboard</h2>
<a class="row" href="{escape(streamlit)}" target="_blank" rel="noopener">
  <div><b>Streamlit &mdash; {escape(streamlit)}</b><span>recovery funnel, bank
  &times; rail heatmap, chaser effectiveness, promises, receivables, voice,
  audit</span></div><em>open &rarr;</em></a>

<h2>See the money loop close</h2>
<ol>
  <li>Open any <b>customer page</b> above that offers a pay button.</li>
  <li>Press <b>Pay</b> &mdash; you land on the fake gateway's checkout.</li>
  <li>Press <b>Pay (pretend)</b>. It sends a genuinely HMAC-signed
      <code>payment.captured</code> to the real webhook endpoint.</li>
  <li>Reload the customer page: it reads <b>recovered</b> with a receipt, and
      the console's recovered figure has moved.</li>
</ol>
</div></body></html>""")


@router.post("/demo/pay-batch", response_class=HTMLResponse)
async def demo_pay_batch(request: Request) -> HTMLResponse:
    """
    Have a share of the customers actually pay — demo only.

    Why this exists: a batch run reports attempts ACCEPTED, which honestly
    means "a payment link now exists", not "the money came back". Recovery is
    only counted when a capture webhook lands and is attributed. In
    production that happens over hours as real customers pay; in a demo
    nobody pays, so the batch would forever read ₹0 recovered and the loop
    would never visibly close.

    This fires genuinely HMAC-signed `payment.captured` events at the real
    webhook endpoint for a share of the outstanding demo links — the same
    path a real payment takes, through signature verification, the
    idempotency guard and the attribution join. Nothing about the
    measurement is faked; what is simulated is customers deciding to pay.
    """
    settings = get_settings()
    try:
        share = float(str((await request.form()).get("share") or "0.45"))
    except (TypeError, ValueError):
        share = 0.45
    share = min(max(share, 0.0), 1.0)

    unpaid = [r for r in _LINKS.values() if r["status"] != "paid"]
    # Deterministic, not random: a demo that reports a different figure on
    # every click is one nobody can check against the console afterwards.
    take = unpaid[: int(len(unpaid) * share)]

    target = f"{request.base_url}".rstrip("/") + "/webhooks/razorpay"
    paid = 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        for record in take:
            payload = captured_payload(
                int(record["amount"] or 0),
                idempotency_key=record.get("notes", {}).get("retry_idempotency_key"),
            )
            body = json.dumps(payload).encode()
            try:
                resp = await client.post(
                    target, content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sign(
                            body, reveal(settings.razorpay_webhook_secret)
                        ),
                    },
                )
            except Exception:
                logger.exception("DEMO: capture delivery failed for %s", record["id"])
                continue
            if resp.status_code == 200:
                record["status"] = "paid"
                paid += 1

    logger.info("DEMO: %d of %d outstanding links paid", paid, len(unpaid))
    return _page(
        "Payments simulated",
        f"""<h1>{paid} customers paid</h1>
<p class="sub">Each sent a signed <code>payment.captured</code> to the real
webhook endpoint, so every rupee went through signature verification, the
idempotency guard and the attribution join — exactly as a real payment
would. What was simulated is customers <em>deciding</em> to pay, not the
accounting.</p>
<p class="sub">Go back to <code>/console/batch</code> and re-run, or open
<code>/console/live</code>: the recovered figure has moved.</p>""",
    )


def demo_downtime_payload() -> dict[str, Any]:
    """
    A downtime feed, in Razorpay's documented collection shape.

    The real endpoint is an ON-DEMAND feature — their docs say it must be
    enabled by contacting support — so a fresh test account cannot call it
    and the integration would be undemonstrable. This serves the same shape
    locally, exactly as this module serves Payment Links: the client, the
    parsing and the routing decision are all the real ones, and only the
    data source is local.

    One method-wide outage and one issuer-scoped one, because those are the
    two cases `DowntimeSnapshot.is_down()` has to tell apart — an
    issuer-scoped outage matching every bank would route the whole book off
    a rail because one bank is down.
    """
    return {
        "entity": "collection",
        "count": 2,
        "items": [
            {
                "id": "down_demo_netbanking",
                "entity": "payment.downtime",
                "method": "netbanking",
                "status": "started",
                "severity": "high",
                "instrument": {"bank": "PNB"},
                "created_at": int(time.time()) - 1800,
            },
            {
                "id": "down_demo_wallet",
                "entity": "payment.downtime",
                "method": "wallet",
                "status": "started",
                "severity": "medium",
                "instrument": {},
                "created_at": int(time.time()) - 600,
            },
            {
                # Resolved rows must never steer routing — included so the
                # parser's status filter is exercised by the demo itself.
                "id": "down_demo_resolved",
                "entity": "payment.downtime",
                "method": "upi",
                "status": "resolved",
                "severity": "low",
                "instrument": {},
                "created_at": int(time.time()) - 7200,
            },
        ],
    }
