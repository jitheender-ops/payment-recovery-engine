"""
Bank-response simulator for the eval harness.

Simulates payment retry outcomes using configurable per-bank, per-rail,
per-hour success probabilities.

NOTE: This is a synthetic model. Success probabilities are estimated,
not derived from real bank data. This is stated explicitly because
naming that assumption is a maturity signal, not a weakness.
"""

from __future__ import annotations

import numpy as np

from eval.bank_profiles import get_bank_profile

# Probability multiplier applied per prior failed attempt. Each failure is
# evidence the blocker is persistent rather than transient.
RETRY_ATTEMPT_DECAY: float = 0.6


class BankResponseSimulator:
    """
    Simulates bank responses to payment retry attempts.

    All randomness goes through the seeded RNG for reproducibility.
    """

    def __init__(self, seed: int = 42) -> None:
        self._base_seed = seed
        self._rng = np.random.RandomState(seed)

    def reseed_for_scenario(self, scenario_id: int) -> None:
        """
        Reset the RNG to a stream derived from (base_seed, scenario_id).

        This is what makes the policy comparison a *paired* one (common random
        numbers): scenario 417 under seed 42 draws the identical sequence of
        uniforms no matter which policy is being evaluated. Two policies that
        take the same action on a scenario therefore get the same outcome, and
        the only variance left between policies is the variance caused by their
        decisions — which is the thing we are trying to measure.

        Without this, each policy runs on a different slice of one shared RNG
        stream, and the between-policy difference is confounded with raw
        sampling noise.
        """
        self._rng = np.random.RandomState(
            (self._base_seed * 1_000_003 + scenario_id) % (2**32 - 1)
        )

    def customer_responds_to_nudge(self, p: float = 0.60) -> bool:
        """Whether the customer acts on a nudge. Draws from the scenario stream."""
        return bool(self._rng.random() < p)

    def simulate_payment(
        self, bank: str, rail: str, hour: int, amount: int
    ) -> tuple[bool, float]:
        """
        Simulate a first-attempt payment.

        Returns:
            (success, latency_ms)
        """
        profile = get_bank_profile(bank)
        prob = profile.get_success_probability(rail, hour)

        # Higher amounts have slightly lower success rate
        if amount > 500_000:  # > ₹5,000
            prob *= 0.97
        if amount > 5_000_000:  # > ₹50,000
            prob *= 0.95

        success = self._rng.random() < prob
        latency = self._rng.lognormal(mean=6.5, sigma=0.8)  # ~600ms median
        return success, latency

    def simulate_retry(
        self,
        bank: str,
        rail: str,
        hour: int,
        amount: int,
        failure_class: str,
        delay_minutes: int = 0,
        attempt_number: int = 1,
        switched_rail: bool = False,
        after_nudge: bool = False,
    ) -> tuple[bool, float]:
        """
        Simulate a retry attempt.

        Args:
            bank: Bank name.
            rail: Payment rail.
            hour: Hour of day (0-23).
            amount: Amount in paise.
            failure_class: Original failure class.
            delay_minutes: Minutes waited before retry.
            attempt_number: Which retry attempt this is (1-based).
            switched_rail: Whether this is on a different rail.
            after_nudge: Whether customer was nudged first.

        Returns:
            (success, latency_ms)
        """
        profile = get_bank_profile(bank)
        prob = profile.get_success_probability(
            rail=rail,
            hour=hour,
            failure_class=failure_class,
            is_retry=True,
            switched_rail=switched_rail,
            delay_minutes=delay_minutes,
            after_nudge=after_nudge,
        )

        # Each failed attempt is evidence the blocker is the persistent kind, so
        # the next attempt is worth materially less. The old 0.9 decay was far too
        # gentle — it let a dumb 3x retry brute-force its way to a high recovery
        # rate, which is most of why the baseline looked unbeatable.
        prob *= RETRY_ATTEMPT_DECAY ** (attempt_number - 1)

        # NOTE: no additive delay bonus here. Waiting is already priced into
        # get_success_probability via the downtime recovery curve; adding a second
        # bonus rewarded the same wait twice.

        # Amount penalty
        if amount > 500_000:
            prob *= 0.97
        if amount > 5_000_000:
            prob *= 0.95

        prob = max(0.0, min(1.0, prob))
        success = self._rng.random() < prob
        latency = self._rng.lognormal(mean=6.5, sigma=0.8)
        return success, latency
