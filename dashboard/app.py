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
        <div style="font-family:{theme.FONT_MONO};color:{theme.BRASS_TEXT};
                    font-size:0.7rem;letter-spacing:0.14em;margin-bottom:0.15rem;">₹</div>
        <div style="font-family:{theme.FONT_DISPLAY};font-weight:800;font-size:1.15rem;
                    letter-spacing:-0.02em;color:{theme.PAPER};line-height:1.1;">
          Recovery Engine
        </div>
        <div style="color:{theme.SLATE};font-size:0.76rem;margin:0.2rem 0 1.2rem 0;">
          Payment failure recovery
        </div>
        """,
        unsafe_allow_html=True,
    )
    page = st.radio(
        "Section",
        ["Overview", "Recovery funnel", "Banks & rails", "Policy eval"],
        label_visibility="collapsed",
    )

# ── Overview ─────────────────────────────────────────────────────────────
if page == "Overview":
    st.markdown(
        f"<div style='font-family:{theme.FONT_MONO};color:{theme.SLATE};font-size:0.74rem;"
        f"letter-spacing:0.12em;margin-bottom:0.2rem;'>OVERVIEW</div>",
        unsafe_allow_html=True,
    )
    st.markdown("# Money at risk, and what came back")

    row = query_db(OVERVIEW_SQL)
    if row is None or row.empty:
        no_data("recovery activity")
    else:
        cases = int(row["cases"].iloc[0])
        recovered_cases = int(row["recovered_cases"].iloc[0])
        at_risk = int(row["at_risk_paise"].iloc[0])
        recovered = int(row["recovered_paise"].iloc[0])
        attributed = int(row["attributed_paise"].iloc[0])

        # The signature. Placed above the tiles because it is the answer, and
        # the tiles are the supporting detail.
        st.markdown(
            theme.ledger_band(at_risk, recovered, attributed), unsafe_allow_html=True
        )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Cases opened", f"{cases:,}")
        # Cases recovered over cases opened. The old figure divided
        # retry_attempts with result='success' by payment_failures — a payment
        # LINK being created successfully, which says nothing about whether the
        # customer paid it. It reported a recovery rate for money not yet in.
        c2.metric(
            "Recovery rate (cases)",
            f"{(recovered_cases / cases * 100) if cases else 0:.1f}%",
            help="Cases in a terminal 'recovered' state, over cases opened.",
        )
        c3.metric("In flight", int(row["pending"].iloc[0]),
                  help="Attempts written ahead of the Razorpay call, not yet resolved.")
        c4.metric(
            "Attributed",
            theme.compact_inr(attributed),
            help=(
                "Recovered through a payment link this engine sent. Money the "
                "customer paid on their own is excluded — real revenue, but not "
                "our result."
            ),
        )

        scheduled = int(row["scheduled"].iloc[0])
        if scheduled:
            st.markdown(
                f"<p style='color:{theme.SLATE};font-size:0.84rem;margin-top:0.9rem;'>"
                f"<span style='color:{theme.BRASS_TEXT};font-family:{theme.FONT_MONO};'>"
                f"{scheduled}</span> retries are scheduled for later and will fire from "
                f"the Layer 6 scheduler.</p>",
                unsafe_allow_html=True,
            )

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
        FROM retry_attempts ra ORDER BY ra.created_at DESC LIMIT 25
    """)
    if recent is not None and len(recent) > 0:
        st.dataframe(recent, width="stretch", hide_index=True)
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
