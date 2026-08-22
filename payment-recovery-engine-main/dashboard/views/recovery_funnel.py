"""
Recovery funnel — measured, not illustrated.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from dashboard.db import no_data, query_db

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

# Razorpay Blues & Greens
COLOURS = ['#2563EB', '#3B82F6', '#6366F1', '#8B5CF6', '#A855F7', '#10B981', '#059669']

def render() -> None:
    st.title("Recovery Funnel")
    st.markdown("<p style='color: #64748B; font-size: 1.1rem;'>End-to-end conversion tracking for failed payments.</p>", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    row = query_db(FUNNEL_SQL)
    if row is None or row.empty or int(row["failed"].iloc[0]) == 0:
        no_data("payment failures")
        return

    df = pd.DataFrame(
        {"Stage": [s for s, _ in STAGES], "Count": [int(row[c].iloc[0]) for _, c in STAGES]}
    )

    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader("Pipeline Funnel")
        fig = go.Figure(
            go.Funnel(
                y=df["Stage"],
                x=df["Count"],
                textinfo="value+percent initial",
                marker={"color": COLOURS},
                connector={"line": {"color": "#E2E8F0", "width": 2}}
            )
        )
        fig.update_layout(
            height=500,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            margin=dict(l=20, r=20, t=40, b=20)
        )
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.subheader("Monetary Impact")
        money = query_db(
            "SELECT COALESCE(SUM(amount_at_risk),0)/100.0 AS at_risk, "
            "COALESCE(SUM(amount_recovered),0)/100.0 AS recovered, "
            "COALESCE(SUM(CASE WHEN recovered_via_attempt_id IS NOT NULL "
            "THEN amount_recovered ELSE 0 END),0)/100.0 AS attributed "
            "FROM recovery_cases"
        )
        if money is not None and not money.empty:
            st.metric("Total At Risk", f"₹{money['at_risk'].iloc[0]:,.0f}")
            st.metric("Total Recovered", f"₹{money['recovered'].iloc[0]:,.0f}")
            st.metric("Attributed to Agent", f"₹{money['attributed'].iloc[0]:,.0f}", help="Recovered strictly via our generated payment links.")
        else:
            st.info("No monetary data available yet.")
            
        st.markdown("<hr>", unsafe_allow_html=True)
        st.subheader("Failure Classification")
        fc = query_db(
            "SELECT failure_class AS \"Class\", COUNT(*) AS \"Count\" "
            "FROM payment_failures GROUP BY failure_class ORDER BY 2 DESC"
        )
        if fc is not None and not fc.empty:
            fig_pie = px.pie(
                fc,
                values="Count",
                names="Class",
                color_discrete_sequence=['#2563EB', '#3B82F6', '#60A5FA', '#93C5FD', '#10B981', '#34D399', '#6EE7B7'],
                hole=0.7
            )
            fig_pie.update_layout(
                showlegend=False,
                margin=dict(l=0, r=0, t=10, b=10),
                height=200,
                paper_bgcolor='rgba(0,0,0,0)'
            )
            st.plotly_chart(fig_pie, use_container_width=True)
        else:
            st.info("No classified failures yet.")

    st.caption(
        "Stages 1–3 count payment failures, 4–6 count retry attempts, and "
        "'Recovered' counts terminal recovered states."
    )
