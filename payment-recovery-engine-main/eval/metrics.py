"""
Eval harness metrics — the numbers that matter.
"""

from __future__ import annotations

import math

import pandas as pd
from tabulate import tabulate

# Cost charged per retry attempt, in rupees.
#
# Recovery rate on its own makes brute force optimal by construction: if attempts
# are free, the best policy is always "retry everything, always". Pricing an
# attempt is what lets selectivity show up as a win rather than a rounding error.
#
# This default covers gateway/API and ops cost only. It deliberately does NOT
# price the two costs that dominate in practice and that we cannot source a
# defensible number for: penalties merchants incur for a poor decline ratio, and
# customer goodwill lost to repeated failed-payment messages. Both push the true
# figure higher, so treat this as a floor — and see the break-even analysis the
# runner prints, which answers "how expensive would a retry have to be" without
# needing this number to be right.
COST_PER_RETRY_INR: float = 2.0


def recovery_rate(results: pd.DataFrame) -> float:
    """% of scenarios where payment was eventually recovered."""
    if len(results) == 0:
        return 0.0
    return float(results["recovered"].mean()) * 100


def retry_cost(results: pd.DataFrame) -> float:
    """Average retry attempts per scenario (including failures)."""
    if len(results) == 0:
        return 0.0
    return float(results["attempts"].mean())


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
        # Undefined, not infinite: a policy that recovers nothing has no
        # time-to-recovery to report. inf would survive aggregation as
        # inf - inf = nan and print "inf ± nan"; nan is the float that means
        # "no data", passes through mean/std silently, and serialises to
        # null/empty rather than a number no reader can act on.
        return float("nan")
    return float(recovered["time_to_recovery_minutes"].median())


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
    return float(recovered_inr / crores_failed)


def attempts_per_crore(results: pd.DataFrame) -> float:
    """Retry attempts made per ₹1Cr of failed volume."""
    total_failed = results["amount"].sum()
    if total_failed == 0:
        return 0.0
    crores_failed = total_failed / 1_000_000_000
    if crores_failed == 0:
        return 0.0
    return float(results["attempts"].sum()) / float(crores_failed)


def net_revenue_per_crore(
    results: pd.DataFrame, cost_per_retry_inr: float = COST_PER_RETRY_INR
) -> float:
    """
    Revenue recovered per ₹1Cr failed, net of what the retries cost to make.

    This is the metric the headline should quote. The gross figure rewards a
    policy for hammering every failure regardless of whether it could ever
    succeed; the net figure makes that hammering show up as the expense it is.
    """
    return revenue_recovered_per_crore(results) - attempts_per_crore(results) * cost_per_retry_inr


def compute_all_metrics(
    results: pd.DataFrame, cost_per_retry_inr: float = COST_PER_RETRY_INR
) -> dict[str, float]:
    """Compute all metrics and return as a dict."""
    return {
        "recovery_rate_%": round(recovery_rate(results), 2),
        "retry_cost_avg": round(retry_cost(results), 2),
        "false_retry_rate_%": round(false_retry_rate(results), 2),
        "time_to_recovery_min": round(time_to_recovery(results), 1),
        "₹_per_₹1Cr_failed": round(revenue_recovered_per_crore(results), 0),
        "attempts_per_₹1Cr": round(attempts_per_crore(results), 0),
        "net_₹_per_₹1Cr_failed": round(
            net_revenue_per_crore(results, cost_per_retry_inr), 0
        ),
    }


def fmt_minutes(value: float, std: float | None = None) -> str:
    """
    Render a time-to-recovery figure, or an em dash when it is undefined.

    Printing "nan" invites the reader to treat a missing measurement as a
    broken one. A policy that recovered nothing genuinely has no median.
    """
    if not math.isfinite(value):
        return "—"
    if std is None or not math.isfinite(std):
        return f"{value:.0f}"
    return f"{value:.0f} ± {std:.0f}"


def format_results_table_markdown(all_policy_results: dict[str, dict[str, float]]) -> str:
    """Format results as a markdown table."""
    headers = [
        "Policy",
        "Recovery Rate (%)",
        "Retry Cost (avg)",
        "False-Retry (%)",
        "Time-to-Recovery (min)",
        "₹ per ₹1Cr Failed",
        "Net ₹ per ₹1Cr",
    ]
    rows = []
    for policy, metrics in all_policy_results.items():
        row = [
            policy,
            f"{metrics.get('recovery_rate_%', 0):.1f}",
            f"{metrics.get('retry_cost_avg', 0):.2f}",
            f"{metrics.get('false_retry_rate_%', 0):.1f}",
            fmt_minutes(metrics.get("time_to_recovery_min", float("nan"))),
            f"₹{metrics.get('₹_per_₹1Cr_failed', 0):,.0f}",
            f"₹{metrics.get('net_₹_per_₹1Cr_failed', 0):,.0f}",
        ]
        rows.append(row)

    return str(tabulate(rows, headers=headers, tablefmt="github"))
