"""
`retry_at` decisions that land inside the 23-7 IST blackout.

The guardrail validates the CURRENT hour at decision time, so a deferral
approved at 22:30 sails through and dies at the scheduler's fire-time
re-validation — after attach_attempt already spent an attempt slot. The clamp
moves such a decision to the window's edge before it is parked, so the slot
buys a retry that can actually fire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.guardrail.rules import IST, clamp_retry_at_out_of_blackout, is_in_blackout
from src.models import RetryAttempt, WebhookEvent


def _ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


# ── The pure clamp ───────────────────────────────────────────────────────


def test_a_time_inside_the_blackout_moves_to_its_edge() -> None:
    # 18:30 UTC == 00:00 IST, deep inside the window.
    inside = datetime(2026, 8, 25, 18, 30, tzinfo=UTC)
    clamped = clamp_retry_at_out_of_blackout(inside)
    local = _ist(clamped)
    assert not is_in_blackout(local.hour)
    # Forward-only: waiting longer than asked is compliant; earlier is not ours.
    assert clamped > inside


def test_a_time_outside_the_blackout_is_untouched() -> None:
    # 12:07 UTC == 17:37 IST.
    outside = datetime(2026, 8, 25, 12, 7, tzinfo=UTC)
    assert clamp_retry_at_out_of_blackout(outside) == outside


def test_the_window_edges_themselves_are_respected() -> None:
    # 17:31 UTC == 23:01 IST → just inside; 17:20 UTC == 22:50 IST → just outside.
    just_inside = clamp_retry_at_out_of_blackout(datetime(2026, 8, 25, 17, 31, tzinfo=UTC))
    assert just_inside != datetime(2026, 8, 25, 17, 31, tzinfo=UTC)

    just_outside = datetime(2026, 8, 25, 17, 20, tzinfo=UTC)
    assert clamp_retry_at_out_of_blackout(just_outside) == just_outside


def test_clamping_never_lands_on_the_boundary_minute() -> None:
    """+5min past the edge hour so fire-time rounding cannot re-enter the window."""
    inside = datetime(2026, 8, 25, 18, 30, tzinfo=UTC)
    local = _ist(clamp_retry_at_out_of_blackout(inside))
    assert (local.hour, local.minute) == (7, 5)


# ── The orchestrator applies it before parking the row ───────────────────


class _FixedAgent:
    def __init__(self, retry_at: datetime) -> None:
        self.fallback_count = 0
        self._retry_at = retry_at

    async def decide(self, context: Any) -> Any:
        from src.agent.actions import RetryAction

        return RetryAction(
            action="retry_at", retry_at=self._retry_at,
            reason="fixed test decision", confidence=0.9,
        )


async def test_a_deferral_into_the_blackout_is_parked_outside_it(
    db_sessionmaker: Any,
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """
    A deferral whose target lands inside 23-7 IST used to be parked as-is and
    rejected by the scheduler's re-validation — a spent attempt slot for a
    retry that could never fire. It must be parked outside the window.

    The gate itself is pinned (its own wall-clock behaviour is what makes an
    unpinned test flaky between IST day and night); this test is about where
    the row gets parked.
    """
    from sqlalchemy import select

    from src.guardrail.gate import GuardrailResult
    from src.orchestrator import PaymentRecoveryOrchestrator

    orch = PaymentRecoveryOrchestrator()
    due = datetime.now(UTC).astimezone(IST).replace(microsecond=0) + timedelta(minutes=30)
    agent = _FixedAgent(due)
    monkeypatch.setattr(orch, "_get_agent", lambda: agent)
    monkeypatch.setattr(
        orch._guardrail,
        "validate",
        lambda *a, **k: GuardrailResult(passed=True, rules_checked=1),
    )

    async def no_razorpay(**kwargs: Any) -> dict[str, Any]:
        return {"success": True}

    monkeypatch.setattr(orch._executor, "execute_retry", no_razorpay)

    async with db_sessionmaker() as session:
        event = WebhookEvent(
            razorpay_event_id="evt_clamp_1",
            event_type="payment.failed",
            payload=sample_webhook_payload,
        )
        session.add(event)
        await session.flush()
        await orch.process_payment_failure(event, session)

    async with db_sessionmaker() as reader:
        attempt = (
            await reader.execute(select(RetryAttempt))
        ).scalar_one()

    assert attempt.result == "scheduled"
    scheduled_local = _ist(attempt.scheduled_at)
    parked = scheduled_local.isoformat()
    assert not is_in_blackout(scheduled_local.hour), (
        f"parked at {parked} — inside the blackout it will be rejected at fire time"
    )


# ── The two halves of the window behave differently ──────────────────────
# Late-night inputs must roll to TOMORROW's edge; early-morning inputs land
# TODAY at 07:05. A clamp that got days=2 or dropped `wake` entirely passed
# every test above — these two pin both branches.


def test_late_night_input_rolls_to_tomorrows_edge() -> None:
    # 23:30 IST → 07:05 the NEXT day.
    inside = datetime(2026, 8, 25, 18, 0, tzinfo=UTC)  # 23:30 IST
    clamped = _ist(clamp_retry_at_out_of_blackout(inside))
    assert (clamped.hour, clamped.minute, clamped.day) == (7, 5, 26)


def test_small_hours_land_today_not_tomorrow() -> None:
    # 02:00 IST on the 26th (= 20:30 UTC on the 25th) → TODAY at 07:05.
    # A +2-day drift or a None crash fails here.
    inside = datetime(2026, 8, 25, 20, 30, tzinfo=UTC)  # 02:00 IST, Aug 26
    clamped = _ist(clamp_retry_at_out_of_blackout(inside))
    assert (clamped.hour, clamped.minute, clamped.day) == (7, 5, 26)
    assert clamped > _ist(inside), "forward-only, even within the same day"
