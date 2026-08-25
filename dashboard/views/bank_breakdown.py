"""
Banks and rails — measured, not simulated.

Every chart here once came from np.random: a success heatmap from
uniform(0.75, 0.95), recovery rates from uniform(25, 45), an hourly curve from a
sine wave plus noise. With np.random.seed(42) at the top it was even stable
across reloads, so it read as a measurement that simply was not changing.

Recovery here means the case reached a recovered state — not that the Razorpay
call returned 200. `retry_attempts.result = 'success'` only says a payment link
was created; whether money arrived is a question only recovery_cases answers.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import theme
from dashboard.db import no_data, query_db

# Below this many attempts a percentage is noise wearing a number's clothes.
# Shown, but marked — hiding it would misrepresent coverage instead.
MIN_SAMPLE = 5

BANK_RAIL_SQL = """
SELECT COALESCE(pf.bank, pf.card_issuer, 'unknown') AS bank,
       pf.method                                    AS rail,
       COUNT(*)                                     AS attempts,
       SUM(CASE WHEN rc.state = 'recovered' THEN 1 ELSE 0 END) AS recovered
FROM retry_attempts ra
JOIN payment_failures pf ON ra.payment_failure_id = pf.id
LEFT JOIN recovery_cases rc ON ra.recovery_case_id = rc.id
GROUP BY 1, 2
"""

BY_BANK_SQL = """
SELECT COALESCE(pf.bank, pf.card_issuer, 'unknown') AS bank,
       COUNT(DISTINCT rc.id)                        AS cases,
       SUM(CASE WHEN rc.state = 'recovered' THEN 1 ELSE 0 END) AS recovered,
       COALESCE(SUM(rc.amount_recovered), 0)        AS paise
FROM recovery_cases rc
JOIN payment_failures pf ON pf.payment_id = rc.subject_ref
GROUP BY 1 ORDER BY 4 DESC
"""

BY_HOUR_SQL = """
SELECT EXTRACT(HOUR FROM pf.failed_at) AS hour,
       COUNT(*)                        AS attempts,
       SUM(CASE WHEN ra.result = 'success' THEN 1 ELSE 0 END) AS succeeded
