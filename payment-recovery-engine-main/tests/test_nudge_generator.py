"""
Customer nudge generation — scoped LLM output that goes to a real person.

Two properties matter more than the wording. It must never raise or block: a
nudge is generated mid-pipeline, and an exception here would abort a recovery
attempt over a cosmetic string. And it must never exceed the SMS ceiling, since
an over-long message is silently split and billed twice, or truncated mid-word
at whatever the carrier decides.

The PII test is the third: prompts leave our process, so what goes into them is
a decision, not an accident.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.messaging.nudge_generator import NudgeGenerator

ARGS: dict[str, Any] = {
    "failure_class": "insufficient_funds",
    "amount": 250000,
    "method": "card",
    "next_step": "Please try again with a different card.",
}


@pytest.fixture
def generator(monkeypatch: Any) -> NudgeGenerator:
    """A generator with no LLM behind it — the template path."""
    gen = NudgeGenerator()
    monkeypatch.setattr(gen, "_get_client", lambda: None)
    return gen


def _with_llm(gen: NudgeGenerator, monkeypatch: Any, reply: str | Exception) -> list[str]:
    """Pin the LLM call to a scripted reply or an exception."""
    prompts: list[str] = []

    async def fake(client: Any, failure_class: str, amount_display: str, method: str,
                   next_step: str, customer_name: str | None, merchant_name: str) -> str:
        prompts.append(
            f"{failure_class}|{amount_display}|{method}|{next_step}|{customer_name}|{merchant_name}"
        )
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(gen, "_get_client", lambda: object())
    monkeypatch.setattr(gen, "_generate_llm", fake)
    return prompts


# ── Always returns something ─────────────────────────────────────────────


async def test_the_template_path_produces_a_message(generator: NudgeGenerator) -> None:
    message = await generator.generate(**ARGS)
    assert message and isinstance(message, str)
    assert "2,500.00" in message, "the amount is the one fact the customer needs"


async def test_a_raising_llm_falls_back_to_the_template(
    generator: NudgeGenerator, monkeypatch: Any
) -> None:
    """A cosmetic string must never abort a recovery attempt."""
    _with_llm(generator, monkeypatch, RuntimeError("provider down"))
    message = await generator.generate(**ARGS)
    assert message
    assert "2,500.00" in message


async def test_an_empty_llm_reply_falls_back(
    generator: NudgeGenerator, monkeypatch: Any
) -> None:
    _with_llm(generator, monkeypatch, "")
    assert await generator.generate(**ARGS)


async def test_an_absurdly_long_llm_reply_falls_back(
    generator: NudgeGenerator, monkeypatch: Any
) -> None:
    """Over the buffer means the model ignored the brief — use the template."""
    _with_llm(generator, monkeypatch, "x" * 500)
    message = await generator.generate(**ARGS)
    assert "x" * 200 not in message


async def test_a_good_llm_reply_is_used(
    generator: NudgeGenerator, monkeypatch: Any
) -> None:
    _with_llm(generator, monkeypatch, "Your ₹2,500 payment didn't go through. Try another card?")
    message = await generator.generate(**ARGS)
    assert message.startswith("Your ₹2,500 payment")


# ── The SMS ceiling ──────────────────────────────────────────────────────


async def test_every_path_respects_160_characters(
    generator: NudgeGenerator, monkeypatch: Any
) -> None:
    """Over 160 is silently split and billed twice, or truncated by the carrier."""
    for reply in ("y" * 190, "short and fine"):
        _with_llm(generator, monkeypatch, reply)
        assert len(await generator.generate(**ARGS)) <= 160

    assert len(await NudgeGenerator().generate(**{**ARGS, "customer_name": "A" * 300})) <= 160


async def test_every_failure_class_has_a_template(generator: NudgeGenerator) -> None:
    """An unknown class must not produce an empty SMS."""
    for fc in (
        "insufficient_funds", "3ds_dropoff", "bank_downtime", "network_error",
        "upi_collect_timeout", "issuer_decline", "card_limit_exceeded",
        "payment_timeout", "something_we_have_never_seen",
    ):
        message = await generator.generate(**{**ARGS, "failure_class": fc})
        assert message.strip(), f"empty nudge for {fc}"
        assert len(message) <= 160


# ── What reaches the provider ────────────────────────────────────────────


async def test_the_prompt_carries_no_email_or_phone(
    generator: NudgeGenerator, monkeypatch: Any
) -> None:
    """
    Prompts leave our process. The generator is handed a display name at most —
    if an email or contact ever starts arriving here, it is going to a third
    party on every nudge.
    """
    prompts = _with_llm(generator, monkeypatch, "fine")
    await generator.generate(**{**ARGS, "customer_name": "Priya"})
    assert len(prompts) == 1
    prompt = prompts[0]
    assert "@" not in prompt, "an email address reached the prompt"
    assert "+91" not in prompt, "a phone number reached the prompt"
    # No run of digits long enough to be a contact number. The amount (2,500.00)
    # and the class name are the only numerics that belong here.
    import re
    assert not re.search(r"\d{7,}", prompt.replace(",", "")), "a long digit run reached the prompt"
    assert "Priya" in prompt, "the display name is what the nudge is allowed to use"
