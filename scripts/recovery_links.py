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
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlalchemy as sa  # noqa: E402

from src import recovery_link  # noqa: E402
from src.config import get_settings, reveal  # noqa: E402
from src.customer.explain import explain  # noqa: E402
from src.customer.routes import _view_state  # noqa: E402
from src.models import PaymentFailure, RecoveryCase, RetryAttempt  # noqa: E402

# The seeded buyer account. A stable ref so re-running is idempotent —
# a second run finds this account and adds nothing.
_DEMO_ACCOUNT_REF = "ref:demo-nandini-traders"

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


def _seed_b2b_account(url: str) -> int:
    """
    One buyer account with several open invoices, so the STATEMENT page has
    something to be.

    Neither simulate_webhooks.py nor run_risk_batch.py creates this: the
    first only knows the payment rail, and the second opens invoice cases
    without linking them to an ArAccount. But /statement/{token} is an
    account-level surface — with no account carrying two or more open
    invoices, the page it renders is an empty one, which demonstrates
    nothing. Returns the number of invoices inserted.
    """
    from src.receivables.models import ArAccount

    engine = sa.create_engine(url)
    made = 0
    with engine.begin() as conn:
        existing = conn.execute(
            sa.select(ArAccount.id).where(ArAccount.account_ref == _DEMO_ACCOUNT_REF)
        ).scalar_one_or_none()
        if existing is not None:
            engine.dispose()
            return 0

        now = datetime.now(UTC)
        account_id = uuid.uuid4()
        conn.execute(sa.insert(ArAccount).values(
            id=account_id, account_ref=_DEMO_ACCOUNT_REF,
            display_name="Nandini Traders Pvt Ltd", created_at=now,
        ))
        # Deliberately varied: one plain, one part-paid, one recently due.
        # A statement where every row looks the same shows less than one
        # where the totals have to be read carefully.
        invoices = [
            ("INV-2041", 1_250_000, 0, 27),
            ("INV-2044", 480_000, 150_000, 18),
            ("INV-2050", 96_500, 0, 9),
            ("INV-2053", 310_000, 0, 3),
        ]
        for ref, at_risk, recovered, days_overdue in invoices:
            conn.execute(sa.insert(RecoveryCase).values(
                id=uuid.uuid4(), risk_type="invoice_overdue", subject_ref=ref,
                customer_id="email:ap@nanditraders.example", account_id=account_id,
                amount_at_risk=at_risk, currency="INR", amount_recovered=recovered,
                state="open", attempts_used=1, max_attempts=4, escalation_level=0,
                due_at=now - timedelta(days=days_overdue),
                opened_at=now - timedelta(days=days_overdue),
                updated_at=now,
            ))
            made += 1
    engine.dispose()
    return made


def _as_uuid(value: object) -> uuid.UUID | None:
    """
    Raw SQL hands back a UUID on Postgres and a plain string on SQLite. Every
    caller here wants the object, so normalise once instead of at each site.
    """
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


# The seven capabilities, and the one case each is visible on.
#
# WHY THIS EXISTS: everything below was already built and rendering, and it
# was still effectively invisible — this script grouped links by PAGE STATE
# (payable / confirming / recovered / ...) and printed one per state, so all
# seven features were buried inside "PAYABLE (30 cases)" behind a single
# link that was almost always a plain card decline. A state is what the page
# is doing; a feature is what you came to look at, and they are not the same
# index.
#
# Each entry is (label, what to look at, SQL picking the case that shows it).
# The SQL is the honest part: it selects a case that genuinely exhibits the
# feature, so a missing link means the demo has no such case rather than the
# feature being broken.
_FEATURES: list[tuple[str, str, str]] = [
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
        "Hinglish voice recovery",
        "'We called you on {date}' in the timeline; add ?lang=hi for the "
        "Hindi page",
        "SELECT rc.id FROM recovery_cases rc "
        "JOIN voice_call_queue v ON v.recovery_case_id = rc.id "
        "WHERE v.state='done' LIMIT 1",
    ),
]


