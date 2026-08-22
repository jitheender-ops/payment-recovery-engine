"""
Calibration guards for the bank simulator.

The simulator is the foundation every headline number rests on, and its failure
mode is silent: tweak a modifier, and the whole results table shifts while every
other test still passes. These assert the properties that make the model
defensible rather than any specific figure.
"""

from __future__ import annotations

from eval.bank_profiles import CONDITION_CLEARS, get_bank_profile
from eval.policies.fixed_retry import FixedRetryPolicy
from eval.runner import EvalRunner
from eval.scenario_generator import ScenarioGenerator
from eval.simulator import BankResponseSimulator


def test_blended_recovery_is_in_the_published_band() -> None:
    """
    A dumb 3-retry baseline must land in the 15-30% band that public figures
    report for failed-payment recovery. The earlier model returned ~73%, which
    is the single most challengeable number a payments panel would see.
    """
    runner = EvalRunner(n_scenarios=2000, n_seeds=1)
    scenarios = ScenarioGenerator(seed=42).generate(2000)
    sim = BankResponseSimulator(seed=42)

    results = runner.run_policy("baseline", FixedRetryPolicy(), scenarios, sim)
    rate = results["recovered"].mean() * 100

    assert 15.0 <= rate <= 30.0, f"blended recovery {rate:.1f}% is outside the 15-30% band"


def test_retry_probability_is_far_below_first_attempt_probability() -> None:
    """
    The core modelling fix: a retry is not a fresh payment. Whatever the bank's
    baseline approval rate, retrying an already-failed payment must be much less
    likely to succeed than a first attempt on the same rail.
    """
    profile = get_bank_profile("HDFC")
    first = profile.get_success_probability(rail="upi", hour=12)
    retry = profile.get_success_probability(
        rail="upi", hour=12, failure_class="insufficient_funds", is_retry=True
    )
    assert retry < first / 5, f"retry {retry:.3f} too close to first attempt {first:.3f}"


def test_waiting_helps_downtime_and_nothing_else() -> None:
    """Delay is only a lever for time-based blockers, and it is applied once."""
    profile = get_bank_profile("SBI")

    def p(failure_class: str, delay: int) -> float:
        return profile.get_success_probability(
            rail="card", hour=12, failure_class=failure_class,
            is_retry=True, delay_minutes=delay,
        )

    assert p("bank_downtime", 120) > p("bank_downtime", 0)
    # A wait does not top up someone's bank balance.
    assert p("insufficient_funds", 120) == p("insufficient_funds", 0)
    # Waiting can never push past the switched-rail ceiling.
    ceiling = profile.get_success_probability(
        rail="card", hour=12, failure_class="bank_downtime",
        is_retry=True, switched_rail=True,
    )
    assert p("bank_downtime", 100_000) <= ceiling + 1e-9


def test_unmodelled_failure_class_is_pessimistic() -> None:
    """An unknown blocker must not inherit the full baseline approval rate."""
    profile = get_bank_profile("HDFC")
    assert "meteor_strike" not in CONDITION_CLEARS
    unknown = profile.get_success_probability(
        rail="upi", hour=12, failure_class="meteor_strike", is_retry=True
    )
    assert unknown < profile.get_success_probability(rail="upi", hour=12) / 5
