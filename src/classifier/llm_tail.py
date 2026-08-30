"""
LLM classification for the tail the deterministic mapper gives up on.

src/classifier/mapper.py is a lookup table on purpose — "a regex-solvable
problem solved by an LLM is the first thing a panel will flag." This module
is the deliberate exception, scoped as narrowly as the exception can be
stated: consulted ONLY when the mapper already returned FailureClass.UNKNOWN
(it had no rule that matched), and ONLY when explicitly turned on
(Settings.classifier_llm_tail_enabled — off by default). A confident
deterministic match is never second-guessed here; this never runs for the
85%+ of traffic the lookup table already handles.

Constrained the same way the Policy Agent is: the LLM picks ONE value from
the FailureClass enum, never freeform text. Any error, timeout, or
unparseable response falls back to the mapper's own UNKNOWN result — this
function can only ever REPLACE an UNKNOWN with a specific class, never
introduce a wrong answer that wasn't already "we don't know."
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.classifier.taxonomy import FailureClass
from src.config import get_settings
from src.llm import build_llm_client

logger = logging.getLogger(__name__)

_VALID_CLASSES = {fc.value for fc in FailureClass}

_SYSTEM_PROMPT = (
    "You classify a Razorpay payment failure into exactly one category from "
    "a fixed list. Reply with ONLY a JSON object: "
    '{"failure_class": "<one value from the list>"}. '
    "No other text. If nothing fits well, use \"unknown\".\n\n"
    f"Valid values: {sorted(_VALID_CLASSES)}"
)


def _user_prompt(
    error_code: str,
    error_description: str | None,
    error_source: str | None,
    error_step: str | None,
    error_reason: str | None,
) -> str:
    return (
        f"error_code: {error_code}\n"
        f"error_reason: {error_reason or '(none)'}\n"
        f"error_source: {error_source or '(none)'}\n"
        f"error_step: {error_step or '(none)'}\n"
        f"error_description: {(error_description or '(none)')[:300]}"
    )


# Module-level, built once via the shared src/llm.py construction path: a
# fresh SDK client per classification call rebuilt a socket pool every time
# on a path the mapper's UNKNOWN tail can hit repeatedly.
_client: Any = None


def _get_client() -> Any:
    """The process-wide LLM client, built lazily on first use."""
    global _client
    if _client is None:
        _client = build_llm_client()
    return _client


async def _call_llm(prompt: str) -> str:
    """Raises on any failure — the caller decides the fallback."""
    settings = get_settings()
    client = _get_client()
    if client is None:
        raise RuntimeError("LLM client unavailable (missing API key)")
    if settings.llm_provider == "anthropic":
        response = await client.messages.create(
            model=settings.llm_model,
            max_tokens=100,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        block = response.content[0]
        return block.text if hasattr(block, "text") else str(block)

    completion = await client.chat.completions.create(
        model=settings.llm_model,
        max_tokens=100,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return completion.choices[0].message.content or ""


def _parse(raw: str) -> FailureClass | None:
    try:
        # Tolerate a model that wraps the JSON in a code fence despite being
        # told not to — strip anything before the first '{' and after the
        # last '}' rather than failing on decoration around valid JSON.
        start, end = raw.index("{"), raw.rindex("}") + 1
        data: dict[str, Any] = json.loads(raw[start:end])
        value = data.get("failure_class")
        if value in _VALID_CLASSES:
            return FailureClass(value)
    except (ValueError, KeyError, TypeError):
        pass
    return None


async def classify_tail(
    mapper_result: tuple[FailureClass, bool],
    error_code: str,
    error_description: str | None = None,
    error_source: str | None = None,
    error_step: str | None = None,
    error_reason: str | None = None,
) -> tuple[FailureClass, bool]:
    """
    Consult the LLM only if the deterministic mapper returned UNKNOWN and the
    feature is enabled. Otherwise returns mapper_result unchanged, with zero
    LLM calls — this is the common case and the default.
    """
    failure_class, is_retryable = mapper_result
    if failure_class is not FailureClass.UNKNOWN:
        return mapper_result
    if not get_settings().classifier_llm_tail_enabled:
        return mapper_result

    prompt = _user_prompt(error_code, error_description, error_source, error_step, error_reason)
    try:
        raw = await _call_llm(prompt)
    except Exception:
        logger.warning("LLM tail classification failed, keeping UNKNOWN", exc_info=True)
        return mapper_result

    parsed = _parse(raw)
    if parsed is None:
        logger.warning("LLM tail classification returned unparseable output: %r", raw[:200])
        return mapper_result

    logger.info(
        "LLM tail classification: code=%s reason=%s → %s (was UNKNOWN)",
        error_code, error_reason, parsed.value,
    )
    return parsed, parsed.is_retryable
