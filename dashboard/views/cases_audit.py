"""
Cases and audit — the lifecycle of every unit of revenue at risk.

The overview answers "how much came back"; this page answers "what happened to
THIS one". `case_events` is the append-only trail the README points at with a
raw SQL query; an ops console that makes someone open psql to answer "why was
this customer contacted" is missing its most-used page.

State counts lead because they are the triage question: is money stuck in open
cases, finished, or stopped by compliance? The case table then filters by
state, and selecting one case unfolds its event timeline — actor, type, and
the structured detail, newest last so it reads like a story.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from dashboard import theme
from dashboard.db import no_data, query_db

STATE_SQL = """
SELECT state, COUNT(*) AS n,
       COALESCE(SUM(amount_at_risk), 0)  AS at_risk_paise,
       COALESCE(SUM(amount_recovered), 0) AS recovered_paise
FROM recovery_cases GROUP BY state ORDER BY n DESC
"""

CASES_SQL = """
SELECT rc.id::text                    AS case_id,
       rc.state                       AS State,
       rc.risk_type                   AS Type,
       rc.customer_id                 AS Customer,
       rc.amount_at_risk              AS at_risk,
       rc.amount_recovered            AS recovered,
       rc.attempts_used               AS Used,
       rc.max_attempts                AS Max,
       rc.escalation_level            AS Esc,
       rc.next_action_at              AS Next action,
       rc.close_reason                AS Close reason,
       rc.opened_at                   AS Opened
FROM recovery_cases rc
WHERE (:state = 'all' OR rc.state = :state)
ORDER BY rc.opened_at DESC
LIMIT 200
"""

PROMISE_SQL = """
SELECT status, COUNT(*) AS n FROM promises_to_pay GROUP BY status ORDER BY n DESC
"""

EVENTS_SQL = """
SELECT ce.event_type AS Event, ce.actor AS Actor, ce.detail AS Detail,
       ce.created_at AS At
