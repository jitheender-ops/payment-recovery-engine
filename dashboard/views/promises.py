"""
Promises to pay — words that predict money, and whether they were kept.

A promise is the cheapest recovery there is: the customer names a date and the
engine goes quiet until then. No retry, no reminder, no contact budget spent on
someone who has already answered. That only works if promises are tracked
honestly, which is what this page is for — a kept rate nobody measures is a
polite way of being ignored.

Kept-late is split out from kept on purpose. A promise that landed inside the
grace window is money that arrived, but folding it into "kept" hides that the
date was optimistic, and the date is the thing the engine schedules on.

Payment plans sit here rather than on their own page because a plan IS a group
of promises — each instalment is a PromiseToPay row with its own date and its
own silence. Missing one breaks that instalment, not the plan.
"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from dashboard import theme
from dashboard.db import query_db

TOTALS_SQL = """
SELECT COUNT(*)                                             AS total,
       COUNT(*) FILTER (WHERE status = 'kept')              AS kept,
       COUNT(*) FILTER (WHERE status = 'kept'
                          AND COALESCE(kept_late_days, 0) > 0) AS kept_late,
       COUNT(*) FILTER (WHERE status = 'broken')            AS broken,
       COUNT(*) FILTER (WHERE status = 'pending')           AS pending,
       COALESCE(SUM(amount_promised) FILTER (WHERE status = 'pending'), 0)
                                                            AS pending_paise,
       COALESCE(SUM(amount_promised) FILTER (WHERE status = 'kept'), 0)
                                                            AS kept_paise
FROM promises_to_pay
"""

BY_CHANNEL_SQL = """
SELECT COALESCE(channel, 'unknown') AS channel,
       COUNT(*)                                    AS made,
       COUNT(*) FILTER (WHERE status = 'kept')     AS kept,
       COUNT(*) FILTER (WHERE status = 'broken')   AS broken
FROM promises_to_pay
GROUP BY 1
ORDER BY made DESC
"""

UPCOMING_SQL = """
SELECT p.due_at AS "Due", c.subject_ref AS "Case", c.risk_type AS "Type",
       p.amount_promised AS "Amount", COALESCE(p.channel, '—') AS "Channel",
       CASE WHEN p.reminded_at IS NOT NULL THEN 'reminded' ELSE '' END AS "Nudged"
FROM promises_to_pay p
LEFT JOIN recovery_cases c ON c.id = p.recovery_case_id
WHERE p.status = 'pending'
ORDER BY p.due_at ASC
LIMIT 20
"""

PLANS_SQL = """
SELECT pl.status, COUNT(*) AS plans,
       COALESCE(SUM(pl.principal_paise), 0) AS principal
FROM payment_plans pl
GROUP BY pl.status
ORDER BY plans DESC
"""

INSTALMENTS_SQL = """
SELECT c.subject_ref AS "Case", i.seq AS "No.", i.due_at AS "Due",
       i.amount_paise AS "Amount", p.status AS "Status"
