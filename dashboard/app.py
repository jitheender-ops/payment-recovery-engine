"""
Payment Recovery Engine — Streamlit Dashboard.

Run from the repository root with:

    python -m streamlit run dashboard/app.py

`-m`, and from the root, because `streamlit run` puts the *script's* directory
on sys.path (web/bootstrap.py) — not the working directory — so a bare
`streamlit run dashboard/app.py` cannot import the `dashboard` package the two
lines below need. run.sh and docker-compose.yml both use the `-m` form.

Password-gated: this renders live payment data next to a service that is
published through a public tunnel, and Streamlit binds a port like any other
server. The gate is the first thing that runs after set_page_config, so no query
reaches the database until it passes.

The sub-pages live in dashboard/views/, NOT dashboard/pages/. Streamlit
auto-registers every module under a directory literally named `pages/` as its
own routable URL, which would have handed out /bank_breakdown and friends with
this gate bypassed entirely — and served them broken, since each is a render()
function with no top-level body to execute.
"""

from __future__ import annotations

import streamlit as st

from dashboard.auth import dashboard_password, password_is_correct

st.set_page_config(page_title="Payment Recovery Engine", page_icon="🔄", layout="wide")


# ── Auth ─────────────────────────────────────────────────────────────────
def require_password() -> None:
    """
    Render the login form and stop the script unless already authenticated.

    st.stop() rather than an early return: this module is a script, and every
    caller of every helper below it is at module scope. Stopping is the only way
    to guarantee nothing further executes.
    """
    if st.session_state.get("authenticated"):
        return

    st.title("🔄 Payment Recovery Engine")
    if not dashboard_password():
        st.error(
            "DASHBOARD_PASSWORD is not set, so this dashboard cannot be unlocked. "
            "Set it in .env and restart — `./run.sh` generates one for you."
        )
        st.stop()

    with st.form("login"):
        supplied = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if password_is_correct(supplied):
            # The password itself is never kept — only the fact that it matched.
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password")
    st.stop()


require_password()


# ── DB Connection ────────────────────────────────────────────────────────
# Lives in dashboard/db.py so the view modules can query without importing this
# file back — app.py imports the views at the bottom, and a view importing app
# is a cycle that only works by accident of ordering.
from dashboard.db import no_data, query_db  # noqa: E402

# One round trip for every headline figure, so the four tiles describe a single
# consistent moment rather than four moments a few hundred milliseconds apart.
OVERVIEW_SQL = """
SELECT
  (SELECT COUNT(*) FROM payment_failures)                             AS failures,
  (SELECT COUNT(*) FROM recovery_cases)                               AS cases,
  (SELECT COUNT(*) FROM recovery_cases WHERE state = 'recovered')     AS recovered_cases,
  (SELECT COUNT(*) FROM retry_attempts WHERE result = 'pending')      AS pending,
  (SELECT COUNT(*) FROM retry_attempts WHERE result = 'scheduled')    AS scheduled,
  (SELECT COALESCE(SUM(amount_recovered), 0) FROM recovery_cases
     WHERE recovered_via_attempt_id IS NOT NULL)                      AS attributed_paise
"""


# ── Sidebar ──────────────────────────────────────────────────────────────
st.sidebar.title("🔄 Recovery Engine")
page = st.sidebar.radio(
    "Navigate", ["Overview", "Recovery Funnel", "Bank Breakdown", "Eval Results"]
)

# ── Overview Page ────────────────────────────────────────────────────────
if page == "Overview":
    st.title("🔄 Payment Failure Recovery Engine")
    st.markdown("Real-time dashboard for payment failure recovery pipeline.")

    row = query_db(OVERVIEW_SQL)
    if row is None or row.empty:
        no_data("recovery activity")
    else:
        total = int(row["failures"].iloc[0])
        cases = int(row["cases"].iloc[0])
        recovered_cases = int(row["recovered_cases"].iloc[0])

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Failures", f"{total:,}")
        # Cases recovered over cases opened. The old figure divided
        # retry_attempts with result='success' by payment_failures — a payment
        # LINK being created successfully, which says nothing about whether the
        # customer paid it. It reported a recovery rate for money that had not
        # arrived.
        c2.metric(
            "Recovery Rate",
            f"{(recovered_cases / cases * 100) if cases else 0:.1f}%",
            help="Recovery cases in a terminal 'recovered' state, over cases opened.",
        )
        c3.metric("Active Retries", int(row["pending"].iloc[0]))
        c4.metric(
            "₹ Recovered",
            f"₹{float(row['attributed_paise'].iloc[0]) / 100:,.0f}",
            help=(
                "Attributed to a payment link this engine sent. Money the customer "
                "paid on their own is excluded — real revenue, but not our result."
            ),
        )
        if int(row["scheduled"].iloc[0]):
            st.caption(
                f"{int(row['scheduled'].iloc[0])} retries are scheduled for later "
                "and will fire from src/scheduler.py."
            )

    st.divider()
    st.subheader("Recent Activity")

    recent = query_db("""
        SELECT ra.payment_id, ra.action_type, ra.result, ra.agent_type,
               ra.channel, ra.guardrail_passed, ra.scheduled_at, ra.created_at
        FROM retry_attempts ra ORDER BY ra.created_at DESC LIMIT 20
    """)
    if recent is not None and len(recent) > 0:
        st.dataframe(recent, use_container_width=True)
    else:
        # Deliberately not a demo table. The five fabricated rows that used to
        # render here were indistinguishable from live output, and that is
        # precisely what made this dashboard untrustworthy.
        st.info("No retry attempts recorded yet.")

elif page == "Recovery Funnel":
    from dashboard.views import recovery_funnel
    recovery_funnel.render()
elif page == "Bank Breakdown":
    from dashboard.views import bank_breakdown
    bank_breakdown.render()
elif page == "Eval Results":
    from dashboard.views import eval_results
    eval_results.render()
