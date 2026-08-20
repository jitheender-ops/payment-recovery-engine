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

# ── Failure-class retry success modifiers ────────────────────────────────
FAILURE_RETRY_MODIFIERS: dict[str, dict[str, float]] = {
    "insufficient_funds":  {"same_rail": 0.30, "diff_rail": 0.25, "after_nudge": 0.45},
    "bank_downtime":       {"same_rail": 0.70, "diff_rail": 0.85, "after_delay_30m": 0.80, "after_delay_2h": 0.92},
    "network_error":       {"same_rail": 0.85, "diff_rail": 0.88, "immediate": 0.85},
    "3ds_dropoff":         {"same_rail": 0.55, "diff_rail": 0.78, "to_upi": 0.82},
    "upi_collect_timeout": {"same_rail": 0.50, "diff_rail": 0.60, "after_nudge": 0.70},
    "issuer_decline":      {"same_rail": 0.20, "diff_rail": 0.60, "to_upi": 0.65},
    "payment_timeout":     {"same_rail": 0.75, "diff_rail": 0.80, "immediate": 0.78},
    "card_limit_exceeded": {"same_rail": 0.10, "diff_rail": 0.55, "after_nudge": 0.50},
}


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
        # Base rate for this rail
        base = self.base_rates.get(rail, 0.80)

        # Time-of-day modifier
        time_mod = HOUR_MODIFIERS.get(hour % 24, 0.95)

        prob = base * time_mod

        # Apply failure-class retry modifier
        if is_retry and failure_class in FAILURE_RETRY_MODIFIERS:
            mods = FAILURE_RETRY_MODIFIERS[failure_class]

            if after_nudge and "after_nudge" in mods:
                prob *= mods["after_nudge"]
            elif switched_rail and "diff_rail" in mods:
                prob *= mods["diff_rail"]
            elif failure_class == "bank_downtime" and delay_minutes >= 120:
                prob *= mods.get("after_delay_2h", 0.92)
            elif failure_class == "bank_downtime" and delay_minutes >= 30:
                prob *= mods.get("after_delay_30m", 0.80)
            elif "same_rail" in mods:
                prob *= mods["same_rail"]

        return min(max(prob, 0.0), 1.0)


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