def _feature_urls(url: str) -> list[tuple[str, str, str | None]]:
    """(label, what to look at, URL or None) — one openable page per feature."""
    engine = sa.create_engine(url)
    out: list[tuple[str, str, str | None]] = []
    with engine.connect() as conn:
        for label, blurb, query in _FEATURES:
            try:
                case_id = conn.execute(sa.text(query)).scalar()
            except Exception:
                # A table this demo has not created yet (voice_call_queue on
                # an older database). Report the feature as unavailable
                # rather than taking the whole listing down with it.
                case_id = None
            resolved = _as_uuid(case_id)
            link = recovery_link.url_for(resolved) if resolved else None
            out.append((label, blurb, link))
    engine.dispose()
    return out


def _seed_voice_call(url: str) -> int:
    """
    One COMPLETED voice call, so the "We called you on {date}" trace has
    something to render.

    Nothing else creates one. VOICE_CHASER_ENABLED is off by default (a call
    is the highest-friction touch the engine can make and needs the
    merchant's own DoT/TCPB registration), and even with it on the row only
    reaches state "done" once a real telephony leg claims it and reports
    back. So the honest voice trace on the recovery page was invisible in
    the demo — not because it is broken, but because the demo had no call to
    show. Returns the number of calls inserted.
    """
    from src.models import VoiceCallQueue

    engine = sa.create_engine(url)
    made = 0
    with engine.begin() as conn:
        if conn.execute(
            sa.select(sa.func.count()).select_from(VoiceCallQueue)
        ).scalar_one():
            engine.dispose()
            return 0

        # A case that already has an executed attempt: a voice touch is an
        # attempt-row event, so inventing one with no attempt behind it
        # would be a shape the engine never produces.
        # Prefer a case the feature index is not already using for something
        # else, so the seven links land on seven different pages. The voice
        # chaser queues after a successful nudge on ANY risk type, so this is
        # a presentation choice, not a behavioural claim.
        row = conn.execute(sa.text("""
            SELECT rc.id, rc.risk_type, rc.amount_at_risk, ra.id
            FROM recovery_cases rc
            JOIN retry_attempts ra ON ra.recovery_case_id = rc.id
            WHERE rc.state = 'open' AND ra.executed_at IS NOT NULL
            ORDER BY CASE rc.risk_type
                       WHEN 'invoice_overdue' THEN 0
                       WHEN 'payment_failure' THEN 1
                       ELSE 2
                     END
            LIMIT 1
        """)).first()
        if row is None:
            engine.dispose()
            return 0

        case_id, risk_type, amount, attempt_id = row
        case_id, attempt_id = _as_uuid(case_id), _as_uuid(attempt_id)
        if case_id is None or attempt_id is None:
            engine.dispose()
            return 0
        conn.execute(sa.insert(VoiceCallQueue).values(
            id=uuid.uuid4(),
            recovery_case_id=case_id,
            retry_attempt_id=attempt_id,
            customer_contact="+919876543210",
            risk_type=risk_type,
            amount_paise=amount,
            state="done",
            claimed_at=datetime.now(UTC) - timedelta(hours=6),
            claimed_by="demo-telephony-leg",
            result="promise_captured",
            created_at=datetime.now(UTC) - timedelta(hours=6),
        ))
        made = 1
    engine.dispose()
    return made


