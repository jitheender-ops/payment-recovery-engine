"""
Per-risk-type chase policies — the bounds each chaser runs inside.

One frozen dataclass per risk type, looked up by name. Deliberately NOT env
config: these are product promises (how many times we may contact someone
about an overdue invoice), and a promise that can be moved by a typo in an
env var is not a promise. Changing one is a code change, which means a diff,
which means a review.

The payment rail is intentionally absent from RISK_POLICIES. It is
webhook-driven — every payment.failed event re-triggers the pipeline — so it
has no use for first_action/re_chase intervals, and its bounds live where the
rest of the payment rail's bounds live (src/config.py guardrail thresholds).
policy_for("payment_failure") returns None, and every sweep reads that as
"not a chaser risk type", which keeps the due-case sweep from ever
double-triggering the event-driven rail.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.agent.actions import PaymentRail


@dataclass(frozen=True)
class RiskPolicy:
    """The bounded shape of one chaser."""

    # How many engine-initiated contacts this case may ever spend. Persisted
    # onto the case at open time (RecoveryCase.max_attempts) so a later policy
    # change never rewrites the bound a customer was actually chased under.
    max_attempts: int

    # How long after the case opens we may keep chasing. This is the consent
    # window the guardrail enforces, per type instead of the single global one:
    # a cold cart is stale in two days, a receivable is chaseable for a month.
    consent_window_hours: int

    # Quiet gap between the risk event and the first contact. Chasing a cart
    # ninety seconds after it went cold reads as surveillance; waiting an hour
    # reads as a reminder. Zero for events that are already actionable the
    # moment we hear about them (a charge that failed, an invoice already due).
    first_action_hours: float

    # Minimum gap between engine-initiated contacts after the first. Nudges
    # additionally buy their own widening escalation backoff (attach_attempt);
    # this is the floor for link-sending actions, which spend budget without
    # any contact-frequency rule of their own.
    re_chase_hours: float

    # The rail the recovery page recommends and the minted link enforces, or
    # None for a generic link. Subscriptions and mandates fail on card OTPs
    # the same way one-off payments do, so they inherit the UPI preference;
    # a cart has no original rail and an invoice payer chooses their own.
    recommended_rail: PaymentRail | None

    # The failure-class name this risk type wears in FailureContext. The agent
    # prompt, the XGBoost fallback, the nudge templates and the customer page
    # all branch on it. Not a FailureClass enum member on purpose — the
    # guardrail's hard-decline blocklist passes unknown classes through, and
    # these classes are ours, not the gateway's.
    failure_class: str

    # The customer's word for the thing being chased. Drives page copy and
    # link descriptions: "your order", "your subscription", "invoice INV-204",
    # "your autopay". Never "your payment" — for three of these, no payment
    # was ever attempted, and saying so would be a lie on the money line.
    subject_noun: str


RISK_POLICIES: dict[str, RiskPolicy] = {
    # A cart went cold before any payment was attempted. There is no failure
    # to retry and no rail to switch — the only honest action is one gentle
    # reminder with a way to finish. Two contacts total: the reminder, and one
    # follow-up if the window is still open. A third is spam.
    "checkout_abandonment": RiskPolicy(
        max_attempts=2,
        consent_window_hours=48,
        first_action_hours=1.0,
        re_chase_hours=24.0,
        recommended_rail=None,
        failure_class="abandoned_checkout",
        subject_noun="order",
    ),
    # A renewal charge failed. Structurally a payment failure, detected by the
    # merchant's billing system instead of a webhook. The subscription is
    # usually inside a grace period, so there is time — but the customer
    # believes they are still subscribed, so the first touch goes out
    # immediately and the ladder is the payment rail's shape.
    "subscription_failure": RiskPolicy(
        max_attempts=4,
        consent_window_hours=168,  # 7 days — a typical renewal grace period
        first_action_hours=0.0,
        re_chase_hours=24.0,
        recommended_rail="upi",
        failure_class="subscription_charge_failed",
        subject_noun="subscription",
    ),
    # A B2B invoice is past due. The counterparty is a person at a desk, the
    # amounts are larger, and the promise-to-pay is the centre of the flow —
    # so the ladder is slow (72h rungs), the window is long (30 days), and the
    # budget is four contacts, which is where dunning research puts the
    # diminishing returns for receivables.
    "invoice_overdue": RiskPolicy(
        max_attempts=4,
        consent_window_hours=720,  # 30 days
        first_action_hours=0.0,  # already overdue when we hear about it
        re_chase_hours=72.0,
        recommended_rail=None,
        failure_class="invoice_overdue",
        subject_noun="invoice",
    ),
    # A pre-approved autopay debit failed. The mandate IS standing consent to
    # collect, so this is the one type where the engine may simply present the
    # charge again — but not same-day: a failed debit usually means funds or a
    # bank problem that hours will not fix, so the first retry waits a day.
    "mandate_failure": RiskPolicy(
        max_attempts=3,
        consent_window_hours=168,  # 7 days
        first_action_hours=24.0,
        re_chase_hours=48.0,
        recommended_rail="upi",
        failure_class="mandate_debit_failed",
        subject_noun="autopay",
    ),
}


def policy_for(risk_type: str) -> RiskPolicy | None:
    """
    The chase policy for a risk type, or None when it is not chaser-driven.

    None covers payment_failure (webhook-driven — its trigger is the event
    itself, and sweeping it would run the pipeline twice on every failure) and
    any unknown string (fail closed: a risk type with no policy is reported,
    never chased).
    """
    return RISK_POLICIES.get(risk_type)
