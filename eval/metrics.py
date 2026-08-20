"""
Eval harness metrics — the numbers that matter.
"""

from __future__ import annotations

import pandas as pd
from tabulate import tabulate


def recovery_rate(results: pd.DataFrame) -> float:
    """% of scenarios where payment was eventually recovered."""
    if len(results) == 0:
        return 0.0
    return results["recovered"].mean() * 100


def retry_cost(results: pd.DataFrame) -> float:
    """Average retry attempts per scenario (including failures)."""
    if len(results) == 0:
        return 0.0
    return results["attempts"].mean()


def false_retry_rate(results: pd.DataFrame) -> float:
    """% of retry attempts on non-retryable failures."""
    retried = results[results["attempts"] > 0]
    if len(retried) == 0:
        return 0.0
    false_retries = retried[~retried["is_retryable"]]
    return len(false_retries) / len(retried) * 100


def time_to_recovery(results: pd.DataFrame) -> float:
    """Median minutes from first failure to recovery (recovered payments only)."""
    recovered = results[results["recovered"]]
    if len(recovered) == 0:
        return float("inf")
    return recovered["time_to_recovery_minutes"].median()


def revenue_recovered_per_crore(results: pd.DataFrame) -> float:
    """₹ recovered per ₹1Cr of failed volume."""
    total_failed = results["amount"].sum()
    if total_failed == 0:
        return 0.0
    total_recovered = results[results["recovered"]]["amount"].sum()
    # Scale to per-crore (1Cr = 10,000,000 INR = 1,000,000,000 paise)
    crores_failed = total_failed / 1_000_000_000
    recovered_inr = total_recovered / 100  # paise to INR
    if crores_failed == 0:
        return 0.0
    return recovered_inr / crores_failed


def compute_all_metrics(results: pd.DataFrame) -> dict[str, float]:
    """Compute all metrics and return as a dict."""
    return {
        "recovery_rate_%": round(recovery_rate(results), 2),
        "retry_cost_avg": round(retry_cost(results), 2),
        "false_retry_rate_%": round(false_retry_rate(results), 2),
        "time_to_recovery_min": round(time_to_recovery(results), 1),
        "₹_per_₹1Cr_failed": round(revenue_recovered_per_crore(results), 0),
    }


def format_results_table(all_policy_results: dict[str, dict]) -> str:
    """Format results as a professional ASCII table."""
    headers = [
        "Policy",
        "Recovery Rate (%)",
        "Retry Cost (avg)",
        "False-Retry (%)",
        "Time-to-Recovery (min)",
        "₹ per ₹1Cr Failed",
    ]
    rows = []
    for policy, metrics in all_policy_results.items():
        row = [
            policy,
            f"{metrics.get('recovery_rate_%', 0):.1f}",
            f"{metrics.get('retry_cost_avg', 0):.2f}",
            f"{metrics.get('false_retry_rate_%', 0):.1f}",
            f"{metrics.get('time_to_recovery_min', 0):.0f}",
            f"₹{metrics.get('₹_per_₹1Cr_failed', 0):,.0f}",
        ]
        rows.append(row)

    return tabulate(rows, headers=headers, tablefmt="grid")


def format_results_table_markdown(all_policy_results: dict[str, dict]) -> str:
    """Format results as a markdown table."""
    headers = [
        "Policy",
        "Recovery Rate (%)",
        "Retry Cost (avg)",
        "False-Retry (%)",
        "Time-to-Recovery (min)",
        "₹ per ₹1Cr Failed",
    ]
    rows = []
    for policy, metrics in all_policy_results.items():
        row = [
            policy,
            f"{metrics.get('recovery_rate_%', 0):.1f}",
            f"{metrics.get('retry_cost_avg', 0):.2f}",
            f"{metrics.get('false_retry_rate_%', 0):.1f}",
            f"{metrics.get('time_to_recovery_min', 0):.0f}",
            f"₹{metrics.get('₹_per_₹1Cr_failed', 0):,.0f}",
        ]
        rows.append(row)

    return tabulate(rows, headers=headers, tablefmt="github")
