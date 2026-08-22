"""
Bank breakdown — measured, not simulated.

Every chart on this page used to come from `np.random`: a success-rate heatmap
drawn from `uniform(0.75, 0.95)`, recovery rates from `uniform(25, 45)`, and an
hourly curve from a sine wave plus Gaussian noise. With `np.random.seed(42)` at
the top it was even stable across reloads, so it looked like a real measurement
that simply was not changing.

Everything here is now an aggregate over retry_attempts joined to
payment_failures, and small samples are labelled as small rather than smoothed
into a confident-looking colour.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from dashboard.db import no_data, query_db

# Below this many attempts in a cell, a percentage is noise wearing a number's
# clothes. Shown, but marked — hiding it would misrepresent coverage instead.
MIN_SAMPLE = 5

# Success here means the recovery attempt led to a recovered case, not that the
# Razorpay call returned 200. `retry_attempts.result = 'success'` only says a
# payment link was created; the money question is answered by recovery_cases.
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
    """Render this page. Called by dashboard/app.py."""
    st.title("🏦 Bank Breakdown")

    cells = query_db(BANK_RAIL_SQL)
    if cells is None or cells.empty:
        no_data("retry attempts")
        return

    # ── Banks × rails ────────────────────────────────────────────────────
    st.subheader("Recovery Rate: Banks × Rails")
    cells["rate"] = cells["recovered"] / cells["attempts"]
    grid = cells.pivot(index="bank", columns="rail", values="rate")
    fig = px.imshow(
        grid,
        text_auto=".0%",
        color_continuous_scale="RdYlGn",
        labels={"color": "Recovery Rate"},
        aspect="auto",
        zmin=0,
        zmax=1,
    )
    fig.update_layout(height=400)
    st.plotly_chart(fig, use_container_width=True)

    thin = cells[cells["attempts"] < MIN_SAMPLE]
    if not thin.empty:
        st.caption(
            f"{len(thin)} of {len(cells)} bank×rail cells have fewer than "
            f"{MIN_SAMPLE} attempts. Their percentages are shown as measured and "
            "should not be read as rates yet."
        )
    with st.expander("Underlying counts"):
        st.dataframe(
            cells[["bank", "rail", "attempts", "recovered"]],
            use_container_width=True,
            hide_index=True,
        )

    # ── Per bank ─────────────────────────────────────────────────────────
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
            color_continuous_scale="Viridis",
        )
        fig2.update_layout(height=400)
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("No recovery cases linked to a bank yet.")

    # ── Per hour ─────────────────────────────────────────────────────────
    st.subheader("Attempt Success by Hour of Original Failure")
    by_hour = query_db(BY_HOUR_SQL)
    if by_hour is None or by_hour.empty:
        st.info("No attempts to break down by hour yet.")
        return
    by_hour["rate_pct"] = by_hour["succeeded"] / by_hour["attempts"] * 100
    fig3 = px.line(
        by_hour,
        x="hour",
        y="rate_pct",
        markers=True,
        hover_data=["attempts", "succeeded"],
        labels={"hour": "Hour of day (UTC)", "rate_pct": "Attempt success (%)"},
    )
    fig3.update_layout(height=400)
    st.plotly_chart(fig3, use_container_width=True)
    st.caption(
        "Success here is the retry attempt completing — a payment link created "
        "— not money received. Hours with few attempts will swing wildly; the "
        "hover shows the count behind each point."
    )
