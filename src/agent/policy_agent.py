"""
LLM-based policy agent for payment retry decisions.

Calls Claude or GPT with structured output enforcing the RetryAction schema.
Falls back to a safe abandon action on any failure.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from src.agent.actions import FailureContext, RetryAction
from src.agent.prompts import SYSTEM_PROMPT, format_user_prompt
from src.config import get_settings

logger = logging.getLogger(__name__)


class PolicyAgent:
    """
    LLM-based policy agent. Decides the recovery action for a failed payment.

    Supports Anthropic (Claude) and OpenAI (GPT) as providers.
    Constrained to the fixed RetryAction action space — never freeform.
    """

    def __init__(self, provider: Optional[str] = None) -> None:
        settings = get_settings()
        self._provider = provider or settings.llm_provider
        self._model = settings.llm_model
        self._temperature = settings.llm_temperature
        self._max_tokens = settings.llm_max_tokens
        self._timeout = settings.llm_timeout_seconds

        if self._provider == "anthropic":
            import anthropic
            self._client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                timeout=self._timeout,
            )
        elif self._provider == "openai":
            import openai
            self._client = openai.AsyncOpenAI(
                api_key=settings.openai_api_key,
                timeout=self._timeout,
            )
        else:
            raise ValueError(f"Unsupported LLM provider: {self._provider}")

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
            return self._fallback_action(context, f"LLM error: {str(e)}")

    async def _call_llm(self, user_prompt: str) -> str:
        """Call the LLM and return the raw response text."""
        if self._provider == "anthropic":
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text

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
    def _parse_response(raw: str) -> Optional[RetryAction]:
        """Parse LLM response into a validated RetryAction."""
        try:
            # Strip markdown code fences if present
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
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
