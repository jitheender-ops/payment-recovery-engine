"""
Payment Recovery Engine — operations console.

Run from the repository root with:

    python -m streamlit run dashboard/app.py

`-m`, and from the root, because `streamlit run` puts the *script's* directory
on sys.path (web/bootstrap.py) — not the working directory — so a bare
`streamlit run dashboard/app.py` cannot import the `dashboard` package the lines
below need. run.sh and docker-compose.yml both use the `-m` form.

Password-gated: this renders live payment data next to a service published
through a public tunnel, and Streamlit binds a port like any other server. The
gate is the first thing that runs after set_page_config, so no query reaches the
database until it passes.

The sub-pages live in dashboard/views/, NOT dashboard/pages/. Streamlit
auto-registers every module under a directory literally named `pages/` as its
own routable URL, which would have handed out /bank_breakdown and friends with
this gate bypassed entirely — and served them broken, since each is a render()
function with no top-level body to execute.
"""

from __future__ import annotations

import streamlit as st

from dashboard import theme
from dashboard.auth import dashboard_password, password_is_correct

st.set_page_config(
    page_title="Recovery Engine",
    page_icon="₹",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.apply()


# ── Auth ─────────────────────────────────────────────────────────────────
def require_password() -> None:
    """
    Render the sign-in form and stop the script unless already authenticated.

    st.stop() rather than an early return: this module is a script, and every
    caller of every helper below it is at module scope. Stopping is the only
    way to guarantee nothing further executes.
    """
    if st.session_state.get("authenticated"):
        return

    st.markdown(
        f"""
        <div style="max-width:400px;margin:14vh auto 1.6rem auto;">
          <div style="font-family:{theme.FONT_MONO};color:{theme.BRASS_TEXT};
                      font-size:0.78rem;letter-spacing:0.14em;margin-bottom:0.5rem;">
            PAYMENT RECOVERY ENGINE
          </div>
          <h1 style="margin:0 0 0.5rem 0;font-size:2rem;">Sign in</h1>
          <p style="color:{theme.SLATE};font-size:0.9rem;margin:0;">
            This console reads live payment data.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not dashboard_password():
        st.error(
            "DASHBOARD_PASSWORD is not set, so this console cannot be unlocked. "
            "Set it in .env and restart — `./run.sh` generates one for you."
        )
        st.stop()

    _, mid, _ = st.columns([1, 1.4, 1])
    with mid, st.form("login"):
        supplied = st.text_input("Password", type="password", label_visibility="collapsed",
                                 placeholder="Password")
        submitted = st.form_submit_button("Sign in", width="stretch")
    if submitted:
        if password_is_correct(supplied):
            # The password itself is never kept — only the fact that it matched.
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password")
    st.stop()


require_password()


# ── DB ───────────────────────────────────────────────────────────────────
# Lives in dashboard/db.py so the view modules can query without importing this
# file back — app.py imports the views at the bottom, and a view importing app
# is a cycle that only works by accident of ordering.
from dashboard.db import no_data, query_db  # noqa: E402

# One round trip for every headline figure, so the tiles describe a single
# consistent moment rather than several moments a few hundred milliseconds apart.
OVERVIEW_SQL = """
SELECT
  (SELECT COUNT(*) FROM payment_failures)                             AS failures,
  (SELECT COUNT(*) FROM recovery_cases)                               AS cases,
  (SELECT COUNT(*) FROM recovery_cases WHERE state = 'recovered')     AS recovered_cases,
  (SELECT COUNT(*) FROM retry_attempts WHERE result = 'pending')      AS pending,
  (SELECT COUNT(*) FROM retry_attempts WHERE result = 'scheduled')    AS scheduled,
  (SELECT COALESCE(SUM(amount_at_risk), 0)   FROM recovery_cases)     AS at_risk_paise,
  (SELECT COALESCE(SUM(amount_recovered), 0) FROM recovery_cases)     AS recovered_paise,
  (SELECT COALESCE(SUM(amount_recovered), 0) FROM recovery_cases
     WHERE recovered_via_attempt_id IS NOT NULL)                      AS attributed_paise
"""


# ── Sidebar ──────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        f"""
        <div style="display:flex;align-items:center;gap:0.7rem;
                    margin:0.1rem 0 0.25rem 0.15rem;">
          <div style="width:34px;height:34px;border-radius:9px;flex:none;
                      background:linear-gradient(145deg,#B8900F,{theme.BRASS});
                      box-shadow:inset 0 1px 0 rgba(255,255,255,0.28),
                                 0 6px 18px rgba(165,129,8,0.30);
                      display:flex;align-items:center;justify-content:center;">
            <span style="font-family:{theme.FONT_DISPLAY};font-weight:800;
                         font-size:1.05rem;color:#171204;">₹</span>
          </div>
          <div>
            <div style="font-family:{theme.FONT_DISPLAY};font-weight:800;font-size:1.02rem;
                        letter-spacing:-0.02em;color:{theme.PAPER};line-height:1.05;">
              Recovery Engine
            </div>
            <div style="color:{theme.SLATE};font-size:0.7rem;margin-top:0.12rem;">
              Payment failure recovery
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div style='height:1px;background:{theme.LINE};margin:"
        f"1rem 0 0.4rem 0;'></div>",
        unsafe_allow_html=True,
    )
    page = st.radio(
        "Section",
        [
            "Overview",
            "Recovery funnel",
            "Banks & rails",
            "Policy eval",
            "Cases & audit",
            "Operations",
        ],
        label_visibility="collapsed",
        captions=[
            "Money at risk, and what came back",
            "Where the pipeline leaks",
            "Measured routing quality",
            "Does the agent beat the baseline",
            "Lifecycle and audit trail of every case",
            "Scheduler, guardrail vetoes, ledger health",
        ],
    )
    st.divider()

    # Live connection chip. A console that cannot say whether it is looking at
    # real data is a console that shows stale numbers with total confidence.
    from dashboard.db import get_db_engine

    connected = get_db_engine() is not None
    dot = theme.TEAL if connected else theme.CLAY_TEXT
    st.markdown(theme.chip(
        ("database connected" if connected else "database unreachable"),
        tone="teal" if connected else "clay",
    ).replace(
        # prepend a live dot inside the chip
        "<span", f"<span style='width:7px;height:7px;border-radius:50%;background:{dot};"
        f"display:inline-block;margin-right:0.2rem;'></span><span", 1
    ), unsafe_allow_html=True)

    # Data caches live for 30s; this button is how an operator forces now.
    if st.button("Refresh data", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    # Live mode: the KPI panels re-run themselves every 30s via st.fragment —
    # only that panel rerenders, not the page. Off by default: an unwatched
    # console polling a production database forever is not free.
    st.toggle("Live · refresh 30s", key="live_mode", value=False)

# ── Overview ─────────────────────────────────────────────────────────────
if page == "Overview":
    theme.page_header("OVERVIEW", "Money at risk, and what came back")

    live = bool(st.session_state.get("live_mode"))

    # The signature band and the KPI tiles are one unit — they describe the
    # same moment. As a fragment with run_every, ONLY this panel rerenders on
    # each tick (the official live-dashboard pattern): the ledger moves as
    # money lands without a full-page flicker.
    @st.fragment(run_every="30s" if live else None)
    def _money_panel() -> None:
        current = query_db(OVERVIEW_SQL)
        if current is None or current.empty:
            no_data("recovery activity")
            return
        cases = int(current["cases"].iloc[0])
        recovered_cases = int(current["recovered_cases"].iloc[0])
        at_risk = int(current["at_risk_paise"].iloc[0])
        recovered = int(current["recovered_paise"].iloc[0])
        attributed = int(current["attributed_paise"].iloc[0])
        pending = int(current["pending"].iloc[0])
        scheduled = int(current["scheduled"].iloc[0])

        st.markdown(
            theme.ledger_band(at_risk, recovered, attributed), unsafe_allow_html=True
        )

        c1, c2, c3, c4 = st.columns(4)
        # Cases recovered over cases opened. The old figure divided
        # retry_attempts with result='success' by payment_failures — a payment
        # LINK being created successfully, which says nothing about whether the
        # customer paid it. It reported a recovery rate for money not yet in.
        with c1:
            theme.tile(
                "Money attributed", theme.compact_inr(attributed),
                icon="₹", tone="brass",
                foot="earned by links we sent",
                help="Recovered through a payment link this engine sent. Money the "
                "customer paid on their own is excluded — real revenue, but not "
                "our result.",
            )
        with c2:
            theme.tile(
                "Recovery rate",
                f"{(recovered_cases / cases * 100) if cases else 0:.1f}%",
                icon="↗",
                foot=f"{recovered_cases:,} of {cases:,} cases closed recovered",
                help="Cases in a terminal 'recovered' state, over cases opened.",
            )
        with c3:
            theme.tile(
                "In flight", f"{pending:,}", icon="⟳",
                tone="clay" if pending > 20 else "paper",
                foot="write-ahead, awaiting outcome",
                help="Attempts written ahead of the Razorpay call, not yet resolved.",
            )
        with c4:
            theme.tile(
                "Scheduled", f"{scheduled:,}", icon="⏱",
                foot="fire from the Layer 6 scheduler" if scheduled else "nothing waiting",
                help="Deferred retry_at decisions parked until their moment arrives.",
            )

    _money_panel()

    st.divider()
    theme.section(
        "Recent decisions",
        "Newest first. Every row is one agent decision and its outcome.",
    )

    recent = query_db("""
        SELECT ra.created_at AS "When", ra.payment_id AS "Payment",
               ra.action_type AS "Action", ra.result AS "Result",
               ra.agent_type AS "Decided by", ra.channel AS "Channel",
               ra.guardrail_passed AS "Guardrail", ra.scheduled_at AS "Fires at"
        FROM retry_attempts ra ORDER BY ra.created_at DESC LIMIT 12
    """)
    if recent is not None and len(recent) > 0:
        export_l, list_r = st.columns([1.6, 8.4])
        with export_l:
            st.download_button(
                "Export CSV",
                recent.to_csv(index=False).encode("utf-8"),
                file_name="recent_decisions.csv",
                mime="text/csv",
            )
        rows: list[str] = []
        for _, r in recent.iterrows():
            guard = (
                f"<span style='color:{theme.TEAL_TEXT};font-family:{theme.FONT_MONO};"
                "font-size:0.78rem;' title='guardrail passed'>✓ pass</span>"
                if bool(r["Guardrail"])
                else f"<span style='color:{theme.CLAY_TEXT};"
                     "font-family:" + theme.FONT_MONO + ";font-size:0.78rem;' "
                     "title='guardrail vetoed'>✗ veto</span>"
            )
            fires = theme.fmt_ist(r["Fires at"]) if r["Fires at"] is not None else ""
            fires_html = (
                f"<span style='color:{theme.SLATE};font-size:0.74rem;'>→ {fires}</span>"
                if fires != "—"
                else ""
            )
            rows.append(f"""
              <div style="display:grid;grid-template-columns:9rem 1fr 8rem 7rem 6.5rem;
                          gap:0.8rem;align-items:center;
                          padding:0.55rem 0.35rem;border-bottom:1px solid {theme.LINE};">
                <span style="font-family:{theme.FONT_MONO};color:{theme.SLATE};
                             font-size:0.76rem;">{theme.fmt_ist(r['When'])}</span>
                <span style="font-family:{theme.FONT_MONO};color:{theme.PAPER};
                             font-size:0.8rem;">{r['Payment']}
                    {fires_html}</span>
                <span>{theme.status_chip(r['Action'])}</span>
                <span>{theme.status_chip(r['Result'] or 'pending')}</span>
                <span style="text-align:right;">{guard}
                  <span style="color:{theme.MUTE};font-size:0.7rem;margin-left:0.4rem;"
                        title="who decided">{r['Decided by']}</span></span>
              </div>""")
        # Horizontal scroll containment below ~900px: a grid with fixed track
        # widths that silently overflows its parent is broken on phones, and
        # clipping it would hide the verdict column — scrolling keeps every
        # fact reachable.
        st.markdown(
            "<div style='overflow-x:auto;'><div style='min-width:760px;"
            "border-top:1px solid " + theme.LINE + ";'>" + "".join(rows)
            + "</div></div>",
            unsafe_allow_html=True,
        )
    else:
        # Deliberately not a demo table. The five fabricated rows that used to
        # render here were indistinguishable from live output, and that is
        # precisely what made this console untrustworthy.
        st.info("No decisions recorded yet. Send traffic through the webhook endpoint.")

elif page == "Recovery funnel":
    from dashboard.views import recovery_funnel
    recovery_funnel.render()
elif page == "Banks & rails":
    from dashboard.views import bank_breakdown
    bank_breakdown.render()
elif page == "Policy eval":
    from dashboard.views import eval_results
    eval_results.render()
elif page == "Cases & audit":
    from dashboard.views import cases_audit
    cases_audit.render()
elif page == "Operations":
    from dashboard.views import operations
    operations.render()
