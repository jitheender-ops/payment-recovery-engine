"""
Bank success probability profiles for the eval harness simulator.

Models per-bank × per-rail × per-hour success rates for major Indian banks.
NOTE: These are synthetic estimates, NOT derived from real bank data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ── Bank market share weights ────────────────────────────────────────────
BANK_WEIGHTS: dict[str, float] = {
    "SBI": 0.25, "HDFC": 0.18, "ICICI": 0.15, "Axis": 0.10,
    "Kotak": 0.07, "PNB": 0.06, "BOB": 0.05, "Yes Bank": 0.05,
    "IndusInd": 0.05, "Federal": 0.04,
}

# ── Base success rates per rail ──────────────────────────────────────────
RAIL_BASE_RATES: dict[str, dict[str, float]] = {
    "SBI":      {"upi": 0.87, "credit_card": 0.90, "debit_card": 0.83, "card": 0.86, "netbanking": 0.80, "wallet": 0.92},
    "HDFC":     {"upi": 0.91, "credit_card": 0.94, "debit_card": 0.88, "card": 0.91, "netbanking": 0.86, "wallet": 0.94},
    "ICICI":    {"upi": 0.90, "credit_card": 0.93, "debit_card": 0.87, "card": 0.90, "netbanking": 0.85, "wallet": 0.93},
    "Axis":     {"upi": 0.89, "credit_card": 0.92, "debit_card": 0.85, "card": 0.88, "netbanking": 0.83, "wallet": 0.92},
    "Kotak":    {"upi": 0.88, "credit_card": 0.91, "debit_card": 0.84, "card": 0.87, "netbanking": 0.82, "wallet": 0.91},
    "PNB":      {"upi": 0.85, "credit_card": 0.89, "debit_card": 0.82, "card": 0.85, "netbanking": 0.78, "wallet": 0.90},
    "BOB":      {"upi": 0.84, "credit_card": 0.88, "debit_card": 0.81, "card": 0.84, "netbanking": 0.78, "wallet": 0.90},
    "Yes Bank": {"upi": 0.86, "credit_card": 0.89, "debit_card": 0.83, "card": 0.86, "netbanking": 0.79, "wallet": 0.91},
    "IndusInd": {"upi": 0.87, "credit_card": 0.90, "debit_card": 0.84, "card": 0.87, "netbanking": 0.80, "wallet": 0.91},
    "Federal":  {"upi": 0.85, "credit_card": 0.88, "debit_card": 0.82, "card": 0.85, "netbanking": 0.79, "wallet": 0.90},
}

# ── Time-of-day modifiers (multiplicative) ───────────────────────────────
HOUR_MODIFIERS: dict[int, float] = {
    0: 0.85, 1: 0.85, 2: 0.85, 3: 0.85, 4: 0.85, 5: 0.85,
    6: 0.90, 7: 0.95, 8: 0.95, 9: 0.95,
    10: 1.00, 11: 1.00, 12: 1.00, 13: 1.00,
    14: 0.98, 15: 0.98, 16: 0.98,
    17: 1.00, 18: 1.00, 19: 1.00, 20: 1.00,
    21: 0.93, 22: 0.93,
    23: 0.88,
}

# ── Probability the underlying failure condition has CLEARED ─────────────
#
# This is the model's central assumption, and the earlier version got it wrong.
# A retry is not a fresh payment. The payment already failed for a specific
# reason, so the bank's baseline approval rate (0.85-0.94, calibrated on random
# new payments) says almost nothing about whether a retry works. What matters is
# whether the *cause* went away:
#
#   P(retry succeeds) = P(condition cleared) x P(payment clears | condition gone)
#
# The second term is the bank/rail/hour baseline. The first term is below, and
# it is much smaller than the old modifiers implied — which is why the old model
# reported ~73% blended recovery against a real world nearer 15-30%.
#
# Reading the numbers: insufficient_funds barely improves on an immediate retry
# because the customer's balance has not changed; it improves a lot after a nudge
# because the nudge is what prompts them to top up. card_limit_exceeded is near
# hopeless on the same card and fine on another rail. network_error is genuinely
# transient, so it clears on its own.
CONDITION_CLEARS: dict[str, dict[str, float]] = {
    "insufficient_funds":  {"same_rail": 0.05, "diff_rail": 0.05, "after_nudge": 0.26},
    "bank_downtime":       {"same_rail": 0.20, "diff_rail": 0.58},
    "network_error":       {"same_rail": 0.60, "diff_rail": 0.62},
    "3ds_dropoff":         {"same_rail": 0.13, "diff_rail": 0.32, "after_nudge": 0.40},
    "upi_collect_timeout": {"same_rail": 0.16, "diff_rail": 0.21, "after_nudge": 0.44},
    "issuer_decline":      {"same_rail": 0.07, "diff_rail": 0.30},
    "payment_timeout":     {"same_rail": 0.46, "diff_rail": 0.50},
    "card_limit_exceeded": {"same_rail": 0.04, "diff_rail": 0.32, "after_nudge": 0.14},
}

# Global calibration knob. The per-class numbers above encode *relative* ordering
# (which failures are recoverable and by what lever); this scales all of them to
# land the blended fixed-3-retry recovery rate inside the 15-30% band that public
# figures report. Tune this, not the individual classes, when re-anchoring to a
# new source — the relative structure is the part worth preserving.
#
# ponytail: single global scalar, per-class calibration if real bank data lands.
RETRY_CALIBRATION: float = 0.62

# How much of the remaining gap a wait closes for bank downtime. Replaces the old
# model, which applied a delay multiplier AND an additive delay bonus — rewarding
# the same wait twice and mixing additive with multiplicative for no stated reason.
DOWNTIME_RECOVERY_HALFLIFE_MINUTES: float = 45.0


@dataclass
class BankProfile:
    """Success probability profile for a single bank."""

    name: str
    base_rates: dict[str, float] = field(default_factory=dict)

    def get_success_probability(
        self,
        rail: str,
        hour: int,
        failure_class: str = "",
        is_retry: bool = False,
        switched_rail: bool = False,
        delay_minutes: int = 0,
        after_nudge: bool = False,
    ) -> float:
        """
        Calculate the success probability for a retry attempt.

        Returns a float in [0, 1].
        """
        # P(payment clears | the original blocker is gone) — the bank's baseline.
        base = self.base_rates.get(rail, 0.80)
        time_mod = HOUR_MODIFIERS.get(hour % 24, 0.95)
        prob = base * time_mod

        if not is_retry:
            return min(max(prob, 0.0), 1.0)

        # P(the original blocker is gone).
        clears = CONDITION_CLEARS.get(failure_class)
        if clears is None:
            # Unmodelled failure class — assume mostly unrecoverable rather than
            # silently inheriting the full baseline, which is what made unknown
            # classes look as recoverable as a transient network blip.
            return min(max(prob * 0.15 * RETRY_CALIBRATION, 0.0), 1.0)

        if after_nudge and "after_nudge" in clears:
            p_clear = clears["after_nudge"]
        elif switched_rail:
            p_clear = clears["diff_rail"]
        else:
            p_clear = clears["same_rail"]

        # Waiting only helps when the blocker is time-based. Bank downtime ends
        # on its own schedule, so a wait closes the gap toward the switched-rail
        # ceiling on an exponential curve. Applied once — never stacked on top of
        # a separate delay multiplier.
        if failure_class == "bank_downtime" and delay_minutes > 0:
            ceiling = clears["diff_rail"]
            recovered_fraction = 1.0 - 0.5 ** (delay_minutes / DOWNTIME_RECOVERY_HALFLIFE_MINUTES)
            p_clear += (ceiling - p_clear) * recovered_fraction

        return min(max(prob * p_clear * RETRY_CALIBRATION, 0.0), 1.0)


def get_bank_profile(bank_name: str) -> BankProfile:
    """Get the profile for a bank (falls back to average if unknown)."""
    rates = RAIL_BASE_RATES.get(bank_name, {
        "upi": 0.86, "credit_card": 0.89, "debit_card": 0.83,
        "card": 0.86, "netbanking": 0.80, "wallet": 0.91,
    })
    return BankProfile(name=bank_name, base_rates=rates)


def get_all_banks() -> list[str]:
    """Return list of all modeled bank names."""
    return list(BANK_WEIGHTS.keys())
