"""
Payment rail selection heuristics.

Which rail to retry on when the current one just failed. The orchestrator
consults this between the agent's decision and the guardrail gate: the agent
proposes a rail, this decides whether that proposal survives.
"""

from __future__ import annotations

from typing import get_args

from src.agent.actions import PaymentRail

# Derived from the action schema rather than restated. A rail listed here but
# absent from PaymentRail would be selected, then rejected by the guardrail as
# a schema violation — a switch that silently never happens.
ALL_RAILS: list[PaymentRail] = list(get_args(PaymentRail))


def get_available_rails(current_method: str) -> list[PaymentRail]:
    """Return rails other than the current one."""
    return [r for r in ALL_RAILS if r != current_method]


def select_alternative_rail(
    current_method: str,
    failure_class: str = "",
) -> PaymentRail | None:
    """
    Select the best alternative payment rail based on failure context.

    Heuristics:
    - 3DS dropoff on card → UPI (simpler auth, no OTP)
    - Issuer decline on card → UPI or netbanking
    - UPI timeout → card
    - Netbanking + bank down → UPI
    - Card limit exceeded → UPI

    Never returns `current_method`: the caller has established that rail just
    failed. Returns None only if there is no other rail at all.
    """
    alternatives = get_available_rails(current_method)
    if not alternatives:
        return None

    # Prefer UPI as the default alternative (highest success rate, simplest flow)
    prefer_upi = "upi" in alternatives

    if failure_class in ("3ds_dropoff", "issuer_decline", "card_limit_exceeded"):
        return "upi" if prefer_upi else alternatives[0]

    if failure_class == "upi_collect_timeout":
        return "card" if "card" in alternatives else alternatives[0]

    if failure_class == "bank_downtime":
        # ponytail: bank identity is ignored — every rail here can route back to
        # the same down bank (a UPI VPA on it, netbanking into it). Real
        # downtime-aware routing needs a bank-health source we don't have; wire
        # one in and take `bank` as an argument when it exists.
        if current_method == "netbanking":
            return "upi" if prefer_upi else alternatives[0]
        return alternatives[0]

    # Default: prefer UPI, then card
    if prefer_upi:
        return "upi"
    if "card" in alternatives:
        return "card"
    return alternatives[0]


def resolve_target_rail(
    current_method: str,
    proposed: PaymentRail | None,
    failure_class: str = "",
) -> PaymentRail | None:
    """
    The rail a `switch_rail` action should actually execute on.

    Keeps the agent's own choice — it has context this heuristic doesn't. The
    one case it overrides is a switch onto the rail that just declined, which
    nothing upstream catches: the schema check requires `rail` to be non-null
    and a valid literal, and "the same one" satisfies both. That retry is dead
    on arrival and still costs an attempt slot out of the max of three, plus a
    live Payment Link call.
    """
    if proposed is not None and proposed != current_method:
        return proposed
    return select_alternative_rail(current_method, failure_class)
