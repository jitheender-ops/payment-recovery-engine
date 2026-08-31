"""
The chasers — the four kinds of money that never reach a gateway, plus the one
that does.

A declined card announces itself through a webhook. An abandoned cart, a halted
subscription, an overdue invoice and a bounced mandate only exist in the
merchant's own systems, so they arrive as signed risk events and the engine
opens a case for each. This page is the per-type answer to "is that working",
which no other page gave: Overview totals everything together, and the funnel
counts stages rather than kinds.

Two panels, because they answer different questions:

  by type      how much each chaser opened, closed and brought back
  why it fails the failure taxonomy behind the payment rail — the only type
               with a gateway reason attached, and the reason the rail picks
               a different card next time

The bounds column is not decoration. Each chaser runs a fixed attempt budget
and consent window from src/chasers/policy.py, and a merchant who cannot see
them has to take "bounded" on trust.
"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from dashboard import theme
from dashboard.db import query_db

BY_TYPE_SQL = """
SELECT risk_type,
       COUNT(*)                                                   AS cases,
       COUNT(*) FILTER (WHERE state = 'recovered')                AS recovered,
       COUNT(*) FILTER (WHERE state = 'open')                     AS still_open,
       COUNT(*) FILTER (WHERE state = 'exhausted')                AS exhausted,
       COALESCE(SUM(amount_at_risk), 0)                           AS at_risk,
       COALESCE(SUM(amount_recovered), 0)                         AS recovered_paise,
       COALESCE(SUM(amount_recovered)
                FILTER (WHERE recovered_via_attempt_id IS NOT NULL), 0)
                                                                  AS attributed_paise
FROM recovery_cases
GROUP BY risk_type
ORDER BY at_risk DESC
"""

CONTACT_SQL = """
SELECT c.risk_type, COUNT(*) AS attempts,
       COUNT(*) FILTER (WHERE a.guardrail_passed) AS allowed,
       COUNT(*) FILTER (WHERE NOT a.guardrail_passed) AS refused
FROM retry_attempts a
JOIN recovery_cases c ON c.id = a.recovery_case_id
GROUP BY c.risk_type
"""

FAILURE_CAUSE_SQL = """
SELECT failure_class          AS cause,
       COUNT(*)               AS failures,
       COUNT(*) FILTER (WHERE is_retryable) AS retryable
