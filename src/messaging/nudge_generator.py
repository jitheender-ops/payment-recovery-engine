"""
Recovery nudge message generator.

Uses LLM for personalized messages with a 3-second timeout,
falls back to Jinja2 templates. Never blocks the retry decision.
"""

from __future__ import annotations

import logging
from typing import Any

from src.config import get_settings, reveal
from src.messaging.templates import render_fallback

logger = logging.getLogger(__name__)

_NUDGE_SYSTEM_PROMPT = """\
Generate a brief, empathetic payment failure notification for a customer.
Rules:
- Max 160 characters (SMS limit)
- Be clear about the issue and next step
- Tone: helpful, not pushy
- Do NOT include any links or URLs
- Output ONLY the message text, nothing else
"""


class NudgeGenerator:
    """Generates customer nudge messages — LLM with template fallback."""

    def __init__(self) -> None:
        self._settings = get_settings()
        # Anthropic and OpenAI clients have unrelated shapes; the provider is
        # selected by a str setting, which cannot narrow a union.
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-initialize LLM client."""
        if self._client is not None:
            return self._client

        anthropic_key = reveal(self._settings.anthropic_api_key)
        openai_key = reveal(self._settings.openai_api_key)
        try:
            if self._settings.llm_provider == "anthropic" and anthropic_key:
                import anthropic
                self._client = anthropic.AsyncAnthropic(api_key=anthropic_key, timeout=3.0)
            elif self._settings.llm_provider == "openai" and openai_key:
                import openai
                self._client = openai.AsyncOpenAI(api_key=openai_key, timeout=3.0)
        except Exception:
            logger.warning("Failed to initialize LLM client for nudge generation")

        return self._client

    async def generate(
        self,
        failure_class: str,
        amount: int,
        method: str,
        next_step: str,
        customer_name: str | None = None,
        merchant_name: str = "the merchant",
    ) -> str:
        """
        Generate a customer nudge message.

        Tries LLM first with a 3-second timeout, falls back to template.
        Never blocks or raises — always returns a message.

        Args:
            failure_class: FailureClass value.
            amount: Amount in paise.
            method: Payment method used.
            next_step: Suggested next action for customer.
            customer_name: Optional customer name.
            merchant_name: Merchant display name.

        Returns:
            Nudge message string (max 160 chars).
        """
        amount_display = f"{amount / 100:,.2f}"

        client = self._get_client()
        if client is not None:
            try:
                message = await self._generate_llm(
                    client, failure_class, amount_display, method, next_step,
                    customer_name, merchant_name,
                )
                if message and len(message) <= 200:  # small buffer over 160
                    logger.info("Nudge generated via LLM (len=%d)", len(message))
                    return message[:160]
            except Exception:
                logger.warning("LLM nudge generation failed, using template fallback")

        # Fallback to template
        message = render_fallback(failure_class, amount_display, next_step, customer_name)
        logger.info("Nudge generated via template fallback (len=%d)", len(message))
        return message[:160]

    async def _generate_llm(
        self,
        client: Any,
        failure_class: str,
        amount_display: str,
        method: str,
        next_step: str,
        customer_name: str | None,
        merchant_name: str,
    ) -> str:
        """Call LLM to generate a personalized nudge."""
        user_prompt = (
            f"Payment of ₹{amount_display} via {method} failed. "
            f"Reason: {failure_class.replace('_', ' ')}. "
            f"Customer name: {customer_name or 'unknown'}. "
            f"Merchant: {merchant_name}. "
            f"Suggested next step: {next_step}. "
            f"Generate the notification message (max 160 chars)."
        )

        settings = self._settings
        if settings.llm_provider == "anthropic":
            response = await client.messages.create(
                model=settings.llm_model,
                max_tokens=100,
                # No temperature: sampling params were removed on current Claude
                # models and return a 400 — the same reason the policy agent's
                # call site omits it. Every nudge this path used to attempt
                # failed, and the "LLM" half of LLM-with-template-fallback was
                # silently dead weight.
                system=_NUDGE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return str(response.content[0].text).strip()

        elif settings.llm_provider == "openai":
            response = await client.chat.completions.create(
                model=settings.llm_model,
                max_tokens=100,
                temperature=0.7,
                messages=[
                    {"role": "system", "content": _NUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return (response.choices[0].message.content or "").strip()

        return ""
