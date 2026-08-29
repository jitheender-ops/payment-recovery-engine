"""
Recovery nudge message generator.

Uses LLM for personalized messages with a 3-second timeout,
falls back to Jinja2 templates. Never blocks the retry decision.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.config import get_settings, reveal
from src.messaging.templates import render_fallback

logger = logging.getLogger(__name__)

_NUDGE_SYSTEM_PROMPT = """\
Generate a brief, empathetic recovery notification for a customer.
Rules:
- Max 160 characters (SMS limit)
- Be clear about the situation and next step
- Tone: helpful, not pushy
- Do NOT include any links or URLs
- If the situation says no payment was attempted (an abandoned order, an
  overdue invoice), never say "payment failed" or "your payment" — it didn't
- Output ONLY the message text, nothing else
"""

# The situation line per chaser-driven risk type (src/chasers/policy.py).
# Three of the four never attempted a payment, so the LLM must be told that
# plainly — the old prompt opened every situation with "Payment of ₹X failed",
# which made the primary (LLM) path lie about carts and invoices while the
# template fallback told the truth.
_RISK_SITUATIONS: dict[str, str] = {
    "checkout_abandonment": (
        "An order of ₹{amount} was left incomplete at checkout. "
        "No payment was attempted."
    ),
    "subscription_failure": (
        "A subscription renewal of ₹{amount} didn't go through."
    ),
    "invoice_overdue": (
        "An invoice of ₹{amount} is past due and unpaid. "
        "No payment has been received."
    ),
    "mandate_failure": (
        "An autopay debit of ₹{amount} didn't go through."
    ),
}


# Fields that reach this prompt from a payload we do not author: `method` is
# webhook-supplied free text (the orchestrator caps it at 50 chars but does not
# scrub it) and `customer_name` is whatever the merchant's checkout collected.
# Whatever the LLM writes becomes the Razorpay link's checkout description —
# the text the payer reads — so injected instructions here buy an attacker 160
# customer-visible characters. Collapsing to a single line of printable
# characters removes the shape those instructions need (newlines, fake role
# markers, braces) without touching legitimate values like "card" or "netbanking".
_PROMPT_UNSAFE = re.compile(r"[^\w .,'&/@+-]", re.UNICODE)


def _scrub(value: str | None, limit: int = 60) -> str | None:
    """One line of printable characters, or None. For anything we did not author."""
    if value is None:
        return None
    cleaned = _PROMPT_UNSAFE.sub(" ", value)
    cleaned = " ".join(cleaned.split())[:limit].strip()
    return cleaned or None


def build_llm_prompt(
    *,
    failure_class: str,
    amount_display: str,
    method: str,
    next_step: str,
    customer_name: str | None,
    merchant_name: str,
    risk_type: str | None = None,
) -> str:
    """The nudge LLM's user prompt. Split out so the wording is testable."""
    method = _scrub(method) or "unknown"
    customer_name = _scrub(customer_name, limit=80)
    situation = _RISK_SITUATIONS.get(risk_type or "")
    if situation is not None:
        opening = situation.format(amount=amount_display)
    else:
        opening = (
            f"Payment of ₹{amount_display} via {method} failed. "
            f"Reason: {failure_class.replace('_', ' ')}."
        )
    return (
        f"{opening} "
        f"Customer name: {customer_name or 'unknown'}. "
        f"Merchant: {merchant_name}. "
        f"Suggested next step: {next_step}. "
        f"Generate the notification message (max 160 chars)."
    )


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
                self._client = openai.AsyncOpenAI(
                    api_key=openai_key,
                    base_url=self._settings.llm_base_url or None,
                    timeout=3.0,
                )
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
        risk_type: str | None = None,
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
            risk_type: Chaser risk type, when there is no payment failure
                behind the nudge — it selects honest situation wording.

        Returns:
            Nudge message string (max 160 chars).
        """
        amount_display = f"{amount / 100:,.2f}"

        client = self._get_client()
        if client is not None:
            try:
                message = await self._generate_llm(
                    client, failure_class, amount_display, method, next_step,
                    customer_name, merchant_name, risk_type,
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
        risk_type: str | None = None,
    ) -> str:
        """Call LLM to generate a personalized nudge."""
        user_prompt = build_llm_prompt(
            failure_class=failure_class,
            amount_display=amount_display,
            method=method,
            next_step=next_step,
            customer_name=customer_name,
            merchant_name=merchant_name,
            risk_type=risk_type,
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
