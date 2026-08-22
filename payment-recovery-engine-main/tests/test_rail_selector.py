"""
Rail selection: the switch_rail target must never be the rail that just failed.

Nothing else in the pipeline enforces this. The schema check
(`validate_action_schema`) only requires `rail` to be non-null and one of the
four literals, and "the same rail that just declined" satisfies both — so an
agent returning `{"action": "switch_rail", "rail": "card"}` on a card failure
used to get a real Payment Link on card, consuming one of the three permitted
attempts on a retry that cannot succeed.

The last test is the one that matters: this module sat unimported for its whole
life, so a unit test of the pure functions would have passed just as happily
while the pipeline ignored them.
"""

from __future__ import annotations

from typing import Any, get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agent.actions import FailureContext, PaymentRail, RetryAction
from src.classifier.taxonomy import FailureClass
from src.executor.rail_selector import (
    ALL_RAILS,
    resolve_target_rail,
    select_alternative_rail,
)
from src.guardrail.gate import GuardrailResult
from src.models import RetryAttempt, WebhookEvent
from src.orchestrator import PaymentRecoveryOrchestrator

# Every failure class the classifier can produce, plus the two shapes that
# reach the selector when classification told it nothing.
ALL_FAILURE_CLASSES = [fc.value for fc in FailureClass] + ["", "not_a_real_class"]


def test_all_rails_matches_the_action_schema() -> None:
    """
    ALL_RAILS is derived, not restated.

    A rail here that PaymentRail doesn't accept would be selected and then
    rejected downstream as a schema violation — a switch that silently never
    happens.
    """
    assert ALL_RAILS == list(get_args(PaymentRail))


def test_never_returns_the_rail_that_just_failed() -> None:
    """The whole point, over every rail × every failure class."""
    for rail in ALL_RAILS:
        for failure_class in ALL_FAILURE_CLASSES:
            chosen = select_alternative_rail(rail, failure_class)
            assert chosen != rail, f"{failure_class} on {rail} returned {rail}"
            assert chosen in ALL_RAILS


def test_documented_heuristics() -> None:
    """The docstring's promises, so a rewrite can't quietly drop one."""
    assert select_alternative_rail("card", "3ds_dropoff") == "upi"
    assert select_alternative_rail("card", "issuer_decline") == "upi"
    assert select_alternative_rail("card", "card_limit_exceeded") == "upi"
    assert select_alternative_rail("upi", "upi_collect_timeout") == "card"
    assert select_alternative_rail("netbanking", "bank_downtime") == "upi"


def test_the_agents_own_choice_is_kept() -> None:
    """The heuristic defers — the agent has context it doesn't."""
    assert resolve_target_rail("card", "netbanking", "issuer_decline") == "netbanking"
    assert resolve_target_rail("card", "wallet", "3ds_dropoff") == "wallet"


def test_a_switch_onto_the_failed_rail_is_replaced() -> None:
    assert resolve_target_rail("card", "card", "3ds_dropoff") == "upi"
    assert resolve_target_rail("upi", "upi", "upi_collect_timeout") == "card"


def test_a_missing_rail_is_filled_in() -> None:
    """
    Schema validation rejects a null rail outright. Supplying one first turns
    that rejection — an abandoned recoverable payment — into a working switch.
    """
    assert resolve_target_rail("card", None, "issuer_decline") == "upi"


# ── The wiring ───────────────────────────────────────────────────────────
class _SwitchToCardAgent:
    """An agent that switches onto the rail that just failed. The bad case."""

    def __init__(self) -> None:
        self.fallback_count = 0

    async def decide(self, context: FailureContext) -> RetryAction:
        return RetryAction(
            action="switch_rail",
            rail="card",  # sample_webhook_payload failed on card
            reason="deliberately switching onto the rail that just failed",
            confidence=0.9,
        )


async def test_orchestrator_overrides_a_switch_onto_the_failed_rail(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sample_webhook_payload: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """End to end: the rail that reaches Razorpay and the ledger is not `card`."""
    orch = PaymentRecoveryOrchestrator()
    monkeypatch.setattr(orch, "_get_agent", lambda: _SwitchToCardAgent())
    # Pinned for the same reason as the write-ahead tests: the real gate reads
    # the wall clock for the IST retry blackout.
    monkeypatch.setattr(
        orch._guardrail,
        "validate",
        lambda *a, **k: GuardrailResult(passed=True, rules_checked=1, rules_failed=0),
    )
    # Nudge generation runs for switch_rail and would reach an LLM provider.
    monkeypatch.setattr(orch._nudge_gen, "generate", _fixed_nudge)

    seen: dict[str, Any] = {}

    async def spy_execute(**kwargs: Any) -> dict[str, Any]:
        seen["target_rail"] = kwargs["target_rail"]
        return {"success": True, "payment_link_id": "plink_test_rail"}

    monkeypatch.setattr(orch._executor, "execute_retry", spy_execute)

    async with db_sessionmaker() as session:
        event = WebhookEvent(
            razorpay_event_id="evt_rail_override_1",
            event_type="payment.failed",
            payload=sample_webhook_payload,
        )
        session.add(event)
        await session.flush()
        await orch.process_payment_failure(event, session)

    assert seen["target_rail"] != "card", "executed a switch onto the rail that just failed"
    assert seen["target_rail"] == "upi"

    async with db_sessionmaker() as reader:
        rows = await reader.execute(
            select(RetryAttempt).where(RetryAttempt.payment_id == "pay_test_abc123")
        )
        attempts = list(rows.scalars().all())

    # The ledger has to agree with what was executed, or the audit trail lies.
    assert len(attempts) == 1
    assert attempts[0].target_rail == "upi"


async def _fixed_nudge(**kwargs: Any) -> str:
    return "Please try again using UPI."
