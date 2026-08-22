"""
Jinja2 fallback templates for customer nudge messages.

Used when the LLM is unavailable or times out. Messages are kept under
160 chars for SMS compatibility.
"""

from __future__ import annotations

from jinja2 import Environment, BaseLoader

_env = Environment(loader=BaseLoader())

# Per-failure-class templates — all under 160 chars for SMS
_TEMPLATES: dict[str, str] = {
    "insufficient_funds": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} payment didn't go through "
        "due to low balance. {{ next_step }}"
    ),
    "bank_downtime": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} payment failed due to a "
        "temporary bank issue. We'll retry shortly."
    ),
    "3ds_dropoff": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} payment needs OTP "
        "verification. {{ next_step }}"
    ),
    "upi_collect_timeout": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} UPI payment timed out. "
        "Please approve the request or try again."
    ),
    "issuer_decline": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} payment was declined by "
        "your bank. {{ next_step }}"
    ),
    "network_error": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} payment hit a temporary "
        "glitch. We're retrying automatically."
    ),
    "card_limit_exceeded": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} payment exceeded your card "
        "limit. Try a different card or UPI."
    ),
    "payment_timeout": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} payment timed out. "
        "We're retrying automatically."
    ),
}

_FALLBACK_TEMPLATE = (
    "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} payment didn't go through. "
    "{{ next_step }}"
)


def get_template(failure_class: str) -> str:
    """Get the raw template string for a failure class."""
    return _TEMPLATES.get(failure_class, _FALLBACK_TEMPLATE)


def render_fallback(
    failure_class: str,
    amount_display: str,
    next_step: str = "Please try again or use a different payment method.",
    customer_name: str | None = None,
) -> str:
    """
    Render a nudge message using the fallback Jinja2 template.

    Args:
        failure_class: The FailureClass value string.
        amount_display: Formatted amount string (e.g., "500.00").
        next_step: What the customer should do next.
        customer_name: Optional customer name.

    Returns:
        Rendered message string (under 160 chars for SMS).
    """
    template_str = get_template(failure_class)
    template = _env.from_string(template_str)
    return template.render(
        name=customer_name,
        amount=amount_display,
        next_step=next_step,
    )
