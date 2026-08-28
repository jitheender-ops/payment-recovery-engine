"""Chasers: the bounded workflows that chase revenue with no inbound webhook.

A card decline announces itself; an abandoned cart, a halted subscription, an
overdue invoice and a failed mandate debit do not. The case layer
(src/cases.py) is source-agnostic — open_case, stop_reason, due_cases and
attribute_capture all work off risk_type alone — but until this package
existed, nothing acted on the other four risk types: cases could be opened
and nothing ever chased them.

Each chaser is a POLICY, not a pipeline. The pipeline (agent decision →
guardrail → write-ahead execution → attribution) is shared with the payment
rail and lives in src/orchestrator.py; what differs per risk type is the
shape of the bound around it:

    max_attempts          how many contacts this kind of case may ever spend
    consent_window_hours  how long after opening we may keep chasing at all
    first_action_hours    the quiet gap between the event and the first touch
    re_chase_hours        the minimum gap between engine-initiated contacts
    recommended_rail      the rail the page and the link enforce, if any
    failure_class         the class name the agent, guardrail and customer
                          page reason about (never shown raw to the customer)
    subject_noun          the customer's word for the thing being chased

The numbers encode the dunning research rather than a growth hack: the first
cart nudge waits an hour (instant chasing reads as surveillance), invoices
escalate on a multi-day ladder (B2B contacts are people at desks, not phones
in pockets), and every type runs out of budget and stops — a chaser without
a stopping rule is a spam engine.
"""

from src.chasers.policy import RiskPolicy, policy_for

__all__ = ["RiskPolicy", "policy_for"]
