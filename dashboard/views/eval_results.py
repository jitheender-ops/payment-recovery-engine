"""
Policy eval — does the agent beat a fixed retry schedule, and is it real?

This page crashed before it drew anything. It iterated the results JSON as if
every top-level key were a policy, but the file is
{retry_cost_inr, policies, paired_vs_baseline, economics_vs_baseline} — so the
first key it reached was a float and `.get()` on it raised AttributeError.

Recovery rate is deliberately NOT the headline. If attempts were free, "retry
everything, always" would win by construction, so the number that leads is net
of retry cost, and the confidence interval on the paired DIFFERENCE is what
answers "is that real" — two overlapping ± ranges cannot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import theme

RESULTS = Path("eval/results/eval_results.json")
BASELINE = "Fixed 3-Retry"


def _load() -> dict[str, Any] | None:
    if not RESULTS.exists():
        return None
    try:
        with open(RESULTS) as f:
            data: dict[str, Any] = json.load(f)
        return data
    except (OSError, json.JSONDecodeError):
        return None


def render() -> None:
    """Render this page. Called by dashboard/app.py."""
    st.markdown(
        f"<div style='font-family:{theme.FONT_MONO};color:{theme.SLATE};font-size:0.74rem;"
        f"letter-spacing:0.12em;margin-bottom:0.2rem;'>EVIDENCE</div>",
        unsafe_allow_html=True,
    )
    st.markdown("# Is the agent actually better")

    data = _load()
    if data is None:
        st.info("No eval results yet. Generate them with:")
        st.code("python -m eval.runner --scenarios 5000 --seeds 5 --skip-llm", language="bash")
        return

    policies: dict[str, Any] = data.get("policies", {})
    paired: dict[str, Any] = data.get("paired_vs_baseline", {})
    econ: dict[str, Any] = data.get("economics_vs_baseline", {})
    cost = data.get("retry_cost_inr", 2.0)
    if not policies:
        st.warning("The results file has no `policies` block. Re-run the harness.")
        return

    # ── Headline ─────────────────────────────────────────────────────────
    # The best non-baseline policy by NET revenue, which is the only ranking
    # that does not reward brute force.
    ranked = sorted(
        ((n, m) for n, m in policies.items() if n != BASELINE),
        key=lambda kv: kv[1].get("net_₹_per_₹1Cr_failed", 0),
        reverse=True,
    )
    if ranked:
        best, bm = ranked[0]
        base = policies.get(BASELINE, {})
        delta = bm.get("net_₹_per_₹1Cr_failed", 0) - base.get("net_₹_per_₹1Cr_failed", 0)
        st.markdown(
            f"""
            <div style="background:{theme.SURFACE};border:1px solid {theme.LINE};
                        border-left:3px solid {theme.BRASS};border-radius:10px;
                        padding:1.3rem 1.5rem;margin:0.4rem 0 1.8rem 0;">
              <div style="color:{theme.SLATE};text-transform:uppercase;font-size:0.72rem;
                          letter-spacing:0.08em;margin-bottom:0.5rem;">
                {best} vs {BASELINE} · net of ₹{cost:.2f} per attempt
              </div>
              <div style="font-family:{theme.FONT_MONO};font-size:2.4rem;font-weight:500;
                          color:{theme.BRASS_TEXT};line-height:1.1;">
                +{theme.compact_inr(delta * 100)}
              </div>
              <div style="color:{theme.PAPER};font-size:0.94rem;margin-top:0.45rem;">
                additional net revenue per ₹1Cr of failed volume,
                at {bm.get('retry_cost_avg', 0):.2f} attempts per payment
                against {base.get('retry_cost_avg', 0):.2f}.
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── Per-policy table ─────────────────────────────────────────────────
    theme.section("Every policy", f"{len(policies)} policies, mean ± σ across seeds.")
    st.dataframe(
        pd.DataFrame([
            {
                "Policy": n,
                "Recovery %": (
                    f"{m.get('recovery_rate_%', 0):.1f} ± "
                    f"{m.get('recovery_rate_%_std', 0):.1f}"
                ),
                "Attempts": f"{m.get('retry_cost_avg', 0):.2f}",
                "False retry %": f"{m.get('false_retry_rate_%', 0):.1f}",
                "₹ / ₹1Cr": theme.compact_inr(m.get("₹_per_₹1Cr_failed", 0) * 100),
                "Net ₹ / ₹1Cr": theme.compact_inr(m.get("net_₹_per_₹1Cr_failed", 0) * 100),
            }
            for n, m in policies.items()
        ]),
        width="stretch", hide_index=True,
    )

    # ── Net revenue, the ranking that matters ────────────────────────────
    st.divider()
    theme.section(
        "Net revenue per ₹1Cr of failed volume",
        "Net of retry cost. Recovery rate alone would make brute force optimal by construction.",
    )
    names = list(policies.keys())
    nets = [policies[n].get("net_₹_per_₹1Cr_failed", 0) for n in names]
    order = sorted(range(len(names)), key=lambda i: nets[i])
    fig = go.Figure(
        go.Bar(
            x=[nets[i] for i in order],
            y=[names[i] for i in order],
            orientation="h",
            # Colour follows the entity, not the rank: the baseline is clay
            # everywhere on this page, everything else is brass.
            marker={"color": [theme.CLAY if names[i] == BASELINE else theme.BRASS
                              for i in order],
                    "line": {"color": theme.INK, "width": 2}},
            text=[theme.compact_inr(nets[i] * 100) for i in order],
            textposition="outside",
            textfont={"family": theme.FONT_MONO, "color": theme.PAPER, "size": 12},
            hovertemplate="<b>%{y}</b><br>%{x:,.0f} net per ₹1Cr<extra></extra>",
        )
    )
    fig.update_layout(xaxis={"visible": False}, showlegend=False)
    theme.bar_headroom(fig, nets)
    st.plotly_chart(theme.style_fig(fig, height=max(220, 46 * len(names))),
                    width="stretch")

    # ── Is the difference real ───────────────────────────────────────────
    if paired:
        st.divider()
        theme.section(
            "Is that difference real",
            "Paired under common random numbers — the same scenario draws the same "
            "randomness for every policy — so the interval is on the difference itself.",
        )
        rows = [
            {
                "Policy": pol,
                "Metric": "Recovery rate (pp)" if metric == "recovery_rate_pp"
                          else "Retry attempts",
                "Δ": f"{d['mean_delta']:+.3f}",
                "95% CI": f"[{d['ci95'][0]:+.2f}, {d['ci95'][1]:+.2f}]",
                "n": f"{d['n_paired']:,}",
                "Real?": "yes" if d.get("significant") else "no",
            }
            for pol, metrics in paired.items()
            for metric, d in metrics.items()
        ]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    # ── Break-even ───────────────────────────────────────────────────────
    if econ:
        st.divider()
        theme.section(
            "What if the retry cost assumption is wrong",
            f"₹{cost:.2f} per attempt prices gateway and ops only — not decline-ratio "
            "penalties or goodwill. Break-even is the cost at which the two policies tie.",
        )
        for pol, e in econ.items():
            be = e.get("break_even_cost_per_retry_inr")
            st.markdown(
                f"""
                <div style="background:{theme.SURFACE};border:1px solid {theme.LINE};
                            border-radius:10px;padding:1rem 1.2rem;margin-bottom:0.7rem;">
                  <div style="color:{theme.PAPER};font-weight:600;">{pol}</div>
                  <div style="color:{theme.SLATE};font-size:0.88rem;margin-top:0.35rem;">
                    {theme.compact_inr(e.get('delta_revenue_per_crore', 0) * 100)} more revenue
                    on {abs(e.get('delta_attempts_per_crore', 0)):,.0f} fewer attempts per ₹1Cr —
                    <span style="color:{theme.BRASS_TEXT};">{e.get('verdict', '')}</span>
                    {f"· break-even ₹{be:.2f}/retry" if be is not None else ""}
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown(
        f"<p style='color:{theme.SLATE};font-size:0.8rem;margin-top:1.4rem;'>"
        f"The bank simulator is synthetic and says so: success probabilities are "
        f"modelled per failure class in <code>eval/bank_profiles.py</code>, not "
        f"derived from real bank data.</p>",
        unsafe_allow_html=True,
    )
