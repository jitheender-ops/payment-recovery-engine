"""
Payment Recovery Engine — Streamlit Dashboard.

Run from the repository root with:

    python -m streamlit run dashboard/app.py
"""

from __future__ import annotations

import streamlit as st

from dashboard.auth import dashboard_password, password_is_correct

st.set_page_config(page_title="Payment Recovery Engine", page_icon="🔄", layout="wide")

# ── Custom CSS ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif !important;
    color: #0F172A;
}

/* Hide Streamlit Header, Footer, Menu */
#MainMenu {visibility: hidden;}
header {visibility: hidden;}
footer {visibility: hidden;}

/* Navy Sidebar */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0F172A 0%, #1E293B 100%);
    color: white;
}
[data-testid="stSidebar"] [class*="css"], [data-testid="stSidebar"] span, [data-testid="stSidebar"] label {
    color: #E2E8F0 !important;
}
[data-testid="stSidebarNav"] {
    padding-top: 1rem;
}
.stRadio > div {
    gap: 0.5rem;
}
.stRadio label {
    padding: 0.5rem 1rem;
    border-radius: 8px;
    transition: all 0.2s;
}
.stRadio label:hover {
    background: rgba(255, 255, 255, 0.1);
}

/* Primary buttons */
.stButton>button {
    background-color: #2563EB !important;
    color: white !important;
    border-radius: 8px !important;
    border: none !important;
    font-weight: 600 !important;
    transition: all 0.2s;
}
.stButton>button:hover {
    background-color: #1D4ED8 !important;
    box-shadow: 0 4px 6px -1px rgba(37, 99, 235, 0.2);
}

/* Metric Cards */
[data-testid="stMetricValue"] {
    font-size: 1.8rem !important;
    font-weight: 700 !important;
    color: #0F172A !important;
}
[data-testid="stMetricLabel"] {
    text-transform: uppercase;
    font-size: 0.75rem !important;
    color: #64748B !important;
    font-weight: 500 !important;
}
[data-testid="metric-container"] {
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
    padding: 1.5rem;
    box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px 0 rgba(0, 0, 0, 0.06);
    transition: transform 0.2s, box-shadow 0.2s;
}
[data-testid="metric-container"]:hover {
    transform: translateY(-2px);
    box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
}
[data-testid="stMetricDelta"] svg {
    color: #10B981 !important;
}
[data-testid="stMetricDelta"] div {
    color: #10B981 !important;
    font-weight: 600;
}

/* Cards & Dataframes */
[data-testid="stDataFrame"] {
    border: 1px solid #E2E8F0;
    border-radius: 12px;
    overflow: hidden;
}

hr {
    border-color: #E2E8F0 !important;
}

/* Architecture visualizer */
.arch-card {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
    padding: 1rem;
    text-align: center;
    box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1);
    height: 100%;
}
.arch-icon {
    font-size: 2rem;
    margin-bottom: 0.5rem;
}
.arch-title {
    font-weight: 600;
    color: #0F172A;
    font-size: 0.9rem;
    margin-bottom: 0.25rem;
}
.arch-desc {
    font-size: 0.75rem;
    color: #64748B;
}

