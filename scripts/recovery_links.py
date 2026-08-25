"""
Print openable /recover/<token> URLs for the cases already in your database.

Built for the local polish loop. Without it, seeing the page means finding a
case id by hand, minting a token in a REPL, and pasting it together — which is
enough friction that the states nobody looks at are the ones that stay ugly.

It groups by the state the PAGE would render, not by the database column, and
it derives that by calling routes._view_state directly. A second copy of that
logic here would drift, and then this tool would confidently show you a URL
labelled "payable" that renders something else.

Usage:
    python scripts/recovery_links.py            # one URL per page state
    python scripts/recovery_links.py --all      # every case
    python scripts/recovery_links.py --state confirming
    python scripts/recovery_links.py --seed-states   # make every state viewable
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlalchemy as sa  # noqa: E402

from src import recovery_link  # noqa: E402
from src.config import get_settings, reveal  # noqa: E402
from src.customer.explain import explain  # noqa: E402
from src.customer.routes import _view_state  # noqa: E402
from src.models import PaymentFailure, RecoveryCase, RetryAttempt  # noqa: E402

# What each state is for, so it is obvious which ones still need looking at.
BLURB = {
    "payable": "the main flow — amount, reassurance, one pay button",
    "confirming": "a payment is in flight; the pay button MUST be absent",
    "unknown": "in flight too long; admits it cannot confirm",
    "recovered": "already paid; no pay button",
    "not_retryable": "hard decline — retrying would hit the same wall",
    "stopped": "engine gave up; still explains where the money is",
    "opted_out": "customer asked us to stop",
}


# ── Making the unreachable states reachable ──────────────────────────────


def _seed_missing_states(url: str) -> int:
    """
    Insert one preview case for each state the database cannot currently show.

    The four that go missing on ordinary traffic — confirming, unknown,
    not_retryable, opted_out — are exactly the ones where the page REFUSES to
    take money. They are the screens most worth looking at and the least likely
    to appear on their own, which is a bad combination for polishing.

    Everything written here is marked pay_preview_* / preview@example.invalid so
    it is trivial to find and delete:

        DELETE FROM payment_failures WHERE payment_id LIKE 'pay_preview_%';
        DELETE FROM recovery_cases   WHERE subject_ref LIKE 'pay_preview_%';
    """
    import uuid
    from datetime import UTC, datetime, timedelta

    # (page state, case state, failure class, attempt result, attempt age)
    wanted = [
        ("confirming", "open", "insufficient_funds", "pending", 2),
        ("unknown", "open", "insufficient_funds", "pending", 90),
        ("not_retryable", "open", "fraud_block", None, 0),
        ("opted_out", "opted_out", "insufficient_funds", None, 0),
    ]
    engine = sa.create_engine(url)
    made = 0
    with engine.begin() as conn:
        for name, case_state, fclass, result, age_min in wanted:
            pid = f"pay_preview_{name}"
            if conn.execute(
                sa.select(sa.func.count()).select_from(RecoveryCase)
                .where(RecoveryCase.subject_ref == pid)
            ).scalar_one():
                continue

            now = datetime.now(UTC)
            fid, cid = uuid.uuid4(), uuid.uuid4()
            conn.execute(sa.insert(PaymentFailure).values(
                id=fid, payment_id=pid, order_id=f"order_preview_{name}",
                amount=249900, currency="INR", method="card", bank="HDFC",
                error_code="BAD_REQUEST_ERROR", failure_class=fclass,
                is_retryable=fclass != "fraud_block",
                customer_email="preview@example.invalid",
                webhook_event_id=uuid.uuid4(), failed_at=now, created_at=now,
            ))
            conn.execute(sa.insert(RecoveryCase).values(
                id=cid, risk_type="payment_failure", subject_ref=pid,
                customer_id="preview@example.invalid", amount_at_risk=249900,
                currency="INR", amount_recovered=0, state=case_state,
                attempts_used=1, max_attempts=3, escalation_level=0,
                opened_at=now, updated_at=now,
            ))
            if result:
                conn.execute(sa.insert(RetryAttempt).values(
                    id=uuid.uuid4(), payment_failure_id=fid, payment_id=pid,
                    idempotency_key=f"preview_{name}", attempt_number=1,
                    recovery_case_id=cid, action_type="retry_now",
                    agent_type="xgboost", guardrail_passed=True, result=result,
                    executed_at=now - timedelta(minutes=age_min), created_at=now,
                ))
            made += 1
    engine.dispose()
    return made


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true", help="every case, not one per state")
    p.add_argument("--state", help="only this page state")
    p.add_argument("--limit", type=int, default=400)
    p.add_argument("--seed-states", action="store_true",
                   help="insert one preview case per state that has none")
    args = p.parse_args()

    settings = get_settings()
    if not reveal(settings.recovery_link_secret):
        print("RECOVERY_LINK_SECRET is unset — the page is off and every token\n"
              "would be rejected. Set it in .env, then re-run.")
        return 1
    if not settings.public_base_url:
        print("PUBLIC_BASE_URL is unset, so links cannot be built. Set it in .env.")
        return 1

    if args.seed_states:
        seeded = _seed_missing_states(settings.database_url_sync)
        print(f"Inserted {seeded} preview case(s).\n" if seeded
              else "Every state already has a case.\n")

    engine = sa.create_engine(settings.database_url_sync)
    with engine.connect() as conn:
        cases = conn.execute(
            sa.select(RecoveryCase).order_by(RecoveryCase.opened_at.desc()).limit(args.limit)
        ).all()
        failures = {
            r.payment_id: r
            for r in conn.execute(sa.select(PaymentFailure)).all()
        }
        attempts: dict[object, object] = {}
        for r in conn.execute(
            sa.select(RetryAttempt).order_by(RetryAttempt.created_at.desc())
        ).all():
            attempts.setdefault(r.recovery_case_id, r)   # newest wins
    engine.dispose()

    by_state: dict[str, list[str]] = defaultdict(list)
    for case in cases:
        failure = failures.get(case.subject_ref)
        detail = explain(failure.failure_class if failure else None)
        state = _view_state(case, attempts.get(case.id), detail.retryable)
        url = recovery_link.url_for(case.id)
        if url:
            by_state[state].append(url)

    if not by_state:
        print("No recovery cases yet. Start the API and run:\n"
              "    python scripts/simulate_webhooks.py --count 20")
        return 0

    wanted = [args.state] if args.state else list(BLURB)
    shown = 0
    for state in wanted:
        urls = by_state.get(state, [])
        print(f"\n{state.upper()}  ({len(urls)} case{'s' if len(urls) != 1 else ''})")
        print(f"  {BLURB.get(state, '')}")
        if not urls:
            print("  — none in the database yet")
            continue
        for url in (urls if (args.all or args.state) else urls[:1]):
            print(f"  {url}")
            shown += 1

    missing = [s for s in BLURB if not by_state.get(s)]
    if missing and not args.state:
        print(f"\nNot reachable from current data: {', '.join(missing)}")
        print("Those states need cases in that condition before you can see them.")
    print(f"\n{shown} link(s). They expire in "
          f"{settings.consent_window_hours}h.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
