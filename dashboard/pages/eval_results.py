"""Eval results page — displays eval harness output."""
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def render() -> None:
    """Render this page. Called by dashboard/app.py."""

    st.title("📈 Eval Harness Results")

    results_path = Path("eval/results/eval_results.json")

    if not results_path.exists():
        st.info("No eval results found. Run the eval harness first:")
        st.code("python -m eval.runner --scenarios 5000 --seeds 5 --skip-llm", language="bash")
        st.stop()

    with open(results_path) as f:
        results = json.load(f)

    # Main metrics table
    st.subheader("Policy Comparison")
    rows = []
    for policy, metrics in results.items():
        rows.append({
            "Policy": policy,
            "Recovery Rate (%)": (
                f"{metrics.get('recovery_rate_%', 0):.1f} "
                f"± {metrics.get('recovery_rate_%_std', 0):.1f}"
            ),
            "Retry Cost (avg)": f"{metrics.get('retry_cost_avg', 0):.2f}",
            "False-Retry (%)": f"{metrics.get('false_retry_rate_%', 0):.1f}",
            "₹ per ₹1Cr Failed": f"₹{metrics.get('₹_per_₹1Cr_failed', 0):,.0f}",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # Bar charts
    col1, col2 = st.columns(2)
    policies = list(results.keys())

    with col1:
        recovery_vals = [results[p].get("recovery_rate_%", 0) for p in policies]
        recovery_err = [results[p].get("recovery_rate_%_std", 0) for p in policies]
        fig = go.Figure(go.Bar(
            x=policies,
            y=recovery_vals,
            error_y={"type": "data", "array": recovery_err},
            marker_color=["#e74c3c", "#f39c12", "#3498db", "#2ecc71"][: len(policies)],
        ))
        fig.update_layout(title="Recovery Rate by Policy", yaxis_title="%", height=400)
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        rev_vals = [results[p].get("₹_per_₹1Cr_failed", 0) for p in policies]
        rev_err = [results[p].get("₹_per_₹1Cr_failed_std", 0) for p in policies]
        fig2 = go.Figure(go.Bar(
            x=policies,
            y=rev_vals,
            error_y={"type": "data", "array": rev_err},
            marker_color=["#e74c3c", "#f39c12", "#3498db", "#2ecc71"][: len(policies)],
        ))
        fig2.update_layout(title="₹ Recovered per ₹1Cr Failed", yaxis_title="₹", height=400)
        st.plotly_chart(fig2, use_container_width=True)

    # Headline metric
    if "XGBoost/Rules" in results:
        xgb_rev = results["XGBoost/Rules"].get("₹_per_₹1Cr_failed", 0)
        xgb_rate = results["XGBoost/Rules"].get("recovery_rate_%", 0)
        st.success(
            f"**Headline: ₹{xgb_rev:,.0f} recovered per ₹1Cr of failed volume "
            f"at {xgb_rate:.1f}% recovery rate**"
        )
