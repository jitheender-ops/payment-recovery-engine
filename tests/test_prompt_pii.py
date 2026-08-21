"""
No customer PII may reach the LLM provider.

The policy prompt is sent to a third party. Everything in it is a business fact
about a failed payment except one field — customer_id, which is a raw email
address or phone number lifted straight off the Razorpay webhook.

These tests assert on the rendered prompt rather than on the masking function
alone, because the leak is a property of the prompt: a mask nobody calls
protects nothing.
"""

from __future__ import annotations

from typing import Any

from src.agent.actions import FailureContext
from src.agent.prompts import format_user_prompt, mask_customer_id
from src.config import get_settings

EMAIL = "priya.sharma@example.com"
PHONE = "+919876543210"


def _ctx(sample_failure_context: FailureContext, customer_id: str | None) -> FailureContext:
    return sample_failure_context.model_copy(
        update={
            "customer_id": customer_id,
            "customer_email": EMAIL,
            "customer_contact": PHONE,
        }
    )


def test_email_never_appears_in_the_prompt(sample_failure_context: FailureContext) -> None:
    prompt = format_user_prompt(_ctx(sample_failure_context, EMAIL))

    assert EMAIL not in prompt
    assert "priya" not in prompt.lower()
    assert mask_customer_id(EMAIL) in prompt


def test_phone_never_appears_in_the_prompt(sample_failure_context: FailureContext) -> None:
    prompt = format_user_prompt(_ctx(sample_failure_context, PHONE))

    assert PHONE not in prompt
    assert "9876543210" not in prompt
    assert mask_customer_id(PHONE) in prompt


def test_the_unmasked_context_fields_are_not_interpolated(
    sample_failure_context: FailureContext,
) -> None:
    """
    FailureContext also carries customer_email and customer_contact. They are
    absent from the template today; this fails the moment someone adds them.
    """
    prompt = format_user_prompt(_ctx(sample_failure_context, "someone.else@example.com"))

    assert EMAIL not in prompt
    assert PHONE not in prompt


def test_the_same_customer_gets_the_same_token(sample_failure_context: FailureContext) -> None:
    """Stability is the point — the model must still see a repeat customer."""
    a = format_user_prompt(_ctx(sample_failure_context, EMAIL))
    b = format_user_prompt(_ctx(sample_failure_context, EMAIL))

    assert mask_customer_id(EMAIL) == mask_customer_id(EMAIL)
    assert a == b


def test_different_customers_get_different_tokens() -> None:
    assert mask_customer_id(EMAIL) != mask_customer_id(PHONE)
    assert mask_customer_id("a@example.com") != mask_customer_id("b@example.com")


def test_missing_customer_id_does_not_crash(sample_failure_context: FailureContext) -> None:
    """Card payments can arrive with neither email nor contact."""
    prompt = format_user_prompt(_ctx(sample_failure_context, None))

    assert "Customer ID: unknown" in prompt
    assert mask_customer_id("") == "unknown"


def test_the_digest_is_keyed_not_a_bare_hash(monkeypatch: Any) -> None:
    """
    An unkeyed hash of a phone number or an email is reversible by enumeration,
    so the token must move when the key does.
    """
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "secret-one")
    get_settings.cache_clear()
    first = mask_customer_id(EMAIL)

    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "secret-two")
    get_settings.cache_clear()
    second = mask_customer_id(EMAIL)

    get_settings.cache_clear()
    assert first != second
    # And it is not the plaintext dressed up.
    assert EMAIL not in first