FROM retry_attempts ra
JOIN payment_failures pf ON ra.payment_failure_id = pf.id
GROUP BY 1 ORDER BY 1
"""


def render() -> None:
    """Render this page. Called by dashboard/app.py."""
    theme.page_header("ROUTING", "Which bank, on which rail")

    cells = query_db(BANK_RAIL_SQL)
    if cells is None or cells.empty:
        no_data("retry attempts")
        return

    # ── Banks x rails ────────────────────────────────────────────────────
    theme.section(
        "Recovery rate by bank and rail",
        "Magnitude on one hue — darker is more recovered. Hover for the counts behind it.",
    )
    cells["rate"] = cells["recovered"] / cells["attempts"]
    grid = cells.pivot(index="bank", columns="rail", values="rate")
    counts = cells.pivot(index="bank", columns="rail", values="attempts")

    fig = go.Figure(
        go.Heatmap(
            z=grid.values,
            x=list(grid.columns),
            y=list(grid.index),
            customdata=counts.values,
            # Sequential: one hue, light to dark. Never a rainbow — a rainbow
            # implies category where this is pure magnitude.
            colorscale=[[i / (len(theme.SEQUENTIAL) - 1), c]
                        for i, c in enumerate(theme.SEQUENTIAL)],
            zmin=0, zmax=1,
            xgap=2, ygap=2,   # the surface shows through, separating the cells
            hovertemplate="<b>%{y} · %{x}</b><br>%{z:.0%} recovered"
                          "<br>%{customdata:,} attempts<extra></extra>",
            colorbar={"outlinewidth": 0, "tickfont": {"color": theme.SLATE, "size": 11},
                      "tickformat": ".0%", "thickness": 10, "len": 0.9},
        )
    )
    # Per-cell labels with PER-CELL ink. Plotly's own annotation colouring is
    # two-tone at best (a documented limitation across its ecosystem), and our
    # ramp spans near-black to bright brass — one fixed text colour fails
    # WCAG on one end or the other. The fill each cell gets is recomputed
    # here through the same ramp, and the label takes ink or paper by
    # measured luminance.
    label_x, label_y, label_text, label_color = [], [], [], []
    for i, bank in enumerate(grid.index):
        for j, rail in enumerate(grid.columns):
            rate = grid.values[i][j]
            if pd.isna(rate):
                continue
            label_x.append(j)
            label_y.append(i)
            label_text.append(f"{rate:.0%}")
            label_color.append(theme.readable_on(theme.ramp_color(float(rate))))
    fig.add_trace(
        go.Scatter(
            x=label_x, y=label_y, mode="text",
            text=label_text,
            textfont={"family": theme.FONT_MONO, "size": 11, "color": label_color},
            showlegend=False, hoverinfo="skip",
        )
    )
    st.plotly_chart(
        theme.style_fig(fig, height=max(280, 42 * len(grid))), width="stretch",
        config=theme.PLOTLY_CONFIG,
    )

    thin = cells[cells["attempts"] < MIN_SAMPLE]
    if not thin.empty:
        st.markdown(
            f"<p style='color:{theme.SLATE};font-size:0.82rem;margin-top:-0.4rem;'>"
            f"{len(thin)} of {len(cells)} cells have fewer than {MIN_SAMPLE} attempts. "
            f"Their percentages are shown as measured and should not be read as rates yet."
            f"</p>",
            unsafe_allow_html=True,
        )
    with st.expander("View as table"):
        st.dataframe(cells[["bank", "rail", "attempts", "recovered"]],
                     width="stretch", hide_index=True)

    # ── Per bank ─────────────────────────────────────────────────────────
    st.divider()
    theme.section("Money recovered by bank", "Brass is money in. Hover for case counts.")
    by_bank = query_db(BY_BANK_SQL)
    if by_bank is not None and not by_bank.empty:
        by_bank = by_bank.sort_values("paise")
        fig2 = go.Figure(
            go.Bar(
                x=[p / 100 for p in by_bank["paise"]],
                y=by_bank["bank"],
                orientation="h",
                marker={"color": theme.BRASS, "line": {"color": theme.INK, "width": 2}},
                text=[theme.compact_inr(p) for p in by_bank["paise"]],
                textposition="outside",
                textfont={"family": theme.FONT_MONO, "color": theme.PAPER, "size": 12},
                customdata=list(zip(by_bank["cases"], by_bank["recovered"])),
                hovertemplate="<b>%{y}</b><br>%{customdata[1]:,} of %{customdata[0]:,} "
                              "cases recovered<extra></extra>",
            )
        )
        fig2.update_layout(xaxis={"visible": False}, showlegend=False)
        theme.bar_headroom(fig2, [p / 100 for p in by_bank["paise"]])
        st.plotly_chart(
            theme.style_fig(fig2, height=max(240, 30 * len(by_bank))),
            width="stretch",
            config=theme.PLOTLY_CONFIG,
        )
    else:
        st.info("No recovery cases linked to a bank yet.")

    # ── Per hour ─────────────────────────────────────────────────────────
    st.divider()
    theme.section(
        "Attempt success by hour of original failure",
        "Success here is the attempt completing — a link created — not money received.",
    )
    by_hour = query_db(BY_HOUR_SQL)
    if by_hour is None or by_hour.empty:
        st.info("No attempts to break down by hour yet.")
        return

    by_hour["rate"] = by_hour["succeeded"] / by_hour["attempts"] * 100
    fig3 = go.Figure(
        go.Scatter(
            x=by_hour["hour"], y=by_hour["rate"],
            mode="lines+markers",
            line={"color": theme.BRASS_TEXT, "width": 2},
            marker={"size": 8, "color": theme.BRASS_TEXT,
                    "line": {"color": theme.INK, "width": 2}},
            customdata=list(zip(by_hour["attempts"], by_hour["succeeded"])),
            hovertemplate="<b>%{x}:00 UTC</b><br>%{y:.0f}% success"
                          "<br>%{customdata[1]:,} of %{customdata[0]:,} attempts<extra></extra>",
        )
    )
    # A soft brass wash under the curve — depth without a second visual subject.
    theme.soft_fill(fig3, theme.BRASS, opacity=0.12)
    # The retry blackout, shown where it applies rather than described in prose.
    # 23:00-07:00 IST is 17:30-01:30 UTC; drawn on the UTC axis this page uses.
    fig3.add_vrect(x0=17.5, x1=24, fillcolor=theme.CLAY, opacity=0.10, line_width=0,
                   annotation_text="IST retry blackout", annotation_position="top left",
                   annotation_font={"color": theme.CLAY_TEXT, "size": 11})
    fig3.add_vrect(x0=0, x1=1.5, fillcolor=theme.CLAY, opacity=0.10, line_width=0)
    fig3.update_layout(
        xaxis={"title": "Hour of day (UTC)", "dtick": 3, "showgrid": False},
        yaxis={"title": "Attempt success (%)", "ticksuffix": "%"},
        showlegend=False, hovermode="x unified",
    )
    st.plotly_chart(theme.style_fig(fig3, height=320), width="stretch",
                    config=theme.PLOTLY_CONFIG)

    with st.expander("View as table"):
        st.dataframe(
            pd.DataFrame({"Hour (UTC)": by_hour["hour"].astype(int),
                          "Attempts": by_hour["attempts"],
                          "Succeeded": by_hour["succeeded"],
                          "Rate %": by_hour["rate"].round(1)}),
            width="stretch", hide_index=True,
        )
