"""
Bank breakdown — measured, not simulated.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from dashboard.db import no_data, query_db

MIN_SAMPLE = 5

BANK_RAIL_SQL = """
SELECT
  COALESCE(pf.bank, pf.card_issuer, 'unknown') AS bank,
  pf.method                                     AS rail,
  COUNT(*)                                      AS attempts,
  SUM(CASE WHEN rc.state = 'recovered' THEN 1 ELSE 0 END) AS recovered
FROM retry_attempts ra
JOIN payment_failures pf ON ra.payment_failure_id = pf.id
LEFT JOIN recovery_cases rc ON ra.recovery_case_id = rc.id
GROUP BY 1, 2
"""

BY_BANK_SQL = """
SELECT
  COALESCE(pf.bank, pf.card_issuer, 'unknown') AS bank,
  COUNT(DISTINCT rc.id)                        AS cases,
  SUM(CASE WHEN rc.state = 'recovered' THEN 1 ELSE 0 END) AS recovered,
  COALESCE(SUM(rc.amount_recovered), 0) / 100.0 AS rupees
FROM recovery_cases rc
JOIN payment_failures pf ON pf.payment_id = rc.subject_ref
GROUP BY 1
ORDER BY 4 DESC
"""

BY_HOUR_SQL = """
SELECT
  EXTRACT(HOUR FROM pf.failed_at) AS hour,
  COUNT(*)                        AS attempts,
  SUM(CASE WHEN ra.result = 'success' THEN 1 ELSE 0 END) AS succeeded
FROM retry_attempts ra
JOIN payment_failures pf ON ra.payment_failure_id = pf.id
GROUP BY 1
ORDER BY 1
"""


def render() -> None:
    st.title("Bank Breakdown")
    st.markdown("<p style='color: #64748B; font-size: 1.1rem;'>Detailed recovery analytics by financial institution.</p>", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    cells = query_db(BANK_RAIL_SQL)
    if cells is None or cells.empty:
        no_data("retry attempts")
        return

    # ── Banks × rails ────────────────────────────────────────────────────
    st.subheader("Recovery Rate Heatmap: Banks × Rails")
    cells["rate"] = cells["recovered"] / cells["attempts"]
    grid = cells.pivot(index="bank", columns="rail", values="rate")
    
    fig = px.imshow(
        grid,
        text_auto=".0%",
        color_continuous_scale="Blues", # Razorpay blues
        labels={"color": "Recovery Rate"},
        aspect="auto",
        zmin=0,
        zmax=1,
    )
    fig.update_layout(
        height=400,
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        margin=dict(l=0, r=0, t=30, b=0)
    )
    st.plotly_chart(fig, use_container_width=True)

    thin = cells[cells["attempts"] < MIN_SAMPLE]
    if not thin.empty:
        st.caption(
            f"{len(thin)} of {len(cells)} bank×rail cells have fewer than "
            f"{MIN_SAMPLE} attempts. Percentages are shown as measured."
        )

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Recovery by Bank")
        by_bank = query_db(BY_BANK_SQL)
        if by_bank is not None and not by_bank.empty:
            by_bank["rate_pct"] = (
                by_bank["recovered"] / by_bank["cases"].replace(0, pd.NA) * 100
            ).fillna(0)
            
            fig2 = px.bar(
                by_bank,
                x="bank",
                y="rate_pct",
                hover_data=["cases", "recovered", "rupees"],
                labels={"bank": "Bank", "rate_pct": "Recovery Rate (%)"},
                color="rate_pct",
                color_continuous_scale="Blues",
            )
            fig2.update_layout(
                height=350,
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                margin=dict(l=0, r=0, t=30, b=0),
                coloraxis_showscale=False
            )
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("No recovery cases linked to a bank yet.")

    with col2:
        st.subheader("Attempt Success by Hour (UTC)")
        by_hour = query_db(BY_HOUR_SQL)
        if by_hour is not None and not by_hour.empty:
            by_hour["rate_pct"] = by_hour["succeeded"] / by_hour["attempts"] * 100
            
            fig3 = px.line(
                by_hour,
                x="hour",
                y="rate_pct",
                markers=True,
                hover_data=["attempts", "succeeded"],
                labels={"hour": "Hour", "rate_pct": "Success (%)"},
            )
            fig3.update_traces(line_color='#2563EB', marker=dict(size=8, color='#1E40AF'))
            fig3.update_layout(
                height=350,
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                margin=dict(l=0, r=0, t=30, b=0)
            )
            st.plotly_chart(fig3, use_container_width=True)
        else:
            st.info("No hourly data yet.")
