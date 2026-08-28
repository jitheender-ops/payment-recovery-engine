"""
What to tell a customer whose payment just failed.

The one question behind every other question is "where is my money". A failed
payment never captured funds, so the honest answer is almost always "still with
you" — but a customer who saw a debit alert on their phone does not believe that
unless the page says it plainly. Indian issuers place a temporary hold on some
declines and release it over the next few working days; naming that is the
difference between a reassured customer and a support ticket.

Every entry answers four things in the customer's order of anxiety:

    headline    what happened, in their words, never ours
    money       where their money is right now
    next        the one thing to do
    retryable   whether offering another attempt is honest

`retryable=False` is not a UI detail. Offering "try again" on a fraud block
sends someone into a wall their bank put up deliberately, and offering it on an
expired card guarantees a second failure. Those cases get a different action.
"""

from __future__ import annotations

from typing import NamedTuple


class Explanation(NamedTuple):
    headline: str
    money: str
    next: str
    retryable: bool


# The money line is deliberately near-identical across classes: it is the same
# true fact every time, and varying the wording would imply the answer changes.
_HELD = (
    "No money has left your account. If your bank showed a debit, that is a "
    "temporary hold and it is released automatically within 3 to 5 working days."
)

_BY_CLASS: dict[str, Explanation] = {
    "insufficient_funds": Explanation(
        "Your bank declined the payment for insufficient balance",
        _HELD,
        "Add funds or use a different card or UPI app, then pay below.",
        True,
    ),
    "3ds_dropoff": Explanation(
        "The OTP step wasn't completed",
        _HELD,
        "Pay below and complete the OTP your bank sends. UPI skips OTP entirely.",
        True,
    ),
    "upi_collect_timeout": Explanation(
        "The UPI request expired before it was approved",
        _HELD,
        "Pay below and approve the request in your UPI app within a few minutes.",
        True,
    ),
    "bank_downtime": Explanation(
        "Your bank was temporarily unreachable",
        _HELD,
        "This usually clears quickly. Pay below, or try a different bank or UPI.",
        True,
    ),
    "network_error": Explanation(
        "The connection dropped while the payment was going through",
        _HELD,
        "Pay below. This usually works on the second attempt.",
        True,
    ),
    "payment_timeout": Explanation(
        "The payment took too long and timed out",
        _HELD,
        "Pay below. If you are on a weak connection, UPI is usually faster.",
        True,
    ),
    "issuer_decline": Explanation(
        "Your bank declined the payment",
        _HELD,
        "Banks decline for many reasons and rarely say which. Try a different "
        "card or UPI below, or call the number on your card.",
        True,
    ),
    "card_limit_exceeded": Explanation(
        "The payment is over your card's limit",
        _HELD,
        "Use a different card, or pay by UPI or netbanking below.",
        True,
    ),
    "invalid_card": Explanation(
        "Those card details weren't accepted",
        _HELD,
        "Check the number, expiry and CVV, or use a different payment method below.",
        True,
    ),
    "expired_instrument": Explanation(
        "That card has expired",
        _HELD,
        "Use a different card, or pay by UPI or netbanking below.",
        False,
    ),
    "fraud_block": Explanation(
        "Your bank blocked this payment as a security precaution",
        _HELD,
        "Only your bank can clear this. Call the number on the back of your "
        "card before trying again.",
        False,
    ),
    "hard_decline": Explanation(
        "Your bank declined this payment permanently",
        _HELD,
        "Another attempt on the same method will be declined too. Use a "
        "different card or UPI, or contact your bank.",
        False,
    ),
    "customer_cancelled": Explanation(
        "The payment was cancelled",
        _HELD,
        "You can complete it below whenever you're ready.",
        True,
    ),
}

_UNKNOWN = Explanation(
    "The payment didn't go through",
    _HELD,
    "You can try again below.",
    True,
)

# The four chaser-driven risk types (src/chasers/policy.py). Three of these
# never attempted a payment at all, so the money line says so plainly — and
# the wording never says "your payment failed", because it didn't.
_HELD_NOT_CHARGED = (
    "No money has left your account — nothing was charged."
)
_BY_RISK_TYPE: dict[str, Explanation] = {
    "checkout_abandonment": Explanation(
        "Your order wasn't completed",
        _HELD_NOT_CHARGED,
        "Pay below to finish your order whenever you're ready.",
        True,
    ),
    "subscription_failure": Explanation(
        "Your subscription renewal didn't go through",
        _HELD,
        "Pay below to keep your subscription active.",
        True,
    ),
    "invoice_overdue": Explanation(
        "This invoice is past due",
        "No payment has been received for this invoice yet.",
        "Pay below, or reply to our message if you need more time.",
        True,
    ),
    "mandate_failure": Explanation(
        "Your autopay debit didn't go through",
        _HELD,
        "Pay below, or approve the request in your UPI app if one is pending.",
        True,
    ),
}


def explain(failure_class: str | None, risk_type: str | None = None) -> Explanation:
    """
    The customer-facing reading of a failure class or risk type. Never raises.

    For the chaser-driven risk types there is no gateway failure class — the
    risk type itself names what happened, so it is looked up first.
    """
    if risk_type is not None and risk_type in _BY_RISK_TYPE:
        return _BY_RISK_TYPE[risk_type]
    return _BY_CLASS.get(failure_class or "", _UNKNOWN)
