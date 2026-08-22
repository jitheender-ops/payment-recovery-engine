"""
Reporting checks for eval metrics.

These guard against publishing a figure that no reader can act on. A policy
that recovers nothing has no median time-to-recovery, and the results table is
the one artifact anyone actually reads — "inf ± nan" in it reads as broken
arithmetic rather than as the absence of a measurement.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from eval.metrics import compute_all_metrics, fmt_minutes, time_to_recovery


def _frame(recovered: bool) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "recovered": recovered,
                "attempts": 0 if not recovered else 1,
                "is_retryable": True,
                "time_to_recovery_minutes": 15 if recovered else 0,
                "amount": 100_000,
            }
        ]
    )


def test_no_recovery_reports_undefined_not_infinite() -> None:
    """
    inf would survive aggregation as inf - inf = nan and print "inf ± nan",
    emitting a numpy RuntimeWarning on the way. nan is the float that means
    "no data" and serialises to null/empty instead.
    """
    value = time_to_recovery(_frame(recovered=False))
    assert math.isnan(value)
    assert not math.isinf(value)


def test_recovery_reports_the_median() -> None:
    assert time_to_recovery(_frame(recovered=True)) == 15


def test_undefined_time_renders_as_a_dash() -> None:
    assert fmt_minutes(float("nan")) == "—"
    assert fmt_minutes(float("inf")) == "—"
    assert fmt_minutes(15.0) == "15"
    assert fmt_minutes(15.0, 2.0) == "15 ± 2"
    # A defined mean with an undefined spread still reports the mean.
    assert fmt_minutes(15.0, float("nan")) == "15"


def test_undefined_time_survives_aggregation_without_a_warning(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """A no-recovery policy must not emit numpy warnings through mean/std."""
    import numpy as np

    metrics = compute_all_metrics(_frame(recovered=False))
    values = [metrics["time_to_recovery_min"]] * 5
    assert math.isnan(float(np.mean(values)))
    assert math.isnan(float(np.std(values)))
    assert [w for w in recwarn if w.category is RuntimeWarning] == []
