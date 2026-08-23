"""
Recovery funnel — where money leaves the pipeline, measured.

This page once drew a hardcoded funnel ([1247, 1247, 1058, ...]) and a
hardcoded failure-class pie. Every figure here is a COUNT over the real tables,
and when there are none the page says so rather than inventing a shape.

The failure-class breakdown is a sorted bar, not the pie it used to be. Twelve
slices is past the point where anyone can rank them by eye, and rank is the
whole question — which blocker costs the most.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import theme
from dashboard.db import no_data, query_db

FUNNEL_SQL = """
SELECT
  (SELECT COUNT(*) FROM payment_failures)                              AS failed,
  (SELECT COUNT(*) FROM payment_failures WHERE is_retryable)           AS retryable,
  (SELECT COUNT(*) FROM retry_attempts)                                AS decided,
  (SELECT COUNT(*) FROM retry_attempts WHERE guardrail_passed)         AS passed,
  (SELECT COUNT(*) FROM retry_attempts
     WHERE result IN ('success','failed','pending'))                   AS attempted,
  (SELECT COUNT(*) FROM recovery_cases WHERE state = 'recovered')      AS recovered
"""

# Two funnels, not one, because these count different things. A single chart
# reads as one shrinking population and this one is not: there are more attempts
# than payments (a case can spend three), so the middle bar was TALLER than the
# first and the whole thing looked broken. Split by unit, each is genuinely
# monotonic and the prose caveat stops being load-bearing.
PAYMENT_STAGES = [("Failed", "failed"), ("Retryable", "retryable"), ("Recovered", "recovered")]
ATTEMPT_STAGES = [("Decided", "decided"), ("Guardrail passed", "passed"), ("Executed", "attempted")]


def _funnel(row: pd.Series, stages: list[tuple[str, str]], title: str, unit: str) -> go.Figure:
    """One monotonic funnel over a single unit."""
    counts = [int(row[c].iloc[0]) for _, c in stages]
    labels = [s for s, _ in stages]
    top = max(counts[0], 1)
    # Ordinal: one hue stepping light-to-dark says "position in a sequence".
    # Five unrelated hues would say "identity" and imply a difference in kind.
    ramp = theme.SEQUENTIAL[-len(stages):]
    fig = go.Figure(
        go.Bar(
            x=counts, y=labels, orientation="h",
            marker={"color": ramp, "line": {"color": theme.INK, "width": 2}},
            text=[f"{c:,}" for c in counts], textposition="outside",
            textfont={"family": theme.FONT_MONO, "color": theme.PAPER, "size": 13},
            customdata=[c / top * 100 for c in counts],
            hovertemplate="<b>%{y}</b><br>%{x:,} " + unit
                          + "<br>%{customdata:.1f}% of the first stage<extra></extra>",
        )
    )
    fig.update_layout(
        title=title,
        yaxis={"autorange": "reversed", "tickfont": {"size": 13}},
        xaxis={"visible": False}, showlegend=False,
    )
    return theme.bar_headroom(fig, counts)


def render() -> None:
    """Render this page. Called by dashboard/app.py."""
    st.markdown(
        f"<div style='font-family:{theme.FONT_MONO};color:{theme.SLATE};font-size:0.74rem;"
        f"letter-spacing:0.12em;margin-bottom:0.2rem;'>PIPELINE</div>",
        unsafe_allow_html=True,
    )
    st.markdown("# Where the money leaves")

    row = query_db(FUNNEL_SQL)
    if row is None or row.empty or int(row["failed"].iloc[0]) == 0:
        no_data("payment failures")
        return

    left, right = st.columns(2)
    with left:
        st.plotly_chart(
            theme.style_fig(_funnel(row, PAYMENT_STAGES, "Payments", "payments"), height=250),
            width="stretch",
        )
    with right:
        st.plotly_chart(
            theme.style_fig(_funnel(row, ATTEMPT_STAGES, "Attempts", "attempts"), height=250),
            width="stretch",
        )

    st.markdown(
        f"<p style='color:{theme.SLATE};font-size:0.82rem;margin:-0.6rem 0 1.4rem 0;'>"
        f"Separate because they count different things — one case can spend several "
        f"attempts, so attempts outnumber payments. Read each funnel against its own "
        f"first stage.</p>",
        unsafe_allow_html=True,
    )

    st.divider()
    theme.section("What is blocking payment", "By failure class, most common first.")

    fc = query_db(
        "SELECT failure_class, COUNT(*) AS n FROM payment_failures "
        "GROUP BY failure_class ORDER BY n DESC"
    )
    if fc is None or fc.empty:
        st.info("No classified failures yet.")
        return

    fc = fc.sort_values("n")
    fig2 = go.Figure(
        go.Bar(
            x=fc["n"],
            y=[c.replace("_", " ") for c in fc["failure_class"]],
            orientation="h",
            # One hue: this is magnitude, not identity. Twelve categorical hues
            # would be a rainbow and would still not be rankable by eye.
            marker={"color": theme.BRASS, "line": {"color": theme.INK, "width": 2}},
            text=[f"{n:,}" for n in fc["n"]],
            textposition="outside",
            textfont={"family": theme.FONT_MONO, "color": theme.SLATE, "size": 12},
            hovertemplate="<b>%{y}</b><br>%{x:,} failures<extra></extra>",
        )
    )
    fig2.update_layout(xaxis={"visible": False}, showlegend=False)
    theme.bar_headroom(fig2, fc["n"])
    st.plotly_chart(
        theme.style_fig(fig2, height=max(260, 26 * len(fc))), width="stretch"
    )

    with st.expander("View as table"):
        st.dataframe(
            pd.DataFrame({"Failure class": fc["failure_class"], "Failures": fc["n"]})
            .sort_values("Failures", ascending=False),
            width="stretch",
            hide_index=True,
        )
