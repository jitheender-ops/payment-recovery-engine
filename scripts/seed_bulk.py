"""
Bulk demo dataset — thousands of cases carrying REAL decisions.

Why this exists rather than `simulate_webhooks.py --count 2500`: that script
POSTs one webhook per case over HTTP and runs the whole pipeline on each,
which is slow at this size and trips the page rate limiter. This writes rows
directly.

WHAT IT DOES NOT SHORTCUT. Every case is routed through the real
`ClassifierMapper`, the real `XGBoostBaseline`, and the real `GuardrailGate`
before anything is written. The failure class, the agent's action, its
reasoning and confidence, and the guardrail's verdict and rejection reasons
are all genuine outputs of the code under test — only the persistence is
bulk. A batch whose decisions were invented would prove nothing, and the
whole point of the demo is a measured recovery figure.

Decline reasons are drawn from Razorpay's own vocabulary — the documented
list plus the forcing test cards (see docs/decline-taxonomy.md), including
the three whose mapping was wrong until 2026-09-01. Cases the guardrail
BLOCKS and cases that exhaust their budget are generated on purpose: a demo
that only shows successes cannot show that the engine knows when to stop.

Usage:
    python scripts/seed_bulk.py --count 2500
    python scripts/seed_bulk.py --count 2500 --recovered-rate 0.34
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)

from src.agent.actions import FailureContext  # noqa: E402
from src.agent.xgboost_baseline import XGBoostBaseline  # noqa: E402
from src.classifier.mapper import ClassifierMapper  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.formatting import IST  # noqa: E402
from src.guardrail.gate import GuardrailGate  # noqa: E402
from src.models import CaseEvent, PaymentFailure, RecoveryCase, RetryAttempt  # noqa: E402

# Razorpay's own strings, with the error_code/source/step their category
# implies. Weighted so the mix looks like a real book rather than a uniform
# draw across eighteen equally-likely causes.
DECLINES: list[tuple[str, str, str, str, int]] = [
    # (error_reason, error_code, error_source, error_step, weight)
    ("insufficient_fund", "BAD_REQUEST_ERROR", "customer", "payment_authorization", 18),
    ("authentication_failed", "GATEWAY_ERROR", "customer", "payment_authentication", 14),
    ("card_declined", "BAD_REQUEST_ERROR", "bank", "payment_authorization", 12),
    ("payment_timed_out", "BAD_REQUEST_ERROR", "customer", "payment_authorization", 9),
    ("payment_collect_request_expired", "BAD_REQUEST_ERROR", "customer",
     "payment_authorization", 8),
    ("gateway_technical_error", "GATEWAY_ERROR", "gateway", "payment_authorization", 7),
    ("payment_declined", "BAD_REQUEST_ERROR", "bank", "payment_authorization", 6),
    ("bank_technical_error", "GATEWAY_ERROR", "gateway", "payment_authorization", 6),
    ("card_not_enrolled", "BAD_REQUEST_ERROR", "customer", "payment_authentication", 5),
    ("payment_cancelled", "BAD_REQUEST_ERROR", "customer", "payment_authentication", 5),
    ("card_number_invalid", "BAD_REQUEST_ERROR", "customer", "payment_authorization", 4),
    ("card_disabled_for_online_payments", "BAD_REQUEST_ERROR", "customer",
     "payment_authorization", 3),
    ("payment_risk_check_failed", "BAD_REQUEST_ERROR", "razorpay", "payment_authorization", 3),
    ("server_error", "SERVER_ERROR", "razorpay", "payment_capture", 2),
    ("invalid_amount", "BAD_REQUEST_ERROR", "business", "payment_initiation", 1),
]

# Realistic Indian ticket sizes in paise. The amount ceiling and every ₹
# figure on the console only mean something against amounts a merchant sees.
AMOUNTS = [49900, 79900, 129900, 199900, 250000, 349900, 499000, 599000,
           899000, 1250000, 1899000, 2499000]
METHODS = [("card", 62), ("upi", 26), ("netbanking", 9), ("wallet", 3)]
BANKS = ["HDFC", "ICICI", "SBI", "Axis", "Kotak", "PNB", "IndusInd", "Yes Bank"]

# Relative payment volume per IST hour, 00..23. Shaped like Indian
# e-commerce: a trough overnight, a morning climb, a lunch bump and an
# evening peak. Used so the guardrail's 23:00-07:00 blackout rejects a
# realistic slice rather than a third of the book.
IST_HOUR_WEIGHTS = [
    3, 2, 1, 1, 1, 2, 4, 9, 18, 30, 42, 50,
    54, 48, 44, 46, 52, 62, 74, 82, 78, 60, 34, 12,
]


def _weighted(rows: list[tuple[Any, ...]], rng: random.Random) -> tuple[Any, ...]:
    return rng.choices(rows, weights=[r[-1] for r in rows], k=1)[0]


async def seed(count: int, recovered_rate: float, seed_value: int) -> dict[str, int]:
    rng = random.Random(seed_value)
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    mapper = ClassifierMapper()
    agent = XGBoostBaseline()
    gate = GuardrailGate()
    now = datetime.now(UTC)
    tally = {
        "cases": 0, "recovered": 0, "blocked": 0, "exhausted": 0, "open": 0,
        "recovered_paise": 0, "at_risk_paise": 0,
    }

    try:
        async with sm() as session:
            for i in range(count):
                reason, code, source, step, _ = _weighted(DECLINES, rng)
                method = _weighted([(m, w) for m, w in METHODS], rng)[0]
                amount = rng.choice(AMOUNTS)
                bank = rng.choice(BANKS)
                pid = f"pay_bulk_{uuid.uuid4().hex[:12]}"
                # Spread over three weeks so aging, funnels and time-series
                # panels have a distribution instead of one spike — and drawn
                # against a realistic Indian shopping-hour curve rather than
                # uniformly across the clock. That is not cosmetic: the
                # guardrail blackout is 23:00-07:00 IST, so a uniform draw
                # puts a third of all traffic inside it and the demo reports
                # the engine refusing half of everything. Real volume is not
                # flat at 3am.
                #
                # And inside the CONSENT WINDOW. Backdating three weeks
                # produced decisions the engine would never make: the
                # consent-window rule refuses a case older than the window,
                # and the schema rule refuses a retry_at that is already in
                # the past — both correct, both artefacts of replaying
                # history rather than product behaviour. The engine can only
                # act within the window, so that is the book it has.
                ist_hour = rng.choices(range(24), weights=IST_HOUR_WEIGHTS, k=1)[0]
                hours_ago = rng.uniform(0.5, settings.consent_window_hours - 6)
                day = now - timedelta(hours=hours_ago)
                failed_at = (
                    day.astimezone(IST)
                    .replace(hour=ist_hour, minute=rng.randint(0, 59),
                             second=rng.randint(0, 59), microsecond=0)
                    .astimezone(UTC)
                )
                if failed_at > now:
                    failed_at -= timedelta(days=1)

                # ── the real decision path ───────────────────────────────
                failure_class, retryable = mapper.classify(
                    code, f"Test: {reason}", source, step, reason
                )
                context = FailureContext(
                    payment_id=pid,
                    order_id=f"order_bulk_{i:06d}",
                    failure_class=failure_class.value,
                    error_code=code,
                    error_description=f"Test: {reason}",
                    error_source=source,
                    error_reason=reason,
                    amount=amount,
                    method=method,
                    bank=bank,
                    customer_id=f"bulk{i % 900}@example.invalid",
                    failed_at=failed_at,
                    # NOW, not the failure instant. This models the
                    # scheduler sweep, which is what actually decides: it
                    # runs over the open book and picks up each case at the
                    # current moment. Deciding "as of" a historical instant
                    # made the agent schedule a retry_at that had already
                    # elapsed, which the schema rule then rejected — a
                    # rejection the real engine would never produce, because
                    # the real engine was never asked the question then.
                    # failed_at stays historical, so the consent-window rule
                    # still measures real age.
                    current_time=now,
                    # Real features, not constants: the agent's heuristic and
                    # the guardrail's blackout rule both read the clock, so a
                    # fixed hour would make every decision identical.
                    #
                    # IST, not UTC. check_time_of_day_blackout() takes an hour
                    # and treats it as IST — passing failed_at.hour (UTC) made
                    # 22% of the book look blacked out when the real figure is
                    # ~3%, because a UTC 02:00 is a perfectly ordinary IST
                    # 07:30. The half-hour offset is exactly why this bites.
                    hour_of_day=ist_hour,
                    day_of_week=failed_at.astimezone(IST).weekday(),
                )
                action = agent.predict(context)
                idem = f"bulk_{pid}_0"
                verdict = gate.validate(action, context, idem, current_attempts=0)

                failure = PaymentFailure(
                    id=uuid.uuid4(), payment_id=pid,
                    order_id=context.order_id, amount=amount, currency="INR",
                    method=method, bank=bank, error_code=code,
                    error_description=context.error_description,
                    error_source=source, error_step=step, error_reason=reason,
                    failure_class=failure_class.value, is_retryable=retryable,
                    webhook_event_id=uuid.uuid4(),
                    failed_at=failed_at, created_at=failed_at,
                )
                case = RecoveryCase(
                    id=uuid.uuid4(), risk_type="payment_failure",
                    subject_ref=pid, customer_id=context.customer_id,
                    amount_at_risk=amount, currency="INR", amount_recovered=0,
                    state="open", attempts_used=0, max_attempts=3,
                    escalation_level=0, opened_at=failed_at, updated_at=failed_at,
                )
                session.add_all([failure, case])
                events: list[CaseEvent] = [
                    CaseEvent(
                        recovery_case_id=case.id, event_type="case_opened",
                        actor="system", created_at=failed_at,
                        detail={"failure_class": failure_class.value,
                                "retryable": retryable, "error_reason": reason},
                    )
                ]

                if not verdict.passed:
                    # The engine refused. No attempt is spent, and the reasons
                    # are kept verbatim — they are what the case page shows.
                    case.state = "abandoned"
                    case.close_reason = "; ".join(verdict.rejection_reasons)[:400]
                    case.closed_at = failed_at
                    events.append(CaseEvent(
                        recovery_case_id=case.id, event_type="guardrail_blocked",
                        actor="guardrail", created_at=failed_at,
                        detail={"action": action.action,
                                "rejection_reasons": verdict.rejection_reasons,
                                "rules_checked": verdict.rules_checked,
                                "rules_failed": verdict.rules_failed},
                    ))
                    tally["blocked"] += 1
                else:
                    executed_at = failed_at + timedelta(minutes=rng.randint(5, 360))
                    recovered = (
                        action.action != "abandon" and rng.random() < recovered_rate
                    )
                    attempt = RetryAttempt(
                        id=uuid.uuid4(), payment_failure_id=failure.id,
                        payment_id=pid, idempotency_key=idem, attempt_number=1,
                        recovery_case_id=case.id, action_type=action.action,
                        target_rail=action.rail, agent_type="xgboost",
                        agent_reasoning=action.reason,
                        agent_confidence=action.confidence,
                        guardrail_passed=True,
                        result="success" if recovered else "failed",
                        executed_at=executed_at, created_at=failed_at,
                    )
                    session.add(attempt)
                    case.attempts_used = 1
                    events.append(CaseEvent(
                        recovery_case_id=case.id, event_type="agent_decided",
                        actor="xgboost", created_at=failed_at,
                        detail={"action": action.action, "rail": action.rail,
                                "confidence": action.confidence,
                                "reason": action.reason,
                                "guardrail_rules_checked": verdict.rules_checked},
                    ))
                    if recovered:
                        case.state = "recovered"
                        case.amount_recovered = amount
                        case.recovered_ref = f"pay_bulk_r_{uuid.uuid4().hex[:12]}"
                        case.recovered_at = executed_at
                        case.recovered_via_attempt_id = attempt.id
                        case.closed_at = executed_at
                        events.append(CaseEvent(
                            recovery_case_id=case.id, event_type="case_recovered",
                            actor="system", created_at=executed_at,
                            detail={"amount": amount, "attributed": True},
                        ))
                        tally["recovered"] += 1
                        tally["recovered_paise"] += amount
                    elif action.action == "abandon":
                        # Hard decline: the agent declined to act at all.
                        case.state = "abandoned"
                        case.close_reason = action.reason
                        case.closed_at = failed_at
                        tally["exhausted"] += 1
                    else:
                        # Budget spent on a slice, so `stop_reason()` has
                        # something real to report on the front page.
                        if rng.random() < 0.28:
                            case.attempts_used = case.max_attempts
                            case.state = "exhausted"
                            case.close_reason = (
                                f"attempt budget spent "
                                f"({case.max_attempts}/{case.max_attempts})"
                            )
                            case.closed_at = executed_at
                            tally["exhausted"] += 1
                        else:
                            tally["open"] += 1
                        tally["at_risk_paise"] += amount

                session.add_all(events)
                tally["cases"] += 1

                # Flush in chunks: 2,500 cases is ~10k rows, and holding all
                # of it in the identity map before one commit is where this
                # would otherwise fall over.
                if (i + 1) % 250 == 0:
                    await session.commit()
                    print(f"  ... {i + 1}/{count}", flush=True)
            await session.commit()
    finally:
        await engine.dispose()
    return tally


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--count", type=int, default=2500)
    p.add_argument("--recovered-rate", type=float, default=0.34,
                   help="share of permitted attempts that recover")
    p.add_argument("--seed", type=int, default=7, help="RNG seed, for a repeatable demo")
    args = p.parse_args()

    print(f"Seeding {args.count} cases through the real classifier, agent and guardrail…")
    t = asyncio.run(seed(args.count, args.recovered_rate, args.seed))
    print(f"""
  Cases            {t['cases']:,}
  Recovered        {t['recovered']:,}   ₹{t['recovered_paise'] / 100:,.0f}
  Blocked          {t['blocked']:,}   (guardrail refused — no attempt spent)
  Closed/exhausted {t['exhausted']:,}
  Still open       {t['open']:,}   ₹{t['at_risk_paise'] / 100:,.0f} at risk

  Synthetic dataset. Decisions are real; the money is not.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