FROM plan_instalments i
JOIN payment_plans pl ON pl.id = i.plan_id
LEFT JOIN recovery_cases c ON c.id = pl.case_id
LEFT JOIN promises_to_pay p ON p.id = i.promise_id
ORDER BY i.due_at ASC
LIMIT 25
"""


def render() -> None:
    """Render this page. Called by dashboard/app.py."""
    theme.page_header(
        "PROMISES",
        "Promise-to-pay tracker",
        "A customer who names a date buys silence until that date. Whether "
        "they keep it is the only thing that makes that trade worth making.",
    )

    totals = query_db(TOTALS_SQL)
    if totals is None or totals.empty or int(totals.iloc[0]["total"]) == 0:
        theme.empty_state(
            "No promises captured yet",
            "A promise is recorded when a customer commits to a date — on the "
            "recovery page, in a voice call, or pushed by you through the "
            "merchant API. The case then goes quiet until that date.",
            action_label="Drive traffic that produces promises",
            action_code="python scripts/run_risk_batch.py --count 24",
        )
        return

    t = totals.iloc[0]
    kept, broken = int(t["kept"]), int(t["broken"])
    resolved = kept + broken
    rate = (kept / resolved * 100) if resolved else None

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        theme.tile(
            "Kept rate",
            f"{rate:.0f}%" if rate is not None else "—",
            icon="check", tone="brass",
            foot=f"{kept:,} kept of {resolved:,} resolved"
            if resolved else "nothing resolved yet",
        )
    with c2:
        theme.tile("Broken", f"{broken:,}", icon="cross",
                   tone="clay" if broken else "paper",
                   foot="chase resumed where it stopped")
    with c3:
        theme.tile("Pending", f"{int(t['pending']):,}", icon="hourglass",
                   foot=f"{theme.compact_inr(int(t['pending_paise']))} promised")
    with c4:
        theme.tile("Kept late", f"{int(t['kept_late']):,}", icon="clock",
                   foot="arrived inside the grace window")

    if resolved:
        st.divider()
        theme.section(
            "Kept against broken",
            "Kept-late is drawn separately: the money arrived, but the date was "
            "optimistic — and the date is what the engine schedules on.",
        )
        on_time = kept - int(t["kept_late"])
        fig = go.Figure()
        for label, value, colour in [
            ("Kept on time", on_time, theme.BRASS),
            ("Kept late", int(t["kept_late"]), theme.TEAL),
            ("Broken", broken, theme.CLAY_TEXT),
        ]:
            fig.add_trace(go.Bar(
                x=[value], y=["promises"], orientation="h", name=label,
                marker_color=colour,
                hovertemplate=f"{label}: %{{x}}<extra></extra>",
            ))
        fig.update_layout(barmode="stack", showlegend=True, yaxis_visible=False)
        st.plotly_chart(theme.style_fig(fig, height=170), use_container_width=True)

    # ── Where promises come from ─────────────────────────────────────────
    channels = query_db(BY_CHANNEL_SQL)
    if channels is not None and not channels.empty:
        st.divider()
        theme.section(
            "Where they were made",
            "The surface a promise was captured on, and how well each holds up.",
        )
        view = channels.copy()
        view["kept rate"] = [
            f"{(k / (k + b) * 100):.0f}%" if (k + b) else "—"
            for k, b in zip(view["kept"], view["broken"], strict=True)
        ]
        st.dataframe(
            view.rename(columns={
                "channel": "Channel", "made": "Made",
                "kept": "Kept", "broken": "Broken",
            }),
            use_container_width=True, hide_index=True,
        )

    # ── What is due next ─────────────────────────────────────────────────
    upcoming = query_db(UPCOMING_SQL)
    if upcoming is not None and not upcoming.empty:
        st.divider()
        theme.section(
            "Due next",
            "Each of these is a case the engine is deliberately not contacting.",
        )
        view = upcoming.copy()
        view["Amount"] = view["Amount"].apply(theme.inr)
        view["Due"] = view["Due"].apply(theme.fmt_ist)
        st.dataframe(view, use_container_width=True, hide_index=True)

    # ── Payment plans ────────────────────────────────────────────────────
    plans = query_db(PLANS_SQL)
    if plans is not None and not plans.empty:
        st.divider()
        theme.section(
            "Payment plans",
            "A plan is a group of promises, not a second state machine — each "
            "instalment carries its own date and its own silence.",
        )
        cols = st.columns(max(1, len(plans)))
        for col, (_, row) in zip(cols, plans.iterrows(), strict=False):
            with col:
                theme.tile(
                    str(row["status"]).title(), f"{int(row['plans']):,}",
                    icon="open-case" if row["status"] == "active" else "check",
                    tone="clay" if row["status"] == "defaulted" else "paper",
                    foot=theme.compact_inr(int(row["principal"])) + " principal",
                )

        instalments = query_db(INSTALMENTS_SQL)
        if instalments is not None and not instalments.empty:
            view = instalments.copy()
            view["Amount"] = view["Amount"].apply(theme.inr)
            view["Due"] = view["Due"].apply(theme.fmt_ist)
            view["Status"] = view["Status"].fillna("—")
            st.dataframe(view, use_container_width=True, hide_index=True)
