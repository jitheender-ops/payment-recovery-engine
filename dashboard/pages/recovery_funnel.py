"""Recovery funnel page."""
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd

st.title("📊 Recovery Funnel")

# Demo data (replace with DB queries when connected)
funnel_data = {
    "Stage": ["Failed Payments", "Classified", "Retryable", "Agent Decided", "Guardrail Passed", "Retry Attempted", "Recovered"],
    "Count": [1247, 1247, 1058, 1058, 892, 892, 427],
}
df = pd.DataFrame(funnel_data)

fig = go.Figure(go.Funnel(
    y=df["Stage"], x=df["Count"],
    textinfo="value+percent initial",
    marker={"color": ["#ff6b6b", "#ee5a24", "#f9ca24", "#6ab04c", "#22a6b3", "#4834d4", "#2ecc71"]},
))
fig.update_layout(title="Recovery Pipeline Funnel", height=500)
st.plotly_chart(fig, use_container_width=True)

# Failure class distribution
st.subheader("Failure Class Distribution")
fc_data = {
    "Class": ["insufficient_funds", "3ds_dropoff", "bank_downtime", "network_error",
              "upi_collect_timeout", "issuer_decline", "payment_timeout", "card_limit_exceeded",
              "invalid_card", "expired_instrument", "fraud_block", "customer_cancelled"],
    "Count": [250, 187, 150, 150, 125, 125, 100, 62, 37, 25, 12, 12],
}
fc_df = pd.DataFrame(fc_data)
fig2 = px.pie(fc_df, values="Count", names="Class", title="Failure Class Breakdown",
              color_discrete_sequence=px.colors.qualitative.Set3)
st.plotly_chart(fig2, use_container_width=True)
