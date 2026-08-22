"""
Common-random-numbers checks for the eval harness.

These guard the property that makes the policy comparison meaningful: the same
scenario must draw the same randomness under every policy. If that breaks, the
paired confidence intervals silently become nonsense while still looking fine.
"""

from __future__ import annotations

import pandas as pd

from eval.policies.fixed_retry import FixedRetryPolicy
from eval.runner import EvalRunner
from eval.scenario_generator import ScenarioGenerator
from eval.simulator import BankResponseSimulator


def _setup(n: int = 200) -> tuple[EvalRunner, pd.DataFrame]:
    runner = EvalRunner(n_scenarios=n, n_seeds=1)
    scenarios = ScenarioGenerator(seed=7).generate(n)
    return runner, scenarios


def test_rerunning_a_policy_reproduces_every_outcome() -> None:
    """
    The simulator is deliberately reused without being rebuilt, so its stream is
    already advanced on the second call. Per-scenario reseeding must make that
    irrelevant — which is exactly what the old code got wrong.
    """
    runner, scenarios = _setup()
    sim = BankResponseSimulator(seed=7)

    first = runner.run_policy("p", FixedRetryPolicy(), scenarios, sim)
    second = runner.run_policy("p", FixedRetryPolicy(), scenarios, sim)

    assert first["recovered"].tolist() == second["recovered"].tolist()
    assert first["attempts"].tolist() == second["attempts"].tolist()


def test_identical_policies_have_exactly_zero_paired_delta() -> None:
    """Two policies that decide identically must difference to exactly zero."""
    runner, scenarios = _setup()
    sim = BankResponseSimulator(seed=7)

    base = runner.run_policy(runner.BASELINE, FixedRetryPolicy(), scenarios, sim)
    clone = runner.run_policy("Clone", FixedRetryPolicy(), scenarios, sim)

    paired = runner._paired_comparison({runner.BASELINE: [base], "Clone": [clone]})

    assert paired["Clone"]["recovery_rate_pp"]["mean_delta"] == 0.0
    assert paired["Clone"]["retry_cost"]["mean_delta"] == 0.0
    assert paired["Clone"]["recovery_rate_pp"]["significant"] is False


def test_different_seeds_produce_different_streams() -> None:
    """Sanity: reseeding is per (base_seed, scenario), not a constant."""
    a, b = BankResponseSimulator(seed=1), BankResponseSimulator(seed=2)
    a.reseed_for_scenario(0)
    b.reseed_for_scenario(0)
    draws_a = [a.simulate_payment("HDFC", "upi", 12, 50_000)[0] for _ in range(50)]
    draws_b = [b.simulate_payment("HDFC", "upi", 12, 50_000)[0] for _ in range(50)]
    assert draws_a != draws_b