FROM case_events ce
WHERE ce.recovery_case_id = :case_id
ORDER BY ce.id ASC
"""

_STATE_TINT: dict[str, str] = {
    # Semantic roles reused, never redefined: brass is money resolved in our
    # favour, clay is money still exposed, slate covers the compliance stops.
    "recovered": theme.BRASS_TEXT,
    "open": theme.CLAY_TEXT,
    "exhausted": theme.SLATE,
    "abandoned": theme.SLATE,
    "expired": theme.SLATE,
    "opted_out": theme.MUTE,
}


def _detail_str(detail: Any) -> str:
    """The JSONB detail column as a readable one-liner."""
    if detail is None:
        return ""
    if isinstance(detail, dict):
        return "; ".join(f"{k}={v}" for k, v in detail.items())
    s = str(detail)
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return "; ".join(f"{k}={v}" for k, v in parsed.items())
    except (json.JSONDecodeError, TypeError):
        pass
    return s


def render() -> None:
    """Render this page. Called by dashboard/app.py."""
    theme.page_header("CASES", "Every unit of revenue at risk")

    states = query_db(STATE_SQL)
    if states is None or states.empty:
        no_data("recovery cases")
        return

    # ── State tiles: the triage row ──────────────────────────────────────
    by_state: dict[str, pd.Series] = {r["state"]: r for _, r in states.iterrows()}
    order = ["open", "recovered", "exhausted", "abandoned", "opted_out", "expired"]
    present = [s for s in order if s in by_state] + [
        s for s in by_state if s not in order
    ]
    cols = st.columns(len(present))
    icons = {"open": "open-case", "recovered": "money",
             "exhausted": "exhausted", "abandoned": "abandoned",
             "opted_out": "opted-out", "expired": "expired"}
    tones = {"recovered": "brass", "open": "clay"}
    for col, state in zip(cols, present, strict=False):
        row = by_state[state]
        with col:
            theme.tile(
                state.replace("_", " ").title(),
                f"{int(row['n']):,}",
                icon=icons.get(state, "◆"),
                tone=tones.get(state, "paper"),
                foot=f"{theme.compact_inr(row['at_risk_paise'])} at risk · "
                     f"{theme.compact_inr(row['recovered_paise'])} back",
                help=f"Cases in the '{state}' state.",
            )

    # ── Case browser ─────────────────────────────────────────────────────
    st.divider()
    state_filter = st.radio(
        "Show",
        ["all", *present],
        format_func=lambda s: s.replace("_", " ") if s != "all" else "all states",
        horizontal=True,
        label_visibility="collapsed",
    )
    cases = query_db(CASES_SQL, params={"state": state_filter})
    if cases is None or cases.empty:
        st.info(f"No cases in state '{state_filter}'.")
        return

    export_col, table_col = st.columns([1.4, 8.6])
    with export_col:
        st.download_button(
            "Export CSV",
            cases.to_csv(index=False).encode("utf-8"),
            file_name=f"cases_{state_filter}.csv",
            mime="text/csv",
        )
    with table_col:
        show = cases.copy()
        show["At risk"] = show["at_risk"].map(theme.compact_inr)
        show["Recovered"] = show["recovered"].map(
            lambda p: theme.compact_inr(p) if p else "—"
        )
        show["Next action"] = show["Next action"].map(theme.fmt_ist)
        show["Opened"] = show["Opened"].map(theme.fmt_ist)
        show["Case"] = show["case_id"].str.slice(0, 8)

        st.dataframe(
            show[[
                "Case", "State", "Type", "Customer", "At risk", "Recovered",
                "Used", "Max", "Esc", "Next action", "Close reason", "Opened",
            ]],
            width="stretch",
            hide_index=True,
            height=380,
            column_config={
                "State": st.column_config.TextColumn("State"),
            },
        )

    # ── One case, unfolded ───────────────────────────────────────────────
    st.divider()
    theme.section(
        "The trail behind one case",
        "Every open, contact, deferral, promise and attribution, in order, "
        "with the actor that did it.",
    )
    options = {
        f"{r['case_id'][:8]} · {r['State']} · {r['Customer']} · {r['At risk']}": r[
            "case_id"
        ]
        for _, r in cases.iterrows()
    }
    chosen = st.selectbox("Case", list(options.keys()), label_visibility="collapsed")
    events = query_db(EVENTS_SQL, params={"case_id": options[chosen]})
    if events is None or events.empty:
        st.info("No events recorded for this case.")
        return

    for _, ev in events.iterrows():
        kind = str(ev["Event"])
        tint = _STATE_TINT.get(kind if kind in _STATE_TINT else "", theme.SLATE)
        when = theme.fmt_ist(ev["At"])
        detail = _detail_str(ev["Detail"])
        st.markdown(
            f"""
            <div style="border-left:2px solid {tint}; padding:0.15rem 0 0.55rem 0.85rem;
                        margin-left:0.25rem;">
              {theme.status_chip(kind)}
              <span style="color:{theme.MUTE};font-size:0.75rem;"> · {when}
                           · {ev['Actor']}</span>
              <div style="color:{theme.SLATE};font-size:0.8rem;margin-top:0.28rem;">
                {detail}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── Promises ─────────────────────────────────────────────────────────
    promises = query_db(PROMISE_SQL)
    if promises is not None and not promises.empty:
        st.divider()
        theme.section(
            "Promises to pay",
            "A pending promise is the customer asking for silence until a date — "
            "kept means money arrived, broken means the date passed without it.",
        )
        chips = "  ".join(
            theme.chip(
                f"{r['status']} <b style='margin-left:0.3rem;'>{int(r['n']):,}</b>",
                tone=theme.STATUS_TONE.get(r["status"], "slate"),
            )
            for _, r in promises.iterrows()
        )
        st.markdown(chips, unsafe_allow_html=True)
