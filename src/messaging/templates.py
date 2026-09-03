"""
Jinja2 fallback templates for customer nudge messages.

Used when the LLM is unavailable or times out. Messages are kept under
160 chars for SMS compatibility.
"""

from __future__ import annotations

import re

from jinja2 import BaseLoader, Environment

# Plain text, not HTML: these bodies go out as SMS and email text, where
# autoescape's HTML entities reach the customer verbatim ("didn&#39;t").
# The injection defense for plain text is stripping the characters that
# could carry markup or hide content (angle brackets, quotes, backslashes,
# non-printables/bidi/zero-width) from the webhook-controlled fields — the
# variables in here an attacker influences.
_UNSAFE_CHARS = re.compile(r"[<>\"'`\\]")

_env = Environment(loader=BaseLoader(), autoescape=False)  # nosemgrep


def _sanitize_plain_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _UNSAFE_CHARS.sub("", value)
    # isprintable() False covers control chars, bidi overrides, zero-widths.
    return "".join(ch for ch in cleaned if ch.isprintable() or ch == " ")

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
    "risk_check_failed": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} payment was stopped by a "
        "security check. Please use a different card or UPI."
    ),
    "payment_timeout": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} payment timed out. "
        "We're retrying automatically."
    ),
    # Non-payment risk types (src/chasers/policy.py). No payment was attempted
    # for three of these, so the wording never says "payment failed" — that
    # would be a lie on the one line the customer reads first. Carts carry the
    # items when we have them: personalization is the one lever the research
    # agrees lifts opens (+26%, Slicker) and the meta already holds the data.
    "abandoned_checkout": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} order"
        "{{ ' — ' + items if items else '' }} is still waiting. "
        "Complete it here when you're ready. {{ next_step }}"
    ),
    "subscription_charge_failed": (
        "Hi{{ ' ' + name if name else '' }}, we couldn't renew your subscription "
        "of ₹{{ amount }}. {{ next_step }}"
    ),
    "invoice_overdue": (
        "Hi{{ ' ' + name if name else '' }}, invoice for ₹{{ amount }} is past "
        "due. {{ next_step }}"
    ),
    "mandate_debit_failed": (
        "Hi{{ ' ' + name if name else '' }}, your ₹{{ amount }} autopay didn't go "
        "through. {{ next_step }}"
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
    cart_summary: str | None = None,
) -> str:
    """
    Render a nudge message using the fallback Jinja2 template.

    Args:
        failure_class: The FailureClass value string.
        amount_display: Formatted amount string (e.g., "500.00").
        next_step: What the customer should do next.
        customer_name: Optional customer name.
        cart_summary: Optional bounded item list for cart nudges, already
            scrubbed to one printable line by the caller.

    Returns:
        Rendered message string (under 160 chars for SMS).
    """
    template_str = get_template(failure_class)
    template = _env.from_string(template_str)
    rendered = template.render(
        name=_sanitize_plain_text(customer_name),
        amount=amount_display,
        next_step=_sanitize_plain_text(next_step) or "Please try again.",
        items=_sanitize_plain_text(cart_summary),
    )
    # The 160-char ceiling is a promise (test_every_path_respects_160_characters),
    # and a long item list can break it. The message stays true without the
    # items; it does not stay under 160 with them.
    return rendered[:160]
