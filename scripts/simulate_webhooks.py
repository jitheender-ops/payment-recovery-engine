"""
Drive the whole engine with synthetic Razorpay traffic, end to end.

Two phases, because one of them was missing and it was the important one:

    1. payment.failed   opens cases, runs the agent, spends attempt budget
    2. payment.captured  the money comes back, and gets attributed

This script used to send only phase 1. Everything downstream of "a case was
opened" was therefore invisible — amount_recovered stayed 0 forever, the ledger
band on the dashboard stayed empty, and the one number the whole project exists
to produce could not be demonstrated at all.

Phase 2 closes the loop the way Razorpay does. A recovered payment carries an id
we have never seen (paying a link mints a new one), so the capture is matched
back through the breadcrumb the executor wrote: `notes.retry_idempotency_key`.
A slice of captures deliberately carries only `order_id` instead — that is the
customer paying on their own, which counts as revenue but must NOT be credited
to the engine. Seeing both on the dashboard is the point.

Usage:
    python scripts/simulate_webhooks.py --count 30
    python scripts/simulate_webhooks.py --count 30 --capture-rate 0.4 --self-pay-rate 0.15
    python scripts/simulate_webhooks.py --count 30 --no-captures     # phase 1 only
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_settings, reveal  # noqa: E402

SAMPLE_FAILURES = [
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "insufficient_funds", "error_source": "customer", "error_step": "payment_authorization", "method": "card", "bank": "HDFC"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "invalid_otp", "error_source": "customer", "error_step": "payment_authentication", "method": "card", "bank": "SBI"},
    {"error_code": "GATEWAY_ERROR", "error_reason": "issuer_down", "error_source": "gateway", "error_step": "payment_authorization", "method": "netbanking", "bank": "PNB"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "payment_cancelled", "error_source": "customer", "error_step": "payment_authentication", "method": "upi", "bank": "ICICI"},
    {"error_code": "GATEWAY_ERROR", "error_reason": "bank_technical_error", "error_source": "gateway", "error_step": "payment_authorization", "method": "card", "bank": "Axis"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "card_expired", "error_source": "customer", "error_step": "payment_initiation", "method": "card", "bank": "HDFC"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "payment_risk_check_failed", "error_source": "razorpay", "error_step": "payment_authorization", "method": "card", "bank": "ICICI"},
    {"error_code": "SERVER_ERROR", "error_reason": "timeout", "error_source": "razorpay", "error_step": "payment_capture", "method": "upi", "bank": "SBI"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "upi_collect_timeout", "error_source": "customer", "error_step": "payment_authorization", "method": "upi", "bank": "Kotak"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "card_limit_exceeded", "error_source": "customer", "error_step": "payment_authorization", "method": "card", "bank": "HDFC"},
]

# Realistic Indian ticket sizes in paise, rather than ₹100, ₹200, ₹300 —
# the amount ceiling and the ₹ figures on the dashboard only mean something
# against amounts a real merchant would see.
AMOUNTS = [49900, 79900, 129900, 250000, 499000, 599000, 1250000, 2499000]


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(client: httpx.Client, host: str, payload: dict[str, Any], secret: str) -> int:
    body = json.dumps(payload).encode()
    resp = client.post(
        f"{host}/webhooks/razorpay",
        content=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": sign(body, secret)},
    )
    return resp.status_code


def failed_payload(failure: dict[str, Any], idx: int, amount: int) -> dict[str, Any]:
    return {
        "entity": "event", "account_id": "acc_test123", "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": {
            "id": f"pay_test_{uuid.uuid4().hex[:12]}",
            "entity": "payment", "amount": amount, "currency": "INR", "status": "failed",
            "order_id": f"order_test_{idx:04d}",
            "method": failure["method"], "bank": failure.get("bank"),
            "email": f"customer{idx % 40}@example.com", "contact": "+919876543210",
            "error_code": failure["error_code"],
            "error_description": f"Test: {failure['error_reason']}",
            "error_source": failure["error_source"], "error_step": failure["error_step"],
            "error_reason": failure["error_reason"], "created_at": int(time.time()),
        }}},
        "created_at": int(time.time()),
    }


def captured_payload(
    amount: int, *, idempotency_key: str | None = None, order_id: str | None = None
) -> dict[str, Any]:
    """
    A capture for a payment we have never seen — which is the whole difficulty.

    With `idempotency_key`, this is our link being paid: Razorpay copies a link's
    notes onto the payment, so the breadcrumb the executor wrote comes back and
    the money is credited to the attempt that earned it. With only `order_id`,
    the customer paid the original order themselves — real revenue, credited to
    the case but explicitly NOT to us.
    """
    entity: dict[str, Any] = {
        "id": f"pay_test_{uuid.uuid4().hex[:12]}",   # deliberately a NEW id
        "entity": "payment", "amount": amount, "currency": "INR",
        "status": "captured", "method": "upi", "created_at": int(time.time()),
        "notes": {"retry_idempotency_key": idempotency_key} if idempotency_key else {},
    }
    if order_id:
        entity["order_id"] = order_id
    return {
        "entity": "event", "event": "payment.captured", "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
        "created_at": int(time.time()),
    }


def open_attempts() -> list[tuple[str, str | None, int]]:
    """(idempotency_key, order_id, amount) for cases still open — the recoverable set."""
    import sqlalchemy as sa

    engine = sa.create_engine(get_settings().database_url_sync)
    with engine.connect() as conn:
        rows = conn.execute(sa.text("""
            SELECT ra.idempotency_key, pf.order_id, rc.amount_at_risk
            FROM retry_attempts ra
            JOIN recovery_cases rc ON ra.recovery_case_id = rc.id
            LEFT JOIN payment_failures pf ON ra.payment_failure_id = pf.id
            WHERE rc.state = 'open' AND ra.action_type <> 'abandon'
            ORDER BY ra.created_at DESC
        """)).all()
    engine.dispose()
    return [(r[0], r[1], int(r[2])) for r in rows]


def summarise() -> None:
    """Read back what the engine actually did. This is the payoff."""
    import sqlalchemy as sa

    from dashboard.theme import compact_inr

    engine = sa.create_engine(get_settings().database_url_sync)
    with engine.connect() as conn:
        s = conn.execute(sa.text("""
            SELECT (SELECT COUNT(*) FROM payment_failures),
                   (SELECT COUNT(*) FROM recovery_cases),
                   (SELECT COUNT(*) FROM recovery_cases WHERE state='recovered'),
                   (SELECT COUNT(*) FROM retry_attempts),
                   (SELECT COUNT(*) FROM retry_attempts WHERE result='scheduled'),
                   (SELECT COALESCE(SUM(amount_at_risk),0) FROM recovery_cases),
                   (SELECT COALESCE(SUM(amount_recovered),0) FROM recovery_cases),
                   (SELECT COALESCE(SUM(amount_recovered),0) FROM recovery_cases
                      WHERE recovered_via_attempt_id IS NOT NULL)
        """)).one()
        actions = conn.execute(sa.text(
            "SELECT action_type, COUNT(*) FROM retry_attempts GROUP BY 1 ORDER BY 2 DESC")).all()
    engine.dispose()

    fails, cases, recovered, attempts, scheduled, at_risk, rec_paise, att_paise = s
    print("\n" + "─" * 58)
    print("  What the engine did")
    print("─" * 58)
    print(f"  Failures ingested      {fails:>10,}")
    print(f"  Cases opened           {cases:>10,}")
    print(f"  Cases recovered        {recovered:>10,}")
    print(f"  Attempts made          {attempts:>10,}   ({scheduled:,} scheduled for later)")
    print()
    print(f"  Money at risk          {compact_inr(at_risk):>10}")
    print(f"  Money recovered        {compact_inr(rec_paise):>10}")
    print(f"  ...attributed to us    {compact_inr(att_paise):>10}   <- the honest number")
    print(f"  ...customer self-paid  {compact_inr(rec_paise - att_paise):>10}")
    print()
    print("  Agent decisions:")
    for action, n in actions:
        print(f"    {action:<18} {n:>6,}")
    print("─" * 58)


def main() -> None:
    p = argparse.ArgumentParser(description="Drive the recovery engine with synthetic traffic")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--host", type=str, default="http://localhost:8000")
    # Defaults to the configured secret. A hardcoded default meant every webhook
    # was rejected at signature check with a 401 and the run looked like the
    # server was down.
    p.add_argument("--secret", type=str, default=None)
    p.add_argument("--capture-rate", type=float, default=0.35,
                   help="Share of open cases whose link gets paid (attributed to us).")
    p.add_argument("--self-pay-rate", type=float, default=0.10,
                   help="Share that the customer pays directly (revenue, but not ours).")
    p.add_argument("--no-captures", action="store_true", help="Phase 1 only.")
    # 15s was fine against localhost and far too tight for a free-tier
    # host that sleeps: the first request pays a cold start of tens of
    # seconds, and --host is a documented remote workflow.
    p.add_argument("--timeout", type=float, default=90.0,
                   help="Per-request timeout, seconds. Generous by default "
                        "because a sleeping free-tier host wakes slowly.")
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
    secret = args.secret or reveal(get_settings().razorpay_webhook_secret)
    if not secret:
        print("RAZORPAY_WEBHOOK_SECRET is not set — every webhook would 401.")
        raise SystemExit(1)

    print(f"Phase 1 — {args.count} payment.failed → {args.host}/webhooks/razorpay\n")
    ok = 0
    with httpx.Client(timeout=args.timeout) as client:
        for i in range(args.count):
            failure = SAMPLE_FAILURES[i % len(SAMPLE_FAILURES)]
            code = post(client, args.host, failed_payload(failure, i, random.choice(AMOUNTS)),
                        secret)
            ok += code == 200
            print(f"  [{i+1:3d}/{args.count}] {failure['error_reason']:<28} "
                  f"{'ok' if code == 200 else f'HTTP {code}'}")
            time.sleep(0.15)
    print(f"\n  {ok}/{args.count} accepted")

    if args.no_captures:
        summarise()
        return

    # The pipeline runs in a BackgroundTask after the 200, so the attempts do
    # not exist the instant the loop above ends.
    print("\n  waiting for the pipeline to settle...")
    time.sleep(4)

    candidates = open_attempts()
    if not candidates:
        print("  No open cases with attempts — nothing to capture.")
        summarise()
        return

    random.shuffle(candidates)
    n_att = int(len(candidates) * args.capture_rate)
    n_self = int(len(candidates) * args.self_pay_rate)
    print(f"\nPhase 2 — {n_att} captures on our links, {n_self} customer self-pays\n")

    with httpx.Client(timeout=args.timeout) as client:
        for key, _order, amount in candidates[:n_att]:
            code = post(client, args.host, captured_payload(amount, idempotency_key=key), secret)
            print(f"  attributed   {key:<44} {'ok' if code == 200 else f'HTTP {code}'}")
            time.sleep(0.15)
        for _key, order, amount in candidates[n_att:n_att + n_self]:
            if not order:
                continue
            code = post(client, args.host, captured_payload(amount, order_id=order), secret)
            print(f"  self-paid    {order:<44} {'ok' if code == 200 else f'HTTP {code}'}")
            time.sleep(0.15)

    time.sleep(3)
    summarise()


if __name__ == "__main__":
    main()
