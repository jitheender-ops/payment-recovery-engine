"""Bank breakdown page."""
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
import numpy as np


def render() -> None:
    """Render this page. Called by dashboard/app.py."""

    st.title("🏦 Bank Breakdown")

    banks = ["SBI", "HDFC", "ICICI", "Axis", "Kotak", "PNB", "BOB", "Yes Bank", "IndusInd"]
    rails = ["UPI", "Card", "Netbanking", "Wallet"]

    # Generate demo heatmap data
    np.random.seed(42)
    heatmap_data = np.random.uniform(0.75, 0.95, (len(banks), len(rails)))
    heatmap_df = pd.DataFrame(heatmap_data, index=banks, columns=rails)

    st.subheader("Success Rate: Banks × Rails")
    fig = px.imshow(heatmap_df, text_auto=".0%", color_continuous_scale="RdYlGn",
                    labels={"color": "Success Rate"}, aspect="auto")
    fig.update_layout(height=400)
    st.plotly_chart(fig, use_container_width=True)

    # Recovery rate by bank
    st.subheader("Recovery Rate by Bank")
    recovery_rates = np.random.uniform(25, 45, len(banks))
    fig2 = px.bar(x=banks, y=recovery_rates, labels={"x": "Bank", "y": "Recovery Rate (%)"},
                  color=recovery_rates, color_continuous_scale="Viridis")
    fig2.update_layout(height=400)
    st.plotly_chart(fig2, use_container_width=True)

    # Success by hour of day
    st.subheader("Success Rate by Hour of Day")
    hours = list(range(24))
    hour_data = {"Hour": hours}
    for bank in ["SBI", "HDFC", "ICICI"]:
        rates = [80 + 10*np.sin((h-14)*np.pi/12) + np.random.normal(0,2) for h in hours]
        hour_data[bank] = rates
    hour_df = pd.DataFrame(hour_data)
    fig3 = px.line(hour_df, x="Hour", y=["SBI", "HDFC", "ICICI"],
                   labels={"value": "Success Rate (%)", "variable": "Bank"})
    fig3.update_layout(height=400)
    st.plotly_chart(fig3, use_container_width=True)
