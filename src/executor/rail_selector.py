"""
Payment rail selection heuristics.

Suggests alternative payment rails based on the current method and failure class.
"""

from __future__ import annotations

ALL_RAILS = ["upi", "card", "netbanking", "wallet"]


def get_available_rails(current_method: str) -> list[str]:
    """Return rails other than the current one."""
    return [r for r in ALL_RAILS if r != current_method]


def select_alternative_rail(
    current_method: str,
    bank: str | None = None,
    failure_class: str = "",
) -> str | None:
    """
    Select the best alternative payment rail based on failure context.

    Heuristics:
    - 3DS dropoff on card → UPI (simpler auth, no OTP)
    - Issuer decline on card → UPI or netbanking
    - UPI timeout → card
    - Netbanking + bank down → UPI
    - Card limit exceeded → UPI
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
        if current_method == "netbanking":
            return "upi" if prefer_upi else alternatives[0]
        return alternatives[0]

    # Default: prefer UPI, then card
    if prefer_upi:
        return "upi"
    if "card" in alternatives:
        return "card"
    return alternatives[0] if alternatives else None
