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

from typing import TYPE_CHECKING, Any

import pandas as pd
import streamlit as st

from dashboard.auth import dashboard_password, password_is_correct

if TYPE_CHECKING:
    # Type-only: create_engine is imported inside get_db_engine so the dashboard
    # still loads (degraded to mock metrics) when sqlalchemy or the DB is absent.
    # A module-level runtime import here would defeat that.
    from sqlalchemy.engine import Engine

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
@st.cache_resource
def get_db_engine() -> Engine | None:
    """Try to connect to Postgres. Returns None if unavailable."""
    try:
        import os

        from sqlalchemy import create_engine
        url = os.getenv("DATABASE_URL_SYNC", "postgresql://recovery:recovery@localhost:5432/payment_recovery")
        engine = create_engine(url)
        engine.connect().close()
        return engine
    except Exception:
        return None


@st.cache_data(ttl=30)
def query_db(query: str) -> pd.DataFrame | None:
    engine = get_db_engine()
    if engine is None:
        return None
    try:
        return pd.read_sql(query, engine)
    except Exception:
        return None


def get_mock_metrics() -> dict[str, Any]:
    """Demo metrics when DB is unavailable."""
    return {
        "total_failures": 1247,
        "recovery_rate": 34.2,
        "active_retries": 18,
        "recovered_amount": 425680,
    }


# ── Sidebar ──────────────────────────────────────────────────────────────
st.sidebar.title("🔄 Recovery Engine")
page = st.sidebar.radio(
    "Navigate", ["Overview", "Recovery Funnel", "Bank Breakdown", "Eval Results"]
)

# ── Overview Page ────────────────────────────────────────────────────────
if page == "Overview":
    st.title("🔄 Payment Failure Recovery Engine")
    st.markdown("Real-time dashboard for payment failure recovery pipeline.")

    engine = get_db_engine()
    if engine is None:
        st.warning("⚠️ Database not connected — showing demo data")
        m = get_mock_metrics()
    else:
        failures_df = query_db("SELECT COUNT(*) as cnt FROM payment_failures")
        retries_df = query_db("SELECT COUNT(*) as cnt FROM retry_attempts WHERE result='success'")
        active_df = query_db("SELECT COUNT(*) as cnt FROM retry_attempts WHERE result='pending'")
        amount_df = query_db(
            "SELECT COALESCE(SUM(pf.amount),0) as total FROM retry_attempts ra "
            "JOIN payment_failures pf ON ra.payment_failure_id=pf.id "
            "WHERE ra.result='success'"
        )

        total = int(failures_df["cnt"].iloc[0]) if failures_df is not None else 0
        recovered = int(retries_df["cnt"].iloc[0]) if retries_df is not None else 0
        m = {
            "total_failures": total,
            "recovery_rate": (recovered / total * 100) if total > 0 else 0,
            "active_retries": int(active_df["cnt"].iloc[0]) if active_df is not None else 0,
            "recovered_amount": (
                int(amount_df["total"].iloc[0]) / 100 if amount_df is not None else 0
            ),
        }

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Failures", f"{m['total_failures']:,}")
    c2.metric("Recovery Rate", f"{m['recovery_rate']:.1f}%")
    c3.metric("Active Retries", m["active_retries"])
    c4.metric("₹ Recovered", f"₹{m['recovered_amount']:,.0f}")

    st.divider()
    st.subheader("Recent Activity")

    recent = query_db("""
        SELECT ra.payment_id, ra.action_type, ra.result, ra.agent_type,
               ra.guardrail_passed, ra.created_at
        FROM retry_attempts ra ORDER BY ra.created_at DESC LIMIT 20
    """)
    if recent is not None and len(recent) > 0:
        st.dataframe(recent, use_container_width=True)
    else:
        demo_data = pd.DataFrame({
            "payment_id": [f"pay_{''.join([str(i)]*6)}" for i in range(5)],
            "action_type": ["retry_now", "switch_rail", "nudge_customer", "abandon", "retry_at"],
            "result": ["success", "success", "pending", "skipped", "failed"],
            "agent_type": ["llm", "llm", "xgboost", "deterministic", "llm"],
            "guardrail_passed": [True, True, True, True, False],
        })
        st.dataframe(demo_data, use_container_width=True)

elif page == "Recovery Funnel":
    from dashboard.views import recovery_funnel
    recovery_funnel.render()
elif page == "Bank Breakdown":
    from dashboard.views import bank_breakdown
    bank_breakdown.render()
elif page == "Eval Results":
    from dashboard.views import eval_results
    eval_results.render()
