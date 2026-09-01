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
    theme.page_header("PIPELINE", "Where the money leaves")

    row = query_db(FUNNEL_SQL)
    if row is None or row.empty or int(row["failed"].iloc[0]) == 0:
        no_data("payment failures")
        return

    left, right = st.columns(2)
    with left:
        st.plotly_chart(
            theme.style_fig(_funnel(row, PAYMENT_STAGES, "Payments", "payments"), height=250),
            width="stretch",
            config=theme.PLOTLY_CONFIG,
        )
    with right:
        st.plotly_chart(
            theme.style_fig(_funnel(row, ATTEMPT_STAGES, "Attempts", "attempts"), height=250),
            width="stretch",
            config=theme.PLOTLY_CONFIG,
        )

    st.markdown(
        f"<p style='color:{theme.SLATE};font-size:0.82rem;margin:-0.6rem 0 1.4rem 0;'>"
        f"Separate because they count different things — one case can spend several "
        f"attempts, so attempts outnumber payments. Read each funnel against its own "
        f"first stage.</p>",
        unsafe_allow_html=True,
    )

    # The failure-class breakdown that used to live here charted exactly the
    # same GROUP BY as the CHASERS page's "Why payments degraded" — two
    # independent queries of one fact, free to drift apart. That page's version
    # is canonical: it filters is_retryable and now carries the recovery rate
    # too. One chart, one place.
    st.divider()
    theme.section("Chaser effectiveness", "Per risk type, per touch: does the rung earn its slot.")
    # Straight SQL over the engine's own tables — the same facts
    # cases.chase_effectiveness computes for the eval harness. Success-
    # attempts per (risk_type, touch) and eventual recovery among them.
    chase = query_db(
        """
        SELECT rc.risk_type            AS "Risk type",
               ra.attempt_number       AS Touch,
               COUNT(DISTINCT rc.id)   AS Contacted,
               COUNT(DISTINCT CASE WHEN rc.state = 'recovered' THEN rc.id END)
                                        AS Recovered,
               ROUND(100.0 * COUNT(DISTINCT CASE WHEN rc.state = 'recovered'
                     THEN rc.id END) / COUNT(DISTINCT rc.id), 1) AS "Recovered %"
        FROM retry_attempts ra
        JOIN recovery_cases rc ON rc.id = ra.recovery_case_id
        WHERE ra.result = 'success'
        GROUP BY rc.risk_type, ra.attempt_number
        ORDER BY rc.risk_type, ra.attempt_number
        """
    )
    if chase is None or chase.empty:
        st.info("No chaser-driven contacts yet.")
        return
    st.dataframe(chase, width="stretch", hide_index=True)
    st.markdown(
        f"<p style='color:{theme.SLATE};font-size:0.82rem;margin:-0.4rem 0 1.4rem 0;'>"
        f"Recovered % is eventual recovery among cases contacted at that touch — "
        f"a case may recover on a later rung. The per-touch page-view rate lives in "
        f"the eval harness (cases.chase_effectiveness), which joins case_events.</p>",
        unsafe_allow_html=True,
    )