.login-card {
    max-width: 400px;
    margin: 4rem auto;
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 16px;
    padding: 2rem;
    box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1);
    text-align: center;
}
</style>
""", unsafe_allow_html=True)


# ── Auth ─────────────────────────────────────────────────────────────────
def require_password() -> None:
    if st.session_state.get("authenticated"):
        return

    st.markdown('<div class="login-card">', unsafe_allow_html=True)
    st.markdown("<h2>💳 Recovery Engine</h2>", unsafe_allow_html=True)
    st.markdown("<p style='color: #64748B; margin-bottom: 2rem;'>Sign in to access dashboard</p>", unsafe_allow_html=True)
    
    if not dashboard_password():
        st.error(
            "DASHBOARD_PASSWORD is not set. Set it in .env and restart."
        )
        st.markdown('</div>', unsafe_allow_html=True)
        st.stop()

    with st.form("login"):
        supplied = st.text_input("Password", type="password", label_visibility="collapsed", placeholder="Enter password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)
    if submitted:
        if password_is_correct(supplied):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password")
    
    st.markdown('</div>', unsafe_allow_html=True)
    st.stop()


require_password()


# ── DB Connection ────────────────────────────────────────────────────────
from dashboard.db import no_data, query_db  # noqa: E402

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
with st.sidebar:
    st.markdown("## 💳 Recovery Engine")
    st.markdown("<br>", unsafe_allow_html=True)
    page = st.radio(
        "Navigation", 
        ["📊 Overview", "🔄 Recovery Funnel", "🏦 Bank Breakdown", "📈 Eval Results"],
        label_visibility="collapsed"
    )
    
    st.markdown("<div style='position:absolute; bottom:20px; text-align:center; width:100%;'>", unsafe_allow_html=True)
    st.markdown("<span style='font-size:0.7rem; background:rgba(255,255,255,0.1); padding:4px 8px; border-radius:4px;'>Powered by Claude AI + XGBoost</span>", unsafe_allow_html=True)
    st.markdown("<p style='font-size:0.6rem; color:#94A3B8; margin-top:8px;'>v2.0.1 (Razorpay Theme)</p>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


# Strip icons for routing
clean_page = page.split(" ", 1)[1] if " " in page else page

# ── Overview Page ────────────────────────────────────────────────────────
if clean_page == "Overview":
    st.title("Payment Recovery Dashboard")
    st.markdown("<p style='color: #64748B; font-size: 1.1rem;'>Real-time monitoring of the payment failure recovery pipeline.</p>", unsafe_allow_html=True)

    row = query_db(OVERVIEW_SQL)
    if row is None or row.empty:
        no_data("recovery activity")
    else:
        total = int(row["failures"].iloc[0])
        cases = int(row["cases"].iloc[0])
        recovered_cases = int(row["recovered_cases"].iloc[0])
        recovery_rate = (recovered_cases / cases * 100) if cases else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Failures", f"{total:,}")
        c2.metric(
            "Recovery Rate",
            f"{recovery_rate:.1f}%",
            delta=f"{recovery_rate:.1f}%", # using the same as delta to show green
            help="Recovery cases in a terminal 'recovered' state, over cases opened.",
        )
        c3.metric("Active Retries", int(row["pending"].iloc[0]))
        c4.metric(
            "₹ Recovered",
            f"₹{float(row['attributed_paise'].iloc[0]) / 100:,.0f}",
            help="Attributed to a payment link this engine sent."
        )
        if int(row["scheduled"].iloc[0]):
            st.caption(
                f"{int(row['scheduled'].iloc[0])} retries are scheduled for later."
            )

    st.markdown("<br>", unsafe_allow_html=True)
    
    # ── Architecture Visualizer ──────────────────────────────────────────────
    st.subheader("Pipeline Architecture")
    arch_cols = st.columns(6)
    
    layers = [
        ("📥", "Ingestion", "Webhooks & Events"),
        ("🧠", "Classifier", "XGBoost Models"),
        ("🤖", "Policy Agent", "Claude AI Logic"),
        ("🛡️", "Guardrails", "Safety Checks"),
        ("⚡", "Executor", "Razorpay Links"),
        ("⏰", "Scheduler", "Time Optimization")
    ]
    
    for col, (icon, title, desc) in zip(arch_cols, layers):
        with col:
            st.markdown(f"""
            <div class="arch-card">
                <div class="arch-icon">{icon}</div>
                <div class="arch-title">{title}</div>
                <div class="arch-desc">{desc}</div>
            </div>
            """, unsafe_allow_html=True)
            
    st.markdown("<br>", unsafe_allow_html=True)

    # ── Charts & Tables ──────────────────────────────────────────────────────
    col_left, col_right = st.columns([1.5, 1])
    
    with col_left:
        st.subheader("Recent Activity")
        recent = query_db("""
            SELECT ra.payment_id, ra.action_type, ra.result, ra.agent_type,
                   ra.channel, ra.guardrail_passed, ra.scheduled_at, ra.created_at
            FROM retry_attempts ra ORDER BY ra.created_at DESC LIMIT 20
        """)
        if recent is not None and len(recent) > 0:
            st.dataframe(recent, use_container_width=True, hide_index=True)
        else:
            st.info("No retry attempts recorded yet.")
            
    with col_right:
        st.subheader("Failure Class")
        fc = query_db(
            "SELECT failure_class AS \"Class\", COUNT(*) AS \"Count\" "
            "FROM payment_failures GROUP BY failure_class ORDER BY 2 DESC"
        )
        if fc is not None and not fc.empty:
            import plotly.express as px
            # Razorpay blues/greens palette
            palette = ['#2563EB', '#3B82F6', '#60A5FA', '#93C5FD', '#10B981', '#34D399', '#6EE7B7']
            fig = px.pie(
                fc,
                values="Count",
                names="Class",
                hole=0.6,
                color_discrete_sequence=palette,
            )
            fig.update_layout(
                margin=dict(l=20, r=20, t=20, b=20),
                showlegend=True,
                legend=dict(orientation="h", yanchor="bottom", y=-0.2, xanchor="center", x=0.5),
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)'
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No classified failures yet.")

elif clean_page == "Recovery Funnel":
    from dashboard.views import recovery_funnel
    recovery_funnel.render()
elif clean_page == "Bank Breakdown":
    from dashboard.views import bank_breakdown
    bank_breakdown.render()
elif clean_page == "Eval Results":
    from dashboard.views import eval_results
    eval_results.render()
