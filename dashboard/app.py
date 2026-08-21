"""
Payment Recovery Engine — Streamlit Dashboard.
Run with: streamlit run dashboard/app.py
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd
import streamlit as st

if TYPE_CHECKING:
    # Type-only: create_engine is imported inside get_db_engine so the dashboard
    # still loads (degraded to mock metrics) when sqlalchemy or the DB is absent.
    # A module-level runtime import here would defeat that.
    from sqlalchemy.engine import Engine

st.set_page_config(page_title="Payment Recovery Engine", page_icon="🔄", layout="wide")

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
    from dashboard.pages import recovery_funnel
    recovery_funnel.render()
elif page == "Bank Breakdown":
    from dashboard.pages import bank_breakdown
    bank_breakdown.render()
elif page == "Eval Results":
    from dashboard.pages import eval_results
    eval_results.render()