FROM payment_failures
GROUP BY failure_class
ORDER BY failures DESC
LIMIT 12
"""

# Display copy per risk type. The engine's own name for each is a schema
# string; a merchant reads a different vocabulary, and the bounds come from
# src/chasers/policy.py so this table cannot promise what the engine will not
# enforce.
_COPY: dict[str, tuple[str, str]] = {
    "payment_failure": (
        "Failed payment",
        "A card or UPI charge declined at the gateway. Arrives by webhook — "
        "the only type the engine is told about without being asked.",
    ),
    "checkout_abandonment": (
        "Checkout drop-off",
        "A cart went cold before any payment was attempted. Two touches, "
        "never a third.",
    ),
    "subscription_failure": (
        "Failed subscription",
        "A renewal did not go through while the customer still believes they "
        "are subscribed. Reached before the grace period ends.",
    ),
    "invoice_overdue": (
        "B2B invoice overdue",
        "A business invoice past due. Escalates by person, not by volume — "
        "see the Receivables page.",
    ),
    "mandate_failure": (
        "Mandate retry sequence",
        "A pre-approved autopay debit bounced. Standing consent to collect, "
        "so it is presented again after a day for funds to arrive.",
    ),
}


def _bounds() -> dict[str, str]:
    """Attempt budget and consent window per type, read from the policy the
    engine actually enforces rather than restated here."""
    from src.chasers.policy import RISK_POLICIES

    out: dict[str, str] = {}
    for risk_type, p in RISK_POLICIES.items():
        hours = p.consent_window_hours
        window = f"{hours // 24}d" if hours % 24 == 0 else f"{hours}h"
        out[risk_type] = f"{p.max_attempts} touches · {window} window"
    out["payment_failure"] = "webhook-driven · config bounds"
    return out


def render() -> None:
    """Render this page. Called by dashboard/app.py."""
    theme.page_header(
        "CHASERS",
        "Five kinds of leaking money",
        "One engine, five sources. A declined card announces itself; the other "
        "four only exist in your own systems until you push them.",
    )

    by_type = query_db(BY_TYPE_SQL)
    if by_type is None or by_type.empty:
        theme.empty_state(
            "No cases opened yet",
            "Failed card charges arrive through the Razorpay webhook. For the "
            "other four, push a signed risk event and the engine opens a case "
            "and starts the chase.",
            action_label="Drive synthetic traffic through all four chasers",
            action_code="python scripts/run_risk_batch.py --count 24",
        )
        return

    contacts = query_db(CONTACT_SQL)
    contact_by_type = (
        {r["risk_type"]: r for _, r in contacts.iterrows()}
        if contacts is not None and not contacts.empty
        else {}
    )
    bounds = _bounds()

    # ── Totals across every chaser ───────────────────────────────────────
    total_at_risk = int(by_type["at_risk"].sum())
    total_attributed = int(by_type["attributed_paise"].sum())
    total_recovered = int(by_type["recovered_paise"].sum())
    open_cases = int(by_type["still_open"].sum())

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        theme.tile("At risk", theme.compact_inr(total_at_risk), icon="money",
                   foot=f"across {int(by_type['cases'].sum()):,} cases")
    with c2:
        theme.tile("Brought back", theme.compact_inr(total_attributed), icon="trend-up",
                   tone="brass", foot="through a link we sent")
    with c3:
        theme.tile("Paid anyway", theme.compact_inr(total_recovered - total_attributed),
                   icon="check", foot="counted, never claimed as ours")
    with c4:
        theme.tile("Still open", f"{open_cases:,}", icon="open-case",
                   foot="being chased right now")

    # ── Per chaser ───────────────────────────────────────────────────────
    st.divider()
    theme.section(
        "How each chaser is doing",
        "Bounds come from src/chasers/policy.py — what the engine enforces, "
        "not what this page claims.",
    )

    for _, row in by_type.iterrows():
        rt = str(row["risk_type"])
        label, blurb = _COPY.get(rt, (rt.replace("_", " ").title(), ""))
        cases = int(row["cases"])
        rec = int(row["recovered"])
        at_risk = int(row["at_risk"])
        got = int(row["attributed_paise"])
        rate = (rec / cases * 100) if cases else 0.0
        con = contact_by_type.get(rt)
        refused = int(con["refused"]) if con is not None else 0

        head, mid, tail = st.columns([3, 2, 2])
        with head:
            st.markdown(
                f"<div style='font-weight:600;font-size:0.98rem;'>{label}</div>"
                f"<div style='color:{theme.SLATE};font-size:0.8rem;margin-top:.15rem;"
                f"max-width:52ch;'>{blurb}</div>"
                f"<div style='margin-top:.4rem;'>"
                f"{theme.chip(bounds.get(rt, 'bounds unset'))}"
                + (theme.chip(f"{refused} refused by guardrail", tone="clay")
                   if refused else "")
                + "</div>",
                unsafe_allow_html=True,
            )
        with mid:
            st.markdown(
                f'<div style="font-family:{theme.FONT_MONO};font-size:1.25rem;">'
                f"{rec:,}<span style='color:{theme.SLATE};font-size:0.85rem;'>"
                f" / {cases:,} recovered</span></div>"
                f"<div style='color:{theme.SLATE};font-size:0.78rem;margin-top:.2rem;'>"
                f"{rate:.0f}% of cases · {int(row['still_open']):,} open · "
                f"{int(row['exhausted']):,} out of attempts</div>",
                unsafe_allow_html=True,
            )
        with tail:
            st.markdown(
                f'<div style="font-family:{theme.FONT_MONO};font-size:1.25rem;'
                f'color:{theme.BRASS_TEXT};text-align:right;">'
                f"{theme.compact_inr(got)}</div>"
                f"<div style='color:{theme.SLATE};font-size:0.78rem;text-align:right;"
                f"margin-top:.2rem;'>of {theme.compact_inr(at_risk)} at risk</div>",
                unsafe_allow_html=True,
            )
        st.markdown(
            f"<div style='height:1px;background:{theme.LINE};margin:.85rem 0;'></div>",
            unsafe_allow_html=True,
        )

    # ── Why the payment rail failed ──────────────────────────────────────
    st.divider()
    theme.section(
        "Why payments degraded",
        "The gateway's reason for each decline, ranked. This drives whether a "
        "retry is worth attempting at all and which rail it moves to.",
    )
    causes = query_db(FAILURE_CAUSE_SQL)
    if causes is None or causes.empty:
        st.info(
            "No gateway failures recorded yet — this panel covers the payment "
            "rail only, since the other four never reach a gateway to be "
            "declined by one."
        )
        return

    causes["cause"] = causes["cause"].fillna("unclassified")
    fig = go.Figure(
        go.Bar(
            x=causes["failures"],
            y=causes["cause"],
            orientation="h",
            marker_color=theme.BRASS,
            hovertemplate="%{y}<br>%{x} failures<extra></extra>",
        )
    )
    fig.update_layout(yaxis={"autorange": "reversed"}, xaxis_title="failures")
    st.plotly_chart(
        theme.style_fig(fig, height=max(240, 26 * len(causes))),
        use_container_width=True,
    )

    hard = causes[causes["retryable"] == 0]["failures"].sum()
    if hard:
        st.caption(
            f"{int(hard):,} of these are hard declines — fraud blocks, stolen "
            "cards, permanent refusals. They are abandoned before the agent is "
            "ever called, because retrying them is spend with no upside."
        )
