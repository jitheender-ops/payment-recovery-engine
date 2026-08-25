"""
Operations — is the machinery actually running.

The money pages answer "how much came back"; this one answers "why did that
retry not fire", which is the question at 2am. Four sweeps run on a loop
(fire due retries, reconcile dropped events, resolve stale write-ahead rows,
expire promises); every tile here reads the tables those sweeps work on, so a
stuck pipeline shows up as numbers instead of silence.

Deliberately read-only: an ops page that can also mutate state turns a tired
click into an unauthorised contact with a customer.
"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from dashboard import theme
from dashboard.db import no_data, query_db

SWEEP_SQL = """
SELECT
  (SELECT COUNT(*) FROM retry_attempts WHERE result = 'scheduled')   AS scheduled,
  (SELECT COUNT(*) FROM retry_attempts WHERE result = 'pending')     AS pending,
  (SELECT COUNT(*) FROM retry_attempts
      WHERE result = 'pending'
        AND created_at < now() - interval '15 minutes')              AS stale_pending,
  (SELECT COUNT(*) FROM webhook_events
      WHERE processed = false)                                       AS events_unprocessed,
  (SELECT COUNT(*) FROM webhook_events
      WHERE processed = true AND processing_error IS NOT NULL)       AS events_errored,
  (SELECT COUNT(*) FROM promises_to_pay
      WHERE status = 'pending' AND due_at < now())                   AS promises_overdue
"""

NEXT_DUE_SQL = """
SELECT ra.idempotency_key AS Key, ra.payment_id AS Payment,
       ra.scheduled_at AS Due, ra.agent_type AS "Decided by"
FROM retry_attempts ra
WHERE ra.result = 'scheduled'
ORDER BY ra.scheduled_at ASC LIMIT 12
"""

REJECTIONS_SQL = """
SELECT ra.created_at AS At, ra.payment_id AS Payment,
       ra.action_type AS Action, ra.guardrail_rejection_reason AS Reason,
       ra.agent_type AS "Decided by"
FROM retry_attempts ra
WHERE ra.guardrail_passed = false AND ra.action_type <> 'abandon'
ORDER BY ra.created_at DESC LIMIT 20
"""

DECISION_MIX_SQL = """
SELECT agent_type, action_type, COUNT(*) AS n
FROM retry_attempts GROUP BY 1, 2 ORDER BY n DESC
"""

LEDGER_SQL = """
SELECT consent_status AS Consent, COUNT(*) AS customers,
       COALESCE(SUM(total_retries_24h), 0) AS retries,
       COALESCE(SUM(total_nudges_24h), 0)  AS nudges
