"""
LLM-based policy agent for payment retry decisions.

Calls Claude or GPT with structured output enforcing the RetryAction schema.
Falls back to a safe abandon action on any failure.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.agent.actions import FailureContext, RetryAction
from src.agent.prompts import SYSTEM_PROMPT, format_user_prompt
from src.config import get_settings, reveal

logger = logging.getLogger(__name__)


class PolicyAgent:
    """
    LLM-based policy agent. Decides the recovery action for a failed payment.

    Supports Anthropic (Claude) and OpenAI (GPT) as providers.
    Constrained to the fixed RetryAction action space — never freeform.
    """

    def __init__(self, provider: str | None = None) -> None:
        settings = get_settings()
        self._provider = provider or settings.llm_provider
        self._model = settings.llm_model
        self._temperature = settings.llm_temperature  # OpenAI path only
        self._effort = settings.llm_effort
        self._max_tokens = settings.llm_max_tokens
        self._timeout = settings.llm_timeout_seconds

        # Two SDK clients with entirely different shapes. The branch below keys
        # off self._provider (a str), which mypy cannot use to narrow a union,
        # so a union type here would need a cast at every call site.
        self._client: Any
        if self._provider == "anthropic":
            if settings.aws_access_key_id and settings.aws_secret_access_key:
                from anthropic import AsyncAnthropicBedrock
                self._client = AsyncAnthropicBedrock(
                    aws_access_key=reveal(settings.aws_access_key_id),
                    aws_secret_key=reveal(settings.aws_secret_access_key),
                    aws_region=settings.aws_region,
                )
            else:
                import anthropic
                self._client = anthropic.AsyncAnthropic(
                    api_key=reveal(settings.anthropic_api_key),
                    timeout=self._timeout,
                )
        elif self._provider == "openai":
            import openai
            self._client = openai.AsyncOpenAI(
                api_key=reveal(settings.openai_api_key),
                # Empty base_url means api.openai.com; set it to point the same
                # client at any OpenAI-compatible host (OpenRouter, Ollama, ...).
                base_url=settings.llm_base_url or None,
                timeout=self._timeout,
            )
        else:
            raise ValueError(f"Unsupported LLM provider: {self._provider}")

        # Counts decisions that came from _fallback_action rather than the LLM.
        # PolicyAgent.decide() swallows LLM errors and returns a heuristic action
        # that is indistinguishable from a real decision at the call site — so
        # without this counter a totally dead LLM still yields a full, plausible
        # results table. Callers must check it before labelling output "LLM".
        self.call_count = 0
        self.fallback_count = 0
        # HTTP status of the last unrecoverable API failure, surfaced so callers
        # can stop early. decide() swallows its own errors by design, so without
        # this a bad key or empty balance is invisible above this class.
        self.last_error_status: int | None = None

        logger.info("PolicyAgent initialized: provider=%s, model=%s", self._provider, self._model)

    async def decide(self, context: FailureContext) -> RetryAction:
        """
        Decide the recovery action for a failed payment.

        Args:
            context: Full failure context with payment, customer, and temporal info.

        Returns:
            RetryAction — validated, constrained action from the fixed action space.
        """
        user_prompt = format_user_prompt(context)
        self.call_count += 1

        try:
            raw_response = await self._call_llm(user_prompt)
            action = self._parse_response(raw_response)

            if action is None:
                # Retry once with a correction prompt
                logger.warning("First LLM response failed to parse, retrying with correction")
                correction = (
                    f"Your previous response was not valid JSON:\n{raw_response}\n\n"
                    "Please respond with ONLY a valid JSON object matching the RetryAction schema."
                )
                raw_response = await self._call_llm(correction)
                action = self._parse_response(raw_response)

            if action is None:
                logger.error("LLM failed to produce valid action after retry — falling back")
                self.fallback_count += 1
                return self._fallback_action(context, "LLM output could not be parsed")

            logger.info(
                "Agent decision: payment=%s action=%s rail=%s confidence=%s reason=%s",
                context.payment_id,
                action.action,
                action.rail,
                action.confidence,
                action.reason[:80],
            )
            return action

        except Exception as e:
            logger.exception("LLM call failed: %s", str(e))
            status = getattr(e, "status_code", None)
            if status in (401, 402, 403, 404):
                self.last_error_status = status
                self.last_error_detail = f"HTTP {status}: {str(e)[:160]}"
            self.fallback_count += 1
            return self._fallback_action(context, f"LLM error: {str(e)}")

    async def _call_llm(self, user_prompt: str) -> str:
        """Call the LLM and return the raw response text."""
        if self._provider == "anthropic":
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                # No temperature: sampling params were removed on current Claude
                # models and return a 400. Depth is controlled by effort.
                output_config={"effort": self._effort},
                # The system prompt is byte-identical on every call, so cache it.
                # Across an eval run of thousands of decisions this is the
                # difference between full price and ~10% on the bulk of the input.
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
            )
            # Thinking is on by default; the JSON is in the text block, which is
            # not necessarily content[0].
            return next(
                (b.text for b in response.content if b.type == "text"), ""
            )

        elif self._provider == "openai":
            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return response.choices[0].message.content or ""

        raise ValueError(f"Unsupported provider: {self._provider}")

    @staticmethod
    def _parse_response(raw: str) -> RetryAction | None:
        """Parse LLM response into a validated RetryAction."""
        try:
            # Strip markdown code fences if present
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [ln for ln in lines if not ln.strip().startswith("```")]
                text = "\n".join(lines)

            data = json.loads(text)
            return RetryAction(**data)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("Failed to parse LLM response: %s — raw: %s", e, raw[:200])
            return None

    @staticmethod
    def _fallback_action(context: FailureContext, error_detail: str) -> RetryAction:
        """Return a safe fallback action when the LLM fails."""
        from src.classifier.taxonomy import FailureClass

        try:
            fc = FailureClass(context.failure_class)
        except ValueError:
            fc = FailureClass.UNKNOWN

        # Use simple heuristics as fallback
        if fc.is_hard_decline:
            return RetryAction(
                action="abandon",
                reason=f"Fallback: hard decline ({error_detail})",
                confidence=0.9,
            )
        elif fc == FailureClass.NETWORK_ERROR:
            return RetryAction(
                action="retry_now",
                reason=f"Fallback: network error, immediate retry ({error_detail})",
                confidence=0.6,
            )
        elif fc == FailureClass.BANK_DOWNTIME:
            from datetime import timedelta
            return RetryAction(
                action="retry_at",
                retry_at=context.current_time + timedelta(minutes=30),
                reason=f"Fallback: bank downtime, retry in 30min ({error_detail})",
                confidence=0.5,
            )
        else:
            return RetryAction(
                action="abandon",
                reason=f"Fallback: conservative abandon ({error_detail})",
                confidence=0.3,
            )
