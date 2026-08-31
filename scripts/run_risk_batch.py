"""
Drive the four chasers with synthetic merchant risk traffic, end to end.

The payment rail has simulate_webhooks.py; this is the same demonstration for
the risk types with NO inbound gateway event — an abandoned cart, a halted
subscription, an overdue invoice and a failed mandate debit. Those only exist
in the merchant's own systems, so the merchant POSTs them to /risks:

    1. phase 1   POST /risks (HMAC-signed) opens cases and, for the immediate
                 types, runs the first chase step in the background
    2. phase 2   payment.captured on the links we minted — the money comes
                 back with a brand-new payment id and is joined home through
                 the notes.retry_idempotency_key breadcrumb

Two honest caveats the output shows rather than hides:

  * checkout_abandonment and mandate_failure defer their first touch
    (first_action_hours 1h / 24h in src/chasers/policy.py). Their cases open
    with next_action_at in the future and are chased by the scheduler's sweep,
    not by ingestion — a fresh run therefore has no links to capture for them
    until the sweep has fired.
  * A risk case has no order in payment_failures, so the "customer self-paid"
    path (order_ref matching) cannot resolve one. Every rupee a risk case
    recovers is attributed to a link we sent, or not recovered at all.

Usage:
    python scripts/run_risk_batch.py --count 24
    python scripts/run_risk_batch.py --count 24 --capture-rate 0.5
    python scripts/run_risk_batch.py --count 24 --no-captures   # phase 1 only
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_settings, reveal  # noqa: E402

# Per-type shape: reference prefix, realistic paise ticket sizes, and a meta
# snippet that exercises the prompt's sanitised merchant-meta block.
RISK_SHAPES: dict[str, dict[str, Any]] = {
    "checkout_abandonment": {
        "prefix": "cart",
        "amounts": [49900, 79900, 129900, 249900],
        "meta": {"cart_items": "2 books", "stage": "payment_step"},
    },
    "subscription_failure": {
        "prefix": "sub",
        "amounts": [19900, 49900, 99900, 29900],
        "meta": {"plan": "monthly", "cycle": 4},
    },
    "invoice_overdue": {
        "prefix": "inv",
        "amounts": [250000, 499000, 1250000, 2499000],
        "meta": {"invoice_number": "INV-2026-0142", "days_overdue": 6},
    },
    "mandate_failure": {
        "prefix": "mandate",
        "amounts": [149900, 349900, 599000],
        "meta": {"emandate_token": "emand_9f2c", "debit_reason": "insufficient_funds"},
    },
}


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# A handful of buyer companies for the B2B events to belong to. Without an
# account_ref every invoice opens unlinked, and the whole receivables layer —
# consolidation, the escalation ladder, aging — has nothing to group. The batch
# used to send neither this nor a due date, so those features could not be
# demonstrated at all: the ladder showed every rung empty and aging reported
# zero invoices while ten overdue cases sat in the table.
B2B_ACCOUNTS = [
    ("ACME-DISTRIBUTION", "Acme Distribution Pvt Ltd"),
    ("NORTHWIND-TRADERS", "Northwind Traders"),
    ("SUNRISE-RETAIL", "Sunrise Retail"),
]


def risk_payload(risk_type: str, idx: int, amount: int) -> dict[str, Any]:
    shape = RISK_SHAPES[risk_type]
    payload: dict[str, Any] = {
        "event_id": f"evt_{risk_type}_{idx:04d}_{uuid.uuid4().hex[:8]}",
        "risk_type": risk_type,
        "reference_id": f"{shape['prefix']}_{idx:04d}_{uuid.uuid4().hex[:6]}",
        "amount_paise": amount,
        "currency": "INR",
        "customer_id": f"cust_{idx % 40}",
        "customer_email": f"customer{idx % 40}@example.com",
        "customer_contact": "+919876543210",
        "meta": dict(shape["meta"]),
    }

    if risk_type == "invoice_overdue":
        # Deliberately few accounts and many invoices: consolidation is the
        # point of this layer, and it only shows up when one buyer owes
        # several. Spread of days-overdue so the ladder reaches its later
        # rungs and the aging buckets are not all one column.
        account_ref, _ = B2B_ACCOUNTS[idx % len(B2B_ACCOUNTS)]
        overdue_days = [2, 9, 16, 24, 33][idx % 5]
        payload["account_ref"] = account_ref
        payload["due_at"] = (
            datetime.now(UTC) - timedelta(days=overdue_days)
        ).isoformat()
        payload["meta"] = {
            **payload["meta"],
            "days_overdue": overdue_days,
            "invoice_number": f"INV-2026-{idx:04d}",
        }

    return payload


def captured_payload(amount: int, idempotency_key: str) -> dict[str, Any]:
    """A capture for a payment we have never seen — paying a link mints a new
    id. The link's notes come back on the payment, so the breadcrumb the
    executor wrote is what joins the money home."""
    return {
        "entity": "event", "event": "payment.captured", "contains": ["payment"],
        "payload": {"payment": {"entity": {
            "id": f"pay_test_{uuid.uuid4().hex[:12]}",
            "entity": "payment", "amount": amount, "currency": "INR",
            "status": "captured", "method": "upi", "created_at": int(time.time()),
            "notes": {"retry_idempotency_key": idempotency_key},
        }}},
        "created_at": int(time.time()),
    }


def open_case_attempts() -> list[tuple[str, int, str]]:
    """(idempotency_key, amount_at_risk, risk_type) for chase attempts on open
    cases — the recoverable set. Case attempts carry no payment_failure_id."""
    import sqlalchemy as sa

    engine = sa.create_engine(get_settings().database_url_sync)
    with engine.connect() as conn:
        rows = conn.execute(sa.text("""
            SELECT ra.idempotency_key, rc.amount_at_risk, rc.risk_type
            FROM retry_attempts ra
            JOIN recovery_cases rc ON ra.recovery_case_id = rc.id
            WHERE rc.state = 'open'
              AND ra.payment_failure_id IS NULL
              AND ra.action_type <> 'abandon'
            ORDER BY ra.created_at DESC
        """)).all()
    engine.dispose()
    return [(r[0], int(r[1]), r[2]) for r in rows]


def summarise() -> None:
    """Read back what the chasers actually did, per risk type."""
    import sqlalchemy as sa

    from dashboard.theme import compact_inr

    engine = sa.create_engine(get_settings().database_url_sync)
    with engine.connect() as conn:
        per_type = conn.execute(sa.text("""
            SELECT risk_type,
                   COUNT(*),
                   SUM(CASE WHEN state = 'recovered' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN state = 'open' THEN 1 ELSE 0 END),
                   COALESCE(SUM(amount_at_risk), 0),
                   COALESCE(SUM(amount_recovered), 0),
                   COALESCE(SUM(CASE WHEN recovered_via_attempt_id IS NOT NULL
                                     THEN amount_recovered ELSE 0 END), 0)
            FROM recovery_cases
            WHERE risk_type IN ('checkout_abandonment', 'subscription_failure',
                                'invoice_overdue', 'mandate_failure')
            GROUP BY risk_type
            ORDER BY risk_type
        """)).all()
        attempts = conn.execute(sa.text("""
            SELECT rc.risk_type, COUNT(*)
            FROM retry_attempts ra
            JOIN recovery_cases rc ON ra.recovery_case_id = rc.id
            WHERE ra.payment_failure_id IS NULL
            GROUP BY rc.risk_type
            ORDER BY rc.risk_type
        """)).all()
    engine.dispose()

    attempt_by_type: dict[str, int] = {row[0]: int(row[1]) for row in attempts}
    print("\n" + "─" * 74)
    print("  What the chasers did")
    print("─" * 74)
    total_cases = total_recovered = total_at_risk = total_rec = 0
    for risk_type, cases, recovered, open_n, at_risk, rec, att in per_type:
        n_attempts = attempt_by_type.get(risk_type, 0)
        total_cases += cases
        total_recovered += recovered
        total_at_risk += at_risk
        total_rec += rec
        print(f"  {risk_type:<24} cases {cases:>4}  recovered {recovered:>4}  "
              f"open {open_n:>4}  chases {n_attempts:>4}")
        print(f"  {'':<24} at risk {compact_inr(at_risk):>10}  "
              f"recovered {compact_inr(rec):>10}  "
              f"attributed {compact_inr(att):>10}")
    print("─" * 74)
    print(f"  {'TOTAL':<24} cases {total_cases:>4}  recovered {total_recovered:>4}  "
          f"chases {sum(attempt_by_type.values()):>4}")
    if total_at_risk:
        print(f"  Recovery rate          {total_rec / total_at_risk * 100:>9.1f}%  "
              f"({compact_inr(total_rec)} of {compact_inr(total_at_risk)})")
    print("─" * 74)




def post_promises(
    host: str, secret: str, refs: list[tuple[str, str, int]], timeout: float
) -> int:
    """
    Record a promise against some open cases — the third thing a real deployment
    accumulates and the batch never produced.

    Without these the promise tracker, the payment plans panel and the "case is
    deliberately quiet" half of the ladder are all empty, so three shipped
    features look unbuilt. A promise is also the cheapest recovery there is, so
    a demo with none of them understates the engine.

    Dates straddle now deliberately: some already due (they will break on the
    expiry sweep and hand the case back), some ahead (the case stays quiet).
    """
    sent = 0
    with httpx.Client(timeout=timeout) as client:
        for n, (risk_type, reference_id, amount) in enumerate(refs):
            # Future dates only. A promise to pay in the PAST is refused with
            # a 400, correctly — you cannot commit to a date that has gone.
            # Broken promises arrive the honest way: these come due, the
            # expiry sweep finds no money, and the case goes back to the
            # chaser. Backdating to manufacture them just fails validation.
            days = [1, 2, 4, 6, 9, 13][n % 6]
            payload = {
                "amount_paise": amount,
                "due_at": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
                "channel": ["voice", "payment_link", "merchant"][n % 3],
                "confidence": "explicit",
                "language": "hinglish" if n % 3 == 0 else "en",
            }
            body = json.dumps(payload).encode()
            resp = client.post(
                f"{host}/risks/{risk_type}/{reference_id}/promise",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Risk-Signature": sign(body, secret),
                },
            )
            ok = resp.status_code == 200
            sent += ok
            print(f"  promise  {reference_id:<28} "
                  f"{'ok' if ok else f'HTTP {resp.status_code}'}")
            time.sleep(0.12)
    return sent


def main() -> None:
    p = argparse.ArgumentParser(
        description="Drive the four chasers with synthetic merchant risk traffic"
    )
    p.add_argument("--count", type=int, default=20,
                   help="Total risk events, spread evenly across the four types.")
    p.add_argument("--host", type=str, default="http://localhost:8000")
    p.add_argument("--secret", type=str, default=None,
                   help="RISK_WEBHOOK_SECRET override; defaults to the configured one.")
    p.add_argument("--capture-rate", type=float, default=0.35,
                   help="Share of open case links that get paid.")
    p.add_argument("--no-captures", action="store_true", help="Phase 1 only.")
    p.add_argument("--promise-rate", type=float, default=0.35,
                   help="Share of open cases a promise is recorded against. "
                        "0 skips the phase.")
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
    secret = args.secret or reveal(get_settings().risk_webhook_secret)
    if not secret:
        print("RISK_WEBHOOK_SECRET is not set — /risks fails closed, so every "
              "event would 401.")
        raise SystemExit(1)

    types = list(RISK_SHAPES)
    per_type = max(args.count // len(types), 1)
    total = per_type * len(types)
    print(f"Phase 1 — {total} risk events ({per_type} per type) → {args.host}/risks\n")
    ok = 0
    # What phase 1 actually got a 200 for. The promise phase below works from
    # THIS list rather than querying a database: --host may point at a remote
    # deployment, and the local database would then hand back references that
    # exist only here. summarise() has that flaw and says so; this does not.
    accepted: list[tuple[str, str, int]] = []
    with httpx.Client(timeout=args.timeout) as client:
        i = 0
        for risk_type in types:
            shape = RISK_SHAPES[risk_type]
            for _ in range(per_type):
                payload = risk_payload(risk_type, i, random.choice(shape["amounts"]))
                body = json.dumps(payload).encode()
                resp = client.post(
                    f"{args.host}/risks",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Risk-Signature": sign(body, secret),
                    },
                )
                if resp.status_code == 200:
                    ok += 1
                    accepted.append(
                        (risk_type, payload["reference_id"], payload["amount_paise"])
                    )
                print(f"  [{i + 1:3d}/{total}] {risk_type:<24} "
                      f"{payload['reference_id']:<28} "
                      f"{'ok' if resp.status_code == 200 else f'HTTP {resp.status_code}'}")
                i += 1
                time.sleep(0.15)
    print(f"\n  {ok}/{total} accepted")

    # ── Promises ──────────────────────────────────────────────────────
    # Before captures on purpose: a promise made and then kept is the sequence
    # the tracker exists to measure, and doing it the other way round would
    # record promises against cases that had already closed.
    if args.promise_rate > 0:
        print("\n  waiting for cases to open before recording promises...")
        time.sleep(6)
        take = int(len(accepted) * args.promise_rate)
        if take:
            print(f"\nPhase 1b — {take} promises on open cases\n")
            sent = post_promises(args.host, secret, accepted[:take], args.timeout)
            print(f"\n  {sent}/{take} promises recorded")
        else:
            print("\n  no open cases yet to promise against")

    if args.no_captures:
        summarise()
        return

    # Cases open and first chases run in a BackgroundTask after the 200.
    print("\n  waiting for the pipeline to settle...")
    time.sleep(4)

    candidates = open_case_attempts()
    if not candidates:
        print("  No open case attempts yet — deferred types (cart, mandate) wait "
              "for the scheduler's chase sweep before they have links to pay.")
        summarise()
        return

    random.shuffle(candidates)
    n_att = int(len(candidates) * args.capture_rate)
    print(f"\nPhase 2 — {n_att} captures on chase links\n")

    webhook_secret = reveal(get_settings().razorpay_webhook_secret)
    if not webhook_secret:
        print("  RAZORPAY_WEBHOOK_SECRET is not set — skipping captures.")
        summarise()
        return

    with httpx.Client(timeout=args.timeout) as client:
        for key, amount, risk_type in candidates[:n_att]:
            body = json.dumps(captured_payload(amount, key)).encode()
            resp = client.post(
                f"{args.host}/webhooks/razorpay",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": sign(body, webhook_secret),
                },
            )
            print(f"  attributed   {risk_type:<24} {key:<44} "
                  f"{'ok' if resp.status_code == 200 else f'HTTP {resp.status_code}'}")
            time.sleep(0.15)

    time.sleep(3)
    summarise()


if __name__ == "__main__":
    main()
