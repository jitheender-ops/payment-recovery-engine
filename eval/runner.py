"""
Eval harness runner — THE MOST IMPORTANT FILE.

Runs all policies over synthetic failure scenarios and produces the
results table that goes in the README and pitch deck.

Usage:
    python -m eval.runner --scenarios 5000 --seeds 5
    python -m eval.runner --skip-llm  # fast, no API keys needed
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from eval.metrics import compute_all_metrics, format_results_table, format_results_table_markdown
from eval.policies.fixed_retry import FixedRetryPolicy
from eval.policies.no_retry import NoRetryPolicy
from eval.policies.xgboost_policy import XGBoostPolicy
from eval.scenario_generator import ScenarioGenerator
from eval.simulator import BankResponseSimulator

console = Console()

NON_RETRYABLE = {"hard_decline", "fraud_block", "customer_cancelled", "invalid_card", "expired_instrument"}


def _json_safe(obj):
    """Replace Infinity/NaN with null so the output is valid JSON for any parser."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    return obj


class EvalRunner:
    """Runs all policies over synthetic scenarios and computes metrics."""

    def __init__(
        self,
        n_scenarios: int = 5000,
        n_seeds: int = 5,
        output_dir: str = "eval/results",
        skip_llm: bool = False,
    ) -> None:
        self.n_scenarios = n_scenarios
        self.n_seeds = n_seeds
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.skip_llm = skip_llm
        self.paired: dict = {}

    def run_policy(
        self,
        policy_name: str,
        policy,
        scenarios: pd.DataFrame,
        simulator: BankResponseSimulator,
    ) -> pd.DataFrame:
        """Run a single policy over all scenarios."""
        results = []

        for _, scenario in scenarios.iterrows():
            # Common random numbers: every policy sees the identical draw
            # sequence for this scenario. See BankResponseSimulator.reseed_for_scenario.
            simulator.reseed_for_scenario(int(scenario["scenario_id"]))

            recovered = False
            attempts = 0
            total_delay = 0
            max_attempts = 5

            for attempt in range(max_attempts):
                decision = policy.decide(scenario, attempt)
                action = decision.get("action", "abandon")

                if action == "abandon":
                    break

                attempts += 1
                delay = decision.get("delay_minutes", 0)
                total_delay += delay
                rail = decision.get("rail", scenario["method"])
                hour = (scenario["hour_of_day"] + (delay // 60)) % 24
                switched = rail != scenario["method"]
                after_nudge = action == "nudge_customer"

                if action == "nudge_customer":
                    # Nudge adds delay, then we simulate a customer-initiated retry
                    delay = max(delay, 30)  # assume 30min for customer to act
                    total_delay += 30
                    # 60% chance customer retries after nudge
                    if not simulator.customer_responds_to_nudge(0.60):
                        continue  # customer didn't retry

                success, _ = simulator.simulate_retry(
                    bank=scenario["bank"],
                    rail=rail,
                    hour=hour,
                    amount=scenario["amount"],
                    failure_class=scenario["failure_class"],
                    delay_minutes=delay,
                    attempt_number=attempt + 1,
                    switched_rail=switched,
                    after_nudge=after_nudge,
                )

                if success:
                    recovered = True
                    break

            results.append({
                "scenario_id": scenario["scenario_id"],
                "policy": policy_name,
                "attempts": attempts,
                "recovered": recovered,
                "is_retryable": scenario["is_retryable"],
                "false_retry": attempts > 0 and not scenario["is_retryable"],
                "time_to_recovery_minutes": total_delay if recovered else 0,
                "amount": scenario["amount"],
            })

        return pd.DataFrame(results)

    def run_with_variance(self) -> dict[str, dict]:
        """Run all policies across multiple seeds and compute mean ± std."""
        policies_factory = {
            "No Retry": lambda: NoRetryPolicy(),
            "Fixed 3-Retry": lambda: FixedRetryPolicy(max_retries=3, delay_minutes=15),
            "XGBoost/Rules": lambda: XGBoostPolicy(),
        }

        if not self.skip_llm:
            try:
                from eval.policies.llm_policy import LLMPolicy
                policies_factory["LLM Agent"] = lambda: LLMPolicy()
            except Exception:
                console.print("[yellow]Skipping LLM policy (import failed)[/yellow]")

        all_seed_metrics: dict[str, list[dict]] = {name: [] for name in policies_factory}
        # Per-scenario outcomes, kept for the paired comparison below.
        per_scenario: dict[str, list[pd.DataFrame]] = {name: [] for name in policies_factory}

        for seed_idx in range(self.n_seeds):
            seed = 42 + seed_idx
            console.print(f"\n[bold blue]═══ Seed {seed} ({seed_idx + 1}/{self.n_seeds}) ═══[/bold blue]")

            gen = ScenarioGenerator(seed=seed)
            scenarios = gen.generate(self.n_scenarios)
            simulator = BankResponseSimulator(seed=seed)

            for policy_name, factory in policies_factory.items():
                t0 = time.time()
                policy = factory()
                results_df = self.run_policy(policy_name, policy, scenarios, simulator)
                metrics = compute_all_metrics(results_df)
                elapsed = time.time() - t0

                all_seed_metrics[policy_name].append(metrics)
                per_scenario[policy_name].append(results_df)
                console.print(
                    f"  {policy_name}: recovery={metrics['recovery_rate_%']:.1f}%, "
                    f"cost={metrics['retry_cost_avg']:.2f}, "
                    f"₹{metrics['₹_per_₹1Cr_failed']:,.0f}/Cr "
                    f"[dim]({elapsed:.1f}s)[/dim]"
                )

        # Aggregate: mean ± std
        aggregated: dict[str, dict] = {}
        for policy_name, seed_metrics_list in all_seed_metrics.items():
            agg = {}
            for key in seed_metrics_list[0]:
                values = [m[key] for m in seed_metrics_list]
                mean_val = np.mean(values)
                std_val = np.std(values)
                agg[key] = round(float(mean_val), 2)
                agg[f"{key}_std"] = round(float(std_val), 2)
            aggregated[policy_name] = agg

        self.paired = self._paired_comparison(per_scenario)
        return aggregated

    # ── Paired comparison ────────────────────────────────────────────────
    BASELINE = "Fixed 3-Retry"

    def _paired_comparison(self, per_scenario: dict[str, list[pd.DataFrame]]) -> dict:
        """
        Compare each policy against the baseline scenario-by-scenario.

        Because the simulator uses common random numbers (same scenario + same
        seed => same draw sequence for every policy), the per-scenario outcomes
        line up one-to-one and can be differenced directly. That removes the
        shared sampling noise and gives a confidence interval tight enough to
        say whether a sub-1pp difference is real.

        Comparing the two independent means instead — 73.5 ± 0.8 vs 72.7 ± 0.9 —
        cannot resolve a difference this small, which is exactly why the
        unpaired version of this table was not evidence of anything.
        """
        if self.BASELINE not in per_scenario or not per_scenario[self.BASELINE]:
            return {}

        base = pd.concat(per_scenario[self.BASELINE], ignore_index=True)
        out: dict[str, dict] = {}

        for policy_name, frames in per_scenario.items():
            if policy_name == self.BASELINE or not frames:
                continue
            other = pd.concat(frames, ignore_index=True)
            if len(other) != len(base):
                continue

            for label, col in (("recovery_rate_pp", "recovered"), ("retry_cost", "attempts")):
                diff = other[col].astype(float).to_numpy() - base[col].astype(float).to_numpy()
                if col == "recovered":
                    diff = diff * 100  # express as percentage points
                n = len(diff)
                mean_d = float(np.mean(diff))
                # Standard error of the paired difference.
                se = float(np.std(diff, ddof=1)) / np.sqrt(n)
                ci = 1.96 * se
                out.setdefault(policy_name, {})[label] = {
                    "mean_delta": round(mean_d, 3),
                    "ci95_halfwidth": round(ci, 3),
                    "ci95": [round(mean_d - ci, 3), round(mean_d + ci, 3)],
                    "n_paired": n,
                    # Significant only if the interval excludes zero.
                    "significant": bool(abs(mean_d) > ci),
                }

        return out

    def save_results(self, results: dict, output_dir: Path | None = None) -> None:
        """Save results to JSON and generate markdown table."""
        out = output_dir or self.output_dir

        # JSON. allow_nan=False so Infinity/NaN raise here instead of silently
        # producing a file that only Python can parse — sanitize first.
        json_path = out / "eval_results.json"
        payload = {"policies": _json_safe(results), "paired_vs_baseline": _json_safe(self.paired)}
        with open(json_path, "w") as f:
            json.dump(payload, f, indent=2, allow_nan=False)

        # Markdown table
        md_table = format_results_table_markdown(results)
        md_path = out / "eval_results.md"
        with open(md_path, "w") as f:
            f.write("# Eval Harness Results\n\n")
            f.write(f"Scenarios: {self.n_scenarios} | Seeds: {self.n_seeds}\n\n")
            f.write(md_table)
            f.write("\n\n*Variance (±σ) shown in JSON results.*\n")
            f.write(self._paired_markdown())

        # CSV summary
        rows = []
        for policy, metrics in results.items():
            row = {"policy": policy}
            row.update(metrics)
            rows.append(row)
        csv_path = out / "eval_results.csv"
        pd.DataFrame(rows).to_csv(csv_path, index=False)

        console.print(f"\n[green]Results saved to {out}/[/green]")

    def _paired_markdown(self) -> str:
        """Render the paired comparison as markdown."""
        if not self.paired:
            return ""
        lines = [
            f"\n## Paired comparison vs. {self.BASELINE}\n",
            "Common random numbers: each scenario draws the identical random sequence",
            "under every policy, so outcomes are differenced one-to-one. A difference is",
            "only called real when the 95% CI excludes zero.\n",
            "| Policy | Metric | Δ vs baseline | 95% CI | n | Real? |",
            "|---|---|---|---|---|---|",
        ]
        for policy, metrics in self.paired.items():
            for metric, d in metrics.items():
                lines.append(
                    f"| {policy} | {metric} | {d['mean_delta']:+.3f} | "
                    f"[{d['ci95'][0]:+.3f}, {d['ci95'][1]:+.3f}] | {d['n_paired']:,} | "
                    f"{'yes' if d['significant'] else 'no — inside noise'} |"
                )
        return "\n".join(lines) + "\n"

    def print_paired(self) -> None:
        """Print the paired comparison table."""
        if not self.paired:
            return
        table = Table(title=f"Paired vs. {self.BASELINE} (common random numbers)",
                      title_style="bold cyan", show_lines=True)
        table.add_column("Policy", style="bold")
        table.add_column("Metric")
        table.add_column("Δ", justify="right")
        table.add_column("95% CI", justify="right")
        table.add_column("Real?", justify="right")
        for policy, metrics in self.paired.items():
            for metric, d in metrics.items():
                verdict = "[green]yes[/green]" if d["significant"] else "[yellow]no[/yellow]"
                table.add_row(
                    policy, metric, f"{d['mean_delta']:+.3f}",
                    f"[{d['ci95'][0]:+.3f}, {d['ci95'][1]:+.3f}]", verdict,
                )
        console.print(table)

    def print_results(self, results: dict) -> None:
        """Print a professional results table using rich."""
        table = Table(
            title="🔄 Payment Recovery Eval Results",
            title_style="bold cyan",
            show_lines=True,
        )
        table.add_column("Policy", style="bold", min_width=15)
        table.add_column("Recovery %", justify="right")
        table.add_column("Retry Cost", justify="right")
        table.add_column("False-Retry %", justify="right")
        table.add_column("Time-to-Recovery", justify="right")
        table.add_column("₹ per ₹1Cr", justify="right", style="green")

        for policy, m in results.items():
            recovery = f"{m['recovery_rate_%']:.1f} ± {m.get('recovery_rate_%_std', 0):.1f}"
            cost = f"{m['retry_cost_avg']:.2f} ± {m.get('retry_cost_avg_std', 0):.2f}"
            false_r = f"{m['false_retry_rate_%']:.1f} ± {m.get('false_retry_rate_%_std', 0):.1f}"
            ttr = f"{m['time_to_recovery_min']:.0f} ± {m.get('time_to_recovery_min_std', 0):.0f}"
            rev = f"₹{m['₹_per_₹1Cr_failed']:,.0f} ± {m.get('₹_per_₹1Cr_failed_std', 0):,.0f}"
            table.add_row(policy, recovery, cost, false_r, ttr, rev)

        console.print(table)

        # Print headline metric
        if "XGBoost/Rules" in results and "No Retry" in results:
            xgb = results["XGBoost/Rules"]["₹_per_₹1Cr_failed"]
            console.print(
                f"\n[bold green]Headline: ₹{xgb:,.0f} recovered per ₹1Cr of failed volume[/bold green]"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Payment Recovery Eval Harness")
    parser.add_argument("--scenarios", type=int, default=5000, help="Number of scenarios")
    parser.add_argument("--seeds", type=int, default=5, help="Number of random seeds")
    parser.add_argument("--output", type=str, default="eval/results", help="Output directory")
    parser.add_argument("--skip-llm", action="store_true", help="Skip LLM policy (no API keys needed)")
    args = parser.parse_args()

    console.print("[bold cyan]🔄 Payment Failure Recovery — Eval Harness[/bold cyan]")
    console.print(f"Scenarios: {args.scenarios} | Seeds: {args.seeds} | Skip LLM: {args.skip_llm}\n")

    runner = EvalRunner(
        n_scenarios=args.scenarios,
        n_seeds=args.seeds,
        output_dir=args.output,
        skip_llm=args.skip_llm,
    )

    results = runner.run_with_variance()
    runner.print_results(results)
    runner.print_paired()
    runner.save_results(results)


if __name__ == "__main__":
    main()
