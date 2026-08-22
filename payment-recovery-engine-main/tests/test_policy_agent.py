"""
The LLM policy path — the layer that decides, and the layer most able to lie.

`decide()` swallows every LLM error and returns a heuristic action that is
indistinguishable from a real decision at the call site. That is deliberate (the
pipeline must never block on a provider outage) and it is exactly why
`fallback_count` exists: without checking it, a completely dead LLM still
produces a full, plausible results table, and the README reports an LLM row for
calls that never happened.

Every test here pins one of the two halves: the model's output really is
constrained to the fixed action space, and a degraded call really is countable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from src.agent.actions import FailureContext, RetryAction
from src.agent.policy_agent import PolicyAgent

now = datetime.now(UTC)


def _context(**overrides: Any) -> FailureContext:
    defaults: dict[str, Any] = dict(
        payment_id="pay_agent_001",
        failure_class="insufficient_funds",
        error_code="BAD_REQUEST_ERROR",
        amount=50000,
        method="card",
        bank="HDFC",
        customer_id="agent@example.com",
        failed_at=now,
        current_time=now,
        hour_of_day=14,
        day_of_week=2,
        is_retryable=True,
    )
    defaults.update(overrides)
    return FailureContext(**defaults)


@pytest.fixture
def agent(monkeypatch: Any) -> PolicyAgent:
    """A PolicyAgent whose SDK client is never constructed for real."""
    monkeypatch.setattr(
        "src.agent.policy_agent.PolicyAgent.__init__",
        lambda self, provider=None: None,
    )
    a = PolicyAgent()
    a._provider = "openai"
    a._model = "test-model"
    a._client = None
    a.call_count = 0
    a.fallback_count = 0
    a.last_error_status = None
    return a


def _reply(agent: PolicyAgent, monkeypatch: Any, text: str | list[str]) -> list[str]:
    """Pin _call_llm to a scripted reply (or a sequence, for the retry path)."""
    replies = [text] if isinstance(text, str) else list(text)
    asked: list[str] = []

    async def fake_call(prompt: str) -> str:
        asked.append(prompt)
        return replies[min(len(asked) - 1, len(replies) - 1)]

    monkeypatch.setattr(agent, "_call_llm", fake_call)
    return asked


# ── The happy path ───────────────────────────────────────────────────────


async def test_valid_json_becomes_a_constrained_action(
    agent: PolicyAgent, monkeypatch: Any
) -> None:
    _reply(agent, monkeypatch, json.dumps({
        "action": "switch_rail", "rail": "upi",
        "reason": "card issuer declining", "confidence": 0.8,
    }))
    action = await agent.decide(_context())
    assert isinstance(action, RetryAction)
    assert (action.action, action.rail) == ("switch_rail", "upi")
    assert agent.fallback_count == 0, "a good response must not count as a fallback"
    assert agent.call_count == 1


async def test_markdown_fenced_json_is_still_parsed(
    agent: PolicyAgent, monkeypatch: Any
) -> None:
    """Models fence JSON constantly; refusing it would fall back on good answers."""
    _reply(agent, monkeypatch,
           '```json\n{"action": "retry_now", "reason": "transient failure"}\n```')
    action = await agent.decide(_context())
    assert action.action == "retry_now"
    assert agent.fallback_count == 0


# ── Degradation is countable ─────────────────────────────────────────────


async def test_unparseable_output_retries_once_then_falls_back(
    agent: PolicyAgent, monkeypatch: Any
) -> None:
    asked = _reply(agent, monkeypatch, "I think you should probably retry this one!")
    action = await agent.decide(_context())

    assert len(asked) == 2, "the correction retry did not happen"
    assert "not valid JSON" in asked[1]
    assert agent.fallback_count == 1
    assert action.reason.startswith("Fallback:")


async def test_a_correction_that_parses_is_not_a_fallback(
    agent: PolicyAgent, monkeypatch: Any
) -> None:
    _reply(agent, monkeypatch, [
        "sorry, here you go:",
        json.dumps({"action": "nudge_customer", "reason": "balance likely low"}),
    ])
    action = await agent.decide(_context())
    assert action.action == "nudge_customer"
    assert agent.fallback_count == 0


async def test_an_action_outside_the_space_is_rejected(
    agent: PolicyAgent, monkeypatch: Any
) -> None:
    """The constraint that stops an LLM inventing a way to move money."""
    _reply(agent, monkeypatch, json.dumps(
        {"action": "refund_customer", "reason": "seems fair to me"}
    ))
    action = await agent.decide(_context())
    assert action.action in ("retry_now", "retry_at", "switch_rail", "nudge_customer", "abandon")
    assert agent.fallback_count == 1


async def test_a_raising_client_falls_back_rather_than_blocking(
    agent: PolicyAgent, monkeypatch: Any
) -> None:
    async def boom(prompt: str) -> str:
        raise TimeoutError("provider timed out")

    monkeypatch.setattr(agent, "_call_llm", boom)
    action = await agent.decide(_context())
    assert isinstance(action, RetryAction)
    assert agent.fallback_count == 1


async def test_an_auth_failure_is_surfaced_not_just_counted(
    agent: PolicyAgent, monkeypatch: Any
) -> None:
    """
    402 is what an out-of-credit OpenRouter key returns. decide() swallows it by
    design, so last_error_status is the only way a caller learns the provider is
    unusable rather than merely unlucky — and the eval harness uses it to drop
    the LLM row instead of reporting fallback numbers as LLM numbers.
    """
    class InsufficientCreditsError(Exception):
        status_code = 402

    async def boom(prompt: str) -> str:
        raise InsufficientCreditsError("Insufficient credits")

    monkeypatch.setattr(agent, "_call_llm", boom)
    await agent.decide(_context())
    assert agent.last_error_status == 402


# ── The fallback heuristic itself ────────────────────────────────────────


def test_fallback_never_retries_a_hard_decline() -> None:
    action = PolicyAgent._fallback_action(
        _context(failure_class="fraud_block", is_retryable=False), "provider down"
    )
    assert action.action == "abandon"


def test_fallback_retries_a_network_error_immediately() -> None:
    action = PolicyAgent._fallback_action(_context(failure_class="network_error"), "down")
    assert action.action == "retry_now"


def test_fallback_waits_out_bank_downtime() -> None:
    action = PolicyAgent._fallback_action(_context(failure_class="bank_downtime"), "down")
    assert action.action == "retry_at"
    assert action.retry_at is not None and action.retry_at > now


def test_fallback_on_an_unknown_class_abandons_rather_than_guesses() -> None:
    """Conservative by design: the money path fails closed."""
    action = PolicyAgent._fallback_action(_context(failure_class="something_new"), "down")
    assert action.action == "abandon"