def _statement_urls(url: str) -> list[tuple[str, str, int]]:
    """(display name, statement URL, open invoice count) per AR account that
    actually has open invoices — the accounts whose statement page is worth
    opening."""
    from src.receivables.models import ArAccount

    engine = sa.create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                ArAccount.id,
                ArAccount.display_name,
                ArAccount.account_ref,
                sa.func.count(RecoveryCase.id),
            )
            .join(RecoveryCase, RecoveryCase.account_id == ArAccount.id)
            .where(
                RecoveryCase.risk_type == "invoice_overdue",
                RecoveryCase.state == "open",
            )
            .group_by(ArAccount.id, ArAccount.display_name, ArAccount.account_ref)
        ).all()
    engine.dispose()
    out = []
    for account_id, name, ref, count in rows:
        link = recovery_link.url_for_account(account_id)
        if link:
            out.append((name or ref, link, int(count)))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true", help="every case, not one per state")
    p.add_argument("--state", help="only this page state")
    p.add_argument("--limit", type=int, default=400)
    p.add_argument("--seed-states", action="store_true",
                   help="insert preview data for any surface that has none "
                        "(page states, and a B2B account for the statement page)")
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
        # The statement page is an ACCOUNT-level surface, not a page state,
        # so it needs its own seed — no other script links invoice cases to
        # an ArAccount, and without one the page renders empty.
        seeded += _seed_b2b_account(settings.database_url_sync)
        seeded += _seed_voice_call(settings.database_url_sync)
        print(f"Inserted {seeded} preview row(s).\n" if seeded
              else "Every surface already has data.\n")

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
        # Derive the state exactly the way routes.py does, or the labels here
        # drift from the page: risk cases get their risk-type explanation and
        # per-type consent window anchored at opened_at, and only the payment
        # rail ever consults payment_failures (a merchant-chosen subject_ref
        # colliding with a real payment id must not drag that failure in).
        if case.risk_type == "payment_failure":
            failure = failures.get(case.subject_ref)
            detail = explain(failure.failure_class if failure else None)
            anchor = failure.failed_at if failure else None
            window_hours = None
        else:
            from src.chasers.policy import policy_for

            policy = policy_for(case.risk_type)
            detail = explain(None, risk_type=case.risk_type)
            anchor = case.opened_at
            window_hours = policy.consent_window_hours if policy else None
        state = _view_state(
            case, attempts.get(case.id), detail.retryable,
            failed_at=anchor, window_hours=window_hours,
        )
        url = recovery_link.url_for(case.id)
        if url:
            by_state[state].append(url)

    if not by_state:
        print("No recovery cases yet. Start the API and run:\n"
              "    python scripts/simulate_webhooks.py --count 20")
        return 0

    shown = 0

    # ── By feature, first ────────────────────────────────────────────────
    # Ahead of the by-state listing on purpose: "show me the mandate retry
    # sequencer" is the question people actually arrive with, and grouping
    # only by page state made every feature unfindable behind one PAYABLE
    # link.
    if not args.state:
        print("\n╔══ WHAT TO LOOK AT ═══════════════════════════════════════")
        print("║  one page per capability, each on a case that really shows it")
        print("╚══════════════════════════════════════════════════════════")
        for label, blurb, link in _feature_urls(settings.database_url_sync):
            print(f"\n{label}")
            print(f"  {blurb}")
            if link:
                print(f"  {link}")
                shown += 1
            else:
                print("  — no case in the database exhibits this yet "
                      "(try --seed-states, or seed more traffic)")
        print("\n" + "─" * 60)
        print("Everything below is the same pages indexed by PAGE STATE — "
              "what the\npage is doing, rather than what you came to look at.")

    wanted = [args.state] if args.state else list(BLURB)
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

    # ── The account statement: a sibling surface, not a page state ──────
    # /statement/{token} shows one buyer every open invoice they have. It is
    # reached by an ACCOUNT-scoped token, so it can never appear in the
    # per-state listing above however many cases exist.
    if not args.state:
        statements = _statement_urls(settings.database_url_sync)
        print(f"\nSTATEMENT  ({len(statements)} account"
              f"{'s' if len(statements) != 1 else ''})")
        print("  every open invoice for one B2B buyer, each row linking to "
              "its own page")
        if statements:
            for name, link, count in statements:
                print(f"  {link}")
                print(f"      {name} — {count} open invoice"
                      f"{'s' if count != 1 else ''}")
                shown += 1
        else:
            print("  — no AR account has open invoices yet "
                  "(try --seed-states)")

        # ── The merchant console: password-gated, not token-gated ───────
        base = settings.public_base_url.rstrip("/")
        print("\nCONSOLE  (sign in with DASHBOARD_PASSWORD)")
        print("  the merchant's side — the same data, for whoever is chasing it")
        for path, blurb in (
            ("/console/login", "sign in first; everything below redirects here"),
            ("/console/live", "ladder, promises, disputes, aging"),
            ("/console/pipeline", "where money leaves, and what the gateway blamed"),
            ("/console/routing", "which bank on which rail — the switch_rail evidence"),
            ("/console/cases", "every case, filterable by state"),
            ("/console/ops", "is the machinery running"),
        ):
            print(f"  {base}{path}")
            print(f"      {blurb}")

    missing = [s for s in BLURB if not by_state.get(s)]
    if missing and not args.state:
        print(f"\nNot reachable from current data: {', '.join(missing)}")
        print("Those states need cases in that condition before you can see them.")
    print(f"\n{shown} link(s). They expire in "
          f"{settings.consent_window_hours}h.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
