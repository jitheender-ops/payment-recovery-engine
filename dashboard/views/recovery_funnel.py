"""
Recovery funnel — measured, not illustrated.

This page used to draw a hardcoded funnel ([1247, 1247, 1058, ...]) and a
hardcoded failure-class pie. They looked exactly like real output, which is the
problem: a demo number rendered in the same chart as a live one is
indistinguishable from a live one to whoever is reading it.

Every figure below is a COUNT over the real tables. When there are none, the
page says so rather than inventing a shape.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from dashboard.db import no_data, query_db

# One query, one row, one column per funnel stage — cheaper than seven
# round-trips and guaranteed to describe a single consistent moment.
FUNNEL_SQL = """
SELECT
  (SELECT COUNT(*) FROM payment_failures)                              AS failed,
  (SELECT COUNT(*) FROM payment_failures
     WHERE failure_class IS NOT NULL)                                  AS classified,
  (SELECT COUNT(*) FROM payment_failures WHERE is_retryable)           AS retryable,
  (SELECT COUNT(*) FROM retry_attempts)                                AS decided,
  (SELECT COUNT(*) FROM retry_attempts WHERE guardrail_passed)         AS passed,
  (SELECT COUNT(*) FROM retry_attempts
     WHERE result IN ('success','failed','pending'))                   AS attempted,
  (SELECT COUNT(*) FROM recovery_cases WHERE state = 'recovered')      AS recovered
"""

STAGES = [
    ("Failed Payments", "failed"),
    ("Classified", "classified"),
    ("Retryable", "retryable"),
    ("Agent Decided", "decided"),
    ("Guardrail Passed", "passed"),
    ("Retry Attempted", "attempted"),
    ("Recovered", "recovered"),
]

COLOURS = ["#ff6b6b", "#ee5a24", "#f9ca24", "#6ab04c", "#22a6b3", "#4834d4", "#2ecc71"]


def render() -> None:
    """Render this page. Called by dashboard/app.py."""
    st.title("📊 Recovery Funnel")

    row = query_db(FUNNEL_SQL)
    if row is None or row.empty or int(row["failed"].iloc[0]) == 0:
        no_data("payment failures")
        return

    df = pd.DataFrame(
        {"Stage": [s for s, _ in STAGES], "Count": [int(row[c].iloc[0]) for _, c in STAGES]}
    )

    fig = go.Figure(
        go.Funnel(
            y=df["Stage"],
            x=df["Count"],
            textinfo="value+percent initial",
            marker={"color": COLOURS},
        )
    )
    fig.update_layout(title="Recovery Pipeline Funnel", height=500)
    st.plotly_chart(fig, use_container_width=True)

    # "Recovered" counts cases, every other stage counts payments or attempts.
    # Stated rather than left for the reader to trip over: one case can hold
    # several attempts, so the last bar is not a subset of the one above it.
    st.caption(
        "Stages 1–3 count payment failures, 4–6 count retry attempts, and "
        "'Recovered' counts recovery cases that reached a terminal recovered "
        "state. A case can span several attempts, so the funnel narrows across "
        "different units — read each stage against its own row count."
    )

    st.subheader("Failure Class Distribution")
    fc = query_db(
        "SELECT failure_class AS \"Class\", COUNT(*) AS \"Count\" "
        "FROM payment_failures GROUP BY failure_class ORDER BY 2 DESC"
    )
    if fc is None or fc.empty:
        st.info("No classified failures yet.")
        return
    st.plotly_chart(
        px.pie(
            fc,
            values="Count",
            names="Class",
            title="Failure Class Breakdown",
            color_discrete_sequence=px.colors.qualitative.Set3,
        ),
        use_container_width=True,
    )

    st.subheader("Money")
    money = query_db(
        "SELECT COALESCE(SUM(amount_at_risk),0)/100.0 AS at_risk, "
        "COALESCE(SUM(amount_recovered),0)/100.0 AS recovered, "
        "COALESCE(SUM(CASE WHEN recovered_via_attempt_id IS NOT NULL "
        "THEN amount_recovered ELSE 0 END),0)/100.0 AS attributed "
        "FROM recovery_cases"
    )
    if money is not None and not money.empty:
        c1, c2, c3 = st.columns(3)
        c1.metric("At risk", f"₹{money['at_risk'].iloc[0]:,.0f}")
        c2.metric("Recovered", f"₹{money['recovered'].iloc[0]:,.0f}")
        c3.metric("Attributed to us", f"₹{money['attributed'].iloc[0]:,.0f}")
        # The distinction the headline number lives or dies on.
        st.caption(
            "'Attributed to us' is the subset recovered through a payment link "
            "this engine sent. The rest is money the customer paid on their own "
            "— real revenue, but not the engine's result."
        )
