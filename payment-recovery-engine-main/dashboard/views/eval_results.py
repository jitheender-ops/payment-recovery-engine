"""Eval results page — displays eval harness output."""
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def render() -> None:
    st.title("Eval Harness Results")
    st.markdown("<p style='color: #64748B; font-size: 1.1rem;'>Comparing LLM and XGBoost policy performance.</p>", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    results_path = Path("eval/results/eval_results.json")

    if not results_path.exists():
        st.info("No eval results found. Run the eval harness first:")
        st.code("python -m eval.runner --scenarios 5000 --seeds 5 --skip-llm", language="bash")
        return

    with open(results_path) as f:
        results = json.load(f)

    # Headline metric
    if "XGBoost/Rules" in results:
        xgb_rev = results["XGBoost/Rules"].get("₹_per_₹1Cr_failed", 0)
        xgb_rate = results["XGBoost/Rules"].get("recovery_rate_%", 0)
        
        st.markdown(f"""
        <div style="background-color: #F0FDF4; border: 1px solid #10B981; border-radius: 12px; padding: 20px; margin-bottom: 24px;">
            <h3 style="color: #065F46; margin-top: 0;">Production Model Baseline</h3>
            <p style="font-size: 1.2rem; color: #047857; margin-bottom: 0;">
                <b>₹{xgb_rev:,.0f}</b> recovered per ₹1Cr of failed volume at <b>{xgb_rate:.1f}%</b> recovery rate
            </p>
        </div>
        """, unsafe_allow_html=True)

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
    st.markdown("<br>", unsafe_allow_html=True)

    # Bar charts
    col1, col2 = st.columns(2)
    policies = list(results.keys())
    
    # Razorpay palette for evals
    RAZORPAY_COLORS = ['#2563EB', '#6366F1', '#10B981', '#F59E0B']

    with col1:
        recovery_vals = [results[p].get("recovery_rate_%", 0) for p in policies]
        recovery_err = [results[p].get("recovery_rate_%_std", 0) for p in policies]
        fig = go.Figure(go.Bar(
            x=policies,
            y=recovery_vals,
            error_y={"type": "data", "array": recovery_err, "color": "#475569"},
            marker_color=RAZORPAY_COLORS[: len(policies)],
            marker_line_width=0
        ))
        fig.update_layout(
            title="Recovery Rate by Policy", 
            yaxis_title="%", 
            height=400,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
        )
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        rev_vals = [results[p].get("₹_per_₹1Cr_failed", 0) for p in policies]
        rev_err = [results[p].get("₹_per_₹1Cr_failed_std", 0) for p in policies]
        fig2 = go.Figure(go.Bar(
            x=policies,
            y=rev_vals,
            error_y={"type": "data", "array": rev_err, "color": "#475569"},
            marker_color=RAZORPAY_COLORS[: len(policies)],
            marker_line_width=0
        ))
        fig2.update_layout(
            title="₹ Recovered per ₹1Cr Failed", 
            yaxis_title="₹", 
            height=400,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
        )
        st.plotly_chart(fig2, use_container_width=True)
