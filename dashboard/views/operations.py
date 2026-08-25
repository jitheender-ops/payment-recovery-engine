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


def _tile(label: str, value: int, *, warn: bool = False, help: str = "") -> None:
    """One number, tinted only when it needs eyes."""
    colour = theme.CLAY_TEXT if warn and value else theme.PAPER
    st.markdown(
        f"""
        <div style="background:{theme.SURFACE};border:1px solid {theme.LINE};
                    border-radius:10px;padding:0.9rem 1.1rem;">
          <div style="color:{theme.SLATE};text-transform:uppercase;letter-spacing:0.07em;
                      font-size:0.68rem;font-weight:500;">{label}</div>
          <div style="font-family:{theme.FONT_MONO};font-size:1.6rem;font-weight:500;
                      color:{colour};margin-top:0.25rem;">{value:,}</div>
          <div style="color:{theme.MUTE};font-size:0.72rem;margin-top:0.1rem;">{help}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render() -> None:
    """Render this page. Called by dashboard/app.py."""
    st.markdown(
        f"<div style='font-family:{theme.FONT_MONO};color:{theme.SLATE};font-size:0.74rem;"
        f"letter-spacing:0.12em;margin-bottom:0.2rem;'>OPERATIONS</div>",
        unsafe_allow_html=True,
    )
    st.markdown("# Is the machinery running")

    sweep = query_db(SWEEP_SQL)
    if sweep is None or sweep.empty:
        no_data("engine activity")
        return
    s = sweep.iloc[0]

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        _tile("Scheduled retries", int(s["scheduled"]),
              help="Parked by a retry_at decision; the Layer 6 scheduler fires them when due.")
    with c2:
        _tile("Write-ahead pending", int(s["pending"]),
              help="Committed ahead of the Razorpay call, outcome not yet recorded.")
    with c3:
        _tile("Stale > 15 min", int(s["stale_pending"]), warn=True,
              help="Pending past the executor's reach — the reconciler marks these failed.")
    with c4:
        _tile("Events unprocessed", int(s["events_unprocessed"]), warn=True,
              help="Stored webhooks whose background task has not finished.")

    if int(s["events_errored"]) or int(s["promises_overdue"]):
        c1, c2 = st.columns(2)
        with c1:
            _tile("Events with errors", int(s["events_errored"]), warn=True,
                  help="Marked processed with a processing_error — never re-run.")
        with c2:
            _tile("Promises overdue", int(s["promises_overdue"]), warn=True,
                  help="Due dates passed with no money; expire_promises "
                       "hands them back to the chaser.")

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
        st.dataframe(
            show[["At", "Payment", "Action", "Reason", "Decided by"]],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No rejections recorded.")

    # ── Decision mix ─────────────────────────────────────────────────────
    left, right = st.columns([3, 2])
    with left:
        theme.section(
            "Who decided what",
            "agent_type records who ACTUALLY decided — llm means the model answered; "
            "xgboost includes silent degradations the fallback counter caught.",
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
        with left:
            st.plotly_chart(theme.style_fig(fig, height=max(240, 26 * len(mix))),
                            width="stretch")

    # ── Ledger health ────────────────────────────────────────────────────
    st.divider()
    theme.section(
        "Customer ledger health",
        "Rate limits roll on a 24h window; opt-out is permanent and stops everything.",
    )
    ledger = query_db(LEDGER_SQL)
    if ledger is not None and not ledger.empty:
        show = ledger.rename(columns={
            "customers": "Customers", "retries": "Retries (24h)", "nudges": "Nudges (24h)",
        })
        show["Consent"] = show["Consent"].map(
            lambda v: v.replace("_", " ") if isinstance(v, str) else v
        )
        st.dataframe(show, width="stretch", hide_index=True)