FROM retry_ledger GROUP BY 1
"""


def render() -> None:
    """Render this page. Called by dashboard/app.py."""
    theme.page_header("OPERATIONS", "Is the machinery running")

    # The sweep tiles are the page's pulse. As a live fragment they re-read
    # every 30s and rerender ONLY themselves — an operator watching a backlog
    # drain sees it drain, without a full-page flicker. Off unless Live mode
    # is on (the toggle lives in the sidebar).
    live = bool(st.session_state.get("live_mode"))

    @st.fragment(run_every="30s" if live else None)
    def _sweep_panel() -> None:
        sweep = query_db(SWEEP_SQL)
        if sweep is None or sweep.empty:
            no_data("engine activity")
            return
        s = sweep.iloc[0]

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            theme.tile(
                "Scheduled retries", f"{int(s['scheduled']):,}", icon="clock",
                foot="fire when their moment arrives",
                help="Parked by a retry_at decision; the Layer 6 scheduler fires "
                     "them when due.",
            )
        with c2:
            theme.tile(
                "Write-ahead pending", f"{int(s['pending']):,}", icon="pending",
                foot="committed ahead of Razorpay",
                help="Committed ahead of the Razorpay call, outcome not yet recorded.",
            )
        with c3:
            theme.tile(
                "Stale > 15 min", f"{int(s['stale_pending']):,}", icon="warning",
                tone="clay" if int(s["stale_pending"]) else "paper",
                foot="reconciler marks these failed",
                help="Pending past the executor's reach — the reconciler marks "
                     "these failed.",
            )
        with c4:
            theme.tile(
                "Events unprocessed", f"{int(s['events_unprocessed']):,}", icon="envelope",
                tone="clay" if int(s["events_unprocessed"]) else "paper",
                foot="background task not finished",
                help="Stored webhooks whose background task has not finished.",
            )

        if int(s["events_errored"]) or int(s["promises_overdue"]):
            e1, e2 = st.columns(2)
            with e1:
                theme.tile(
                    "Events with errors", f"{int(s['events_errored']):,}", icon="warning",
                    tone="clay", foot="never re-run — see processing_error",
                    help="Marked processed with a processing_error — never re-run.",
                )
            with e2:
                theme.tile(
                    "Promises overdue", f"{int(s['promises_overdue']):,}", icon="hourglass",
                    tone="clay", foot="expire_promises re-opens the chase",
                    help="Due dates passed with no money; expire_promises hands "
                         "them back to the chaser.",
                )

    _sweep_panel()

    # ── What fires next ──────────────────────────────────────────────────
    st.divider()
    theme.section(
        "What fires next",
        "The queue the scheduler works from, soonest first.",
    )
    due = query_db(NEXT_DUE_SQL)
    if due is not None and not due.empty:
        show = due.copy()
        show["Due"] = show["Due"].map(theme.fmt_ist)
        st.dataframe(show[["Key", "Payment", "Due", "Decided by"]],
                     width="stretch", hide_index=True)
    else:
        st.info("Nothing scheduled — no deferred retries waiting.")

    # ── Guardrail rejections ─────────────────────────────────────────────
    st.divider()
    theme.section(
        "What the guardrail stopped",
        "Every vetoed action with its reason. A rejection spent an attempt slot "
        "but contacted nobody — this list is how you audit both facts.",
    )
    rejected = query_db(REJECTIONS_SQL)
    if rejected is not None and not rejected.empty:
        show = rejected.copy()
        show["At"] = show["At"].map(theme.fmt_ist)
        export_col, table_col = st.columns([1.4, 8.6])
        with export_col:
            st.download_button(
                "Export CSV",
                rejected.to_csv(index=False).encode("utf-8"),
                file_name="guardrail_rejections.csv",
                mime="text/csv",
            )
        with table_col:
            st.dataframe(
                show[["At", "Payment", "Action", "Reason", "Decided by"]],
                width="stretch",
                hide_index=True,
            )
    else:
        theme.empty_state(
            "No rejections recorded",
            "Either the guardrail has had nothing to veto, or no traffic has "
            "reached it yet. A clean slate here is good news either way.",
            icon="shield",
        )

    # ── Decision mix + ledger health: collapsible ─────────────────────────
    # Primary view stays tidy — an operator lands on the pulse and the queue.
    # The analytical tail opens on demand, which is the drawer pattern this
    # console can actually honor: Streamlit expanders are real disclosure
    # widgets, not decoration.
    with st.expander("Who decided what", expanded=False):
        st.markdown(
            "<p style='color:" + theme.SLATE + ";font-size:0.84rem;"
            "margin:0 0 0.6rem 0;'>agent_type records who ACTUALLY decided — "
            "llm means the model answered; xgboost includes silent "
            "degradations the fallback counter caught.</p>",
            unsafe_allow_html=True,
        )
        mix = query_db(DECISION_MIX_SQL)
        if mix is not None and not mix.empty:
            mix = mix.sort_values("n")
            fig = go.Figure(
                go.Bar(
                    x=mix["n"],
                    y=[f"{a} · {b}" for a, b in zip(mix["agent_type"], mix["action_type"])],
                    orientation="h",
                    marker={"color": theme.BRASS, "line": {"color": theme.INK, "width": 2}},
                    text=[f"{n:,}" for n in mix["n"]],
                    textposition="outside",
                    textfont={"family": theme.FONT_MONO, "color": theme.PAPER, "size": 12},
                    hovertemplate="<b>%{y}</b><br>%{x:,} decisions<extra></extra>",
                )
            )
            fig.update_layout(xaxis={"visible": False}, showlegend=False)
            theme.bar_headroom(fig, mix["n"])
            st.plotly_chart(
                theme.style_fig(fig, height=max(240, 26 * len(mix))),
                width="stretch", config=theme.PLOTLY_CONFIG,
            )

    with st.expander("Customer ledger health", expanded=False):
        st.markdown(
            "<p style='color:" + theme.SLATE + ";font-size:0.84rem;"
            "margin:0 0 0.6rem 0;'>Rate limits roll on a 24h window; opt-out "
            "is permanent and stops everything.</p>",
            unsafe_allow_html=True,
        )
        ledger = query_db(LEDGER_SQL)
        if ledger is not None and not ledger.empty:
            show = ledger.rename(columns={
                "customers": "Customers",
                "retries": "Retries (24h)",
                "nudges": "Nudges (24h)",
            })
            show["Consent"] = show["Consent"].map(
                lambda v: v.replace("_", " ") if isinstance(v, str) else v
            )
            st.dataframe(show, width="stretch", hide_index=True)
