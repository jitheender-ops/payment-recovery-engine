"""
LLM tail classification — off by default, only ever consulted on UNKNOWN,
never allowed to raise or leak past a fallback to the mapper's own result.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.classifier.llm_tail import classify_tail
from src.classifier.taxonomy import FailureClass


def _mock_llm(monkeypatch: Any, reply: str | Exception) -> None:
    async def fake(prompt: str) -> str:
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr("src.classifier.llm_tail._call_llm", fake)


async def test_a_confident_mapper_result_is_never_second_guessed(
    monkeypatch: Any, settings_override: Any
) -> None:
    """
    Even with the feature on and an LLM ready to answer, a non-UNKNOWN
    mapper result must short-circuit before any LLM call happens.
    """
    called = False

    async def fake(prompt: str) -> str:
        nonlocal called
        called = True
        return '{"failure_class": "fraud_block"}'

    monkeypatch.setattr("src.classifier.llm_tail._call_llm", fake)
    settings_override(classifier_llm_tail_enabled=True)

    result = await classify_tail(
        (FailureClass.INSUFFICIENT_FUNDS, True), "BAD_REQUEST_ERROR",
    )
    assert result == (FailureClass.INSUFFICIENT_FUNDS, True)
    assert called is False


async def test_disabled_by_default_leaves_unknown_as_unknown(
    monkeypatch: Any, settings_override: Any
) -> None:
    called = False

    async def fake(prompt: str) -> str:
        nonlocal called
        called = True
        return '{"failure_class": "network_error"}'

    monkeypatch.setattr("src.classifier.llm_tail._call_llm", fake)
    settings_override(classifier_llm_tail_enabled=False)

    result = await classify_tail((FailureClass.UNKNOWN, False), "SOME_NEW_CODE")
    assert result == (FailureClass.UNKNOWN, False)
    assert called is False


async def test_enabled_and_unknown_consults_the_llm(
    monkeypatch: Any, settings_override: Any
) -> None:
    _mock_llm(monkeypatch, '{"failure_class": "network_error"}')
    settings_override(classifier_llm_tail_enabled=True)

    fc, retryable = await classify_tail(
        (FailureClass.UNKNOWN, False), "SOME_NEW_CODE", error_description="timed out mid-call",
    )
    assert fc is FailureClass.NETWORK_ERROR
    assert retryable == FailureClass.NETWORK_ERROR.is_retryable


async def test_llm_exception_falls_back_to_unknown(
    monkeypatch: Any, settings_override: Any
) -> None:
    _mock_llm(monkeypatch, RuntimeError("provider down"))
    settings_override(classifier_llm_tail_enabled=True)

    result = await classify_tail((FailureClass.UNKNOWN, False), "SOME_NEW_CODE")
    assert result == (FailureClass.UNKNOWN, False)


async def test_unparseable_reply_falls_back_to_unknown(
    monkeypatch: Any, settings_override: Any
) -> None:
    _mock_llm(monkeypatch, "not json at all")
    settings_override(classifier_llm_tail_enabled=True)

    result = await classify_tail((FailureClass.UNKNOWN, False), "SOME_NEW_CODE")
    assert result == (FailureClass.UNKNOWN, False)


async def test_invalid_class_name_falls_back_to_unknown(
    monkeypatch: Any, settings_override: Any
) -> None:
    """The LLM must be constrained to real enum values, never freeform."""
    _mock_llm(monkeypatch, '{"failure_class": "definitely_not_a_real_class"}')
    settings_override(classifier_llm_tail_enabled=True)

    result = await classify_tail((FailureClass.UNKNOWN, False), "SOME_NEW_CODE")
    assert result == (FailureClass.UNKNOWN, False)


async def test_tolerates_a_code_fence_wrapped_reply(
    monkeypatch: Any, settings_override: Any
) -> None:
    _mock_llm(monkeypatch, '```json\n{"failure_class": "issuer_decline"}\n```')
    settings_override(classifier_llm_tail_enabled=True)

    fc, _ = await classify_tail((FailureClass.UNKNOWN, False), "SOME_NEW_CODE")
    assert fc is FailureClass.ISSUER_DECLINE


@pytest.fixture
def settings_override(monkeypatch: Any) -> Any:
    """
    Toggle Settings fields for the duration of one test. Patches the name as
    imported into src.classifier.llm_tail specifically — that module did
    `from src.config import get_settings`, which bound its own reference to
    the original function at import time, so patching src.config.get_settings
    would not reach it.
    """
    from src import config

    def _set(**overrides: Any) -> None:
        current = config.get_settings()
        patched = current.model_copy(update=overrides)
        monkeypatch.setattr("src.classifier.llm_tail.get_settings", lambda: patched)

    return _set
