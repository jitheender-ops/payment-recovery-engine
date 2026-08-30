"""
One place to build an LLM client.

Three callers (policy_agent, nudge_generator, llm_tail) each hand-rolled the
same provider branch — and the copies had already drifted (llm_tail built a
fresh client per call; nudge_generator hard-coded a 3s timeout). This module
is the shared construction path; the CALL semantics stay with each caller
because they genuinely differ (retry policy, temperature, max_tokens).
"""

from __future__ import annotations

from typing import Any

from src.config import get_settings, reveal


def build_llm_client(timeout: float | None = None) -> Any | None:
    """
    The configured provider's async client, or None when not usable.

    None (rather than raising) is the right failure mode for every caller:
    each has its own fallback path (XGBoost, template, UNKNOWN) and must
    never block on a missing key. Raises ValueError only for a configured
    provider string that is neither known provider — a config error, not a
    runtime blip.

    timeout: per-caller override; None means the shared settings timeout.
    """
    settings = get_settings()
    if timeout is None:
        timeout = settings.llm_timeout_seconds

    provider = settings.llm_provider
    if provider == "anthropic":
        key = reveal(settings.anthropic_api_key)
        if not key:
            return None
        import anthropic

        return anthropic.AsyncAnthropic(api_key=key, timeout=timeout)
    if provider == "openai":
        key = reveal(settings.openai_api_key)
        if not key:
            return None
        import openai

        return openai.AsyncOpenAI(
            api_key=key,
            # Empty base_url means api.openai.com; set it to point the same
            # client at any OpenAI-compatible host (OpenRouter, Ollama, ...).
            base_url=settings.llm_base_url or None,
            timeout=timeout,
        )
    raise ValueError(f"Unsupported LLM provider: {provider}")
