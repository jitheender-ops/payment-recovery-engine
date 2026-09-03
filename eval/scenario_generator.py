"""
Synthetic payment failure scenario generator.

Generates ~5,000 failure scenarios with distributions modelled on Indian
payment patterns: UPI-heavy, log-normal amounts, time-of-day peaks.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from eval.bank_profiles import BANK_WEIGHTS

# Failure class distribution (weighted by real-world frequency)
FAILURE_WEIGHTS: dict[str, float] = {
    "insufficient_funds": 0.20,
    "3ds_dropoff": 0.15,
    "bank_downtime": 0.12,
    "network_error": 0.12,
    "upi_collect_timeout": 0.10,
    "issuer_decline": 0.10,
    "payment_timeout": 0.08,
    "card_limit_exceeded": 0.05,
    "invalid_card": 0.03,
    "expired_instrument": 0.02,
    "fraud_block": 0.01,
    "customer_cancelled": 0.01,
    "hard_decline": 0.01,
}

# The named mixes the harness can run. A mix changes the POPULATION — which
# failures reach the engine — never the physics (CONDITION_CLEARS decides
# whether a given blocker clears, and by which lever).
#
# "vulcan" is a hypothesis, not a measurement: Razorpay Vulcan (Aug 2026) is
# an authorization-time routing/risk model, so the failures that survive it
# lose their self-clearing transients (the four same-rail transient classes
# drop 0.42 -> 0.21 combined) and skew to blockers only a recovery engine
# can move — nudge-path insufficient funds become the modal failure,
# rail-switch cases roughly double, and risk_check_failed appears at all
# (the legacy mix never emits it, so the switch-only guardrail path was
# never exercised by the harness).
#
# ponytail: guessed mix, replace with observed payment_failures distribution
# once deployed (payment_failures is indexed on failure_class).
MIXES: dict[str, dict[str, float]] = {
    "legacy": FAILURE_WEIGHTS,
    "vulcan": {
        "insufficient_funds": 0.26,
        "3ds_dropoff": 0.18,
        "issuer_decline": 0.13,
        "card_limit_exceeded": 0.07,
        "bank_downtime": 0.06,
        "network_error": 0.06,
        "upi_collect_timeout": 0.05,
        "payment_timeout": 0.04,
        "invalid_card": 0.05,
        "expired_instrument": 0.03,
        "fraud_block": 0.02,
        "customer_cancelled": 0.02,
        "hard_decline": 0.02,
        "risk_check_failed": 0.01,
    },
}

# Method distribution
METHOD_WEIGHTS: dict[str, float] = {
    "upi": 0.55,
    "card": 0.35,  # covers credit + debit
    "netbanking": 0.08,
    "wallet": 0.02,
}

# Non-retryable failure classes
NON_RETRYABLE = {
    "invalid_card", "expired_instrument", "fraud_block",
    "customer_cancelled", "hard_decline",
}


class ScenarioGenerator:
    """Generates synthetic payment failure scenarios for eval."""

    def __init__(self, seed: int = 42, mix: str = "legacy") -> None:
        if mix not in MIXES:
            raise ValueError(f"unknown mix {mix!r}; known: {sorted(MIXES)}")
        self._rng = np.random.RandomState(seed)
        self.mix = mix
        # Copied, not referenced: FAILURE_WEIGHTS (the "legacy" entry) stays
        # byte-for-byte what every pre-existing result was generated with,
        # regardless of what anyone does to this instance.
        self.failure_weights: dict[str, float] = dict(MIXES[mix])

    def generate(self, n: int = 5000) -> pd.DataFrame:
        """
        Generate n synthetic payment failure scenarios.

        Returns DataFrame with columns:
            scenario_id, payment_id, order_id, amount, method, bank,
            failure_class, hour_of_day, day_of_week, customer_id, is_retryable
        """
        # Pre-compute weighted choices
        banks = list(BANK_WEIGHTS.keys())
        bank_probs = np.array(list(BANK_WEIGHTS.values()))
        bank_probs /= bank_probs.sum()

        methods = list(METHOD_WEIGHTS.keys())
        method_probs = np.array(list(METHOD_WEIGHTS.values()))
        method_probs /= method_probs.sum()

        failures = list(self.failure_weights.keys())
        failure_probs = np.array(list(self.failure_weights.values()))
        failure_probs /= failure_probs.sum()

        records = []
        n_customers = min(n // 2, 2000)  # ~2000 unique customers

        for i in range(n):
            # Amount: log-normal, median ~₹500, clipped to [₹10, ₹1L]
            amount = int(self._rng.lognormal(mean=10.8, sigma=1.2))
            amount = max(1000, min(amount, 10_000_000))  # paise

            method = self._rng.choice(methods, p=method_probs)
            bank = self._rng.choice(banks, p=bank_probs)
            failure_class = self._rng.choice(failures, p=failure_probs)

            # Time of day: mixture of peaks at 10, 13, 20
            peak = self._rng.choice([10, 13, 20], p=[0.3, 0.25, 0.45])
            hour = int(self._rng.normal(peak, 3)) % 24

            # Day of week: slight weekday bias
            day_of_week = self._rng.choice(
                7, p=[0.16, 0.16, 0.16, 0.16, 0.15, 0.11, 0.10]
            )

            customer_id = f"cust_{self._rng.randint(0, n_customers):04d}"

            records.append({
                "scenario_id": i,
                "payment_id": f"pay_sim_{i:06d}",
                "order_id": f"order_sim_{i:06d}",
                "amount": amount,
                "method": method,
                "bank": bank,
                "failure_class": failure_class,
                "hour_of_day": hour,
                "day_of_week": int(day_of_week),
                "customer_id": customer_id,
                "is_retryable": failure_class not in NON_RETRYABLE,
            })

        return pd.DataFrame(records)
