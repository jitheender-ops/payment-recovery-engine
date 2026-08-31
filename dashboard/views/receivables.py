"""
B2B receivables — the ladder, and who is standing on each rung.

A business buyer is not one case. It is an ACCOUNT with several overdue
invoices and several people who could pay them, which is why this layer exists
separately from the per-case chase: it picks one carrier case per account,
defers the rest, and sends one consolidated statement. A buyer with four
overdue invoices gets one message, not four.

The ladder is the page's spine because escalation by ROLE is the whole
mechanism — courtesy to accounts payable, then firm, then urgent to the
finance manager, then final to their escalation contact. Rungs are read from
src/receivables/ladder.py so this page cannot describe an escalation the
engine does not run.

Disputes get their own panel and sit above the aging table on purpose. A
disputed invoice is frozen: no chaser touches it until a human resolves the
dispute, so it is the one thing here that needs a person rather than a sweep.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import theme
from dashboard.db import query_db

ACCOUNTS_SQL = """
SELECT a.account_ref, a.display_name,
       COUNT(c.id)                                        AS invoices,
       COUNT(c.id) FILTER (WHERE c.state = 'open')        AS open_invoices,
       COALESCE(SUM(c.amount_at_risk - c.amount_recovered)
                FILTER (WHERE c.state = 'open'), 0)       AS outstanding,
       MAX(EXTRACT(DAY FROM now() - c.due_at))            AS worst_days_over
FROM ar_accounts a
LEFT JOIN recovery_cases c ON c.account_id = a.id
GROUP BY a.id, a.account_ref, a.display_name
ORDER BY outstanding DESC
LIMIT 25
"""

RUNGS_SQL = """
SELECT stage_level, COUNT(DISTINCT account_id) AS accounts,
       COUNT(*) AS contacts, MAX(sent_at) AS last_sent
FROM ar_contact_log
GROUP BY stage_level
ORDER BY stage_level
"""

DISPUTES_SQL = """
SELECT d.opened_at AS "Opened", c.subject_ref AS "Invoice",
       d.reason AS "Reason", d.status AS "Status"
FROM case_disputes d
LEFT JOIN recovery_cases c ON c.id = d.case_id
ORDER BY d.status = 'open' DESC, d.opened_at DESC
LIMIT 20
"""

AGING_SQL = """
SELECT
  CASE
    WHEN now() - due_at < interval '0 day'  THEN '0 not yet due'
    WHEN now() - due_at < interval '8 day'  THEN '1 to 7 days'
    WHEN now() - due_at < interval '15 day' THEN '2 8 to 14 days'
    WHEN now() - due_at < interval '31 day' THEN '3 15 to 30 days'
    ELSE '4 over 30 days'
  END                                                      AS bucket,
  COUNT(*)                                                 AS invoices,
  COALESCE(SUM(amount_at_risk - amount_recovered), 0)      AS outstanding
FROM recovery_cases
WHERE risk_type = 'invoice_overdue' AND state = 'open' AND due_at IS NOT NULL
GROUP BY bucket
ORDER BY bucket
"""


def _ladder_rungs() -> list[dict[str, object]]:
    """The frozen escalation ladder, from the module that enforces it."""
    from src.receivables.ladder import INVOICE_LADDER

    roles = {
        "ap_clerk": "accounts payable",
        "finance_manager": "the finance manager",
        "escalation": "their escalation contact",
    }
    return [
        {
            "level": i + 1,
            "tone": s.tone,
            "days": s.days_past_due,
            "addresses": roles.get(s.addresses[-1], s.addresses[-1]),
        }
        for i, s in enumerate(INVOICE_LADDER)
    ]


def render() -> None:
    """Render this page. Called by dashboard/app.py."""
    theme.page_header(
        "RECEIVABLES",
        "B2B invoice chases",
        "A business buyer is an account, not a case. One contact per account "
        "per rung — four overdue invoices get one statement, not four messages.",
    )

    accounts = query_db(ACCOUNTS_SQL)
    if accounts is None or accounts.empty:
        theme.empty_state(
            "No buyer accounts yet",
            "Push an invoice_overdue risk event carrying an account reference "
            "and the engine groups that buyer's invoices into one account, then "
            "walks the ladder.",
            action_label="Drive synthetic B2B traffic",
            action_code="python scripts/run_risk_batch.py --count 24",
        )
        return

    outstanding = int(accounts["outstanding"].sum())
    open_inv = int(accounts["open_invoices"].sum())
    worst = accounts["worst_days_over"].max()

    c1, c2, c3 = st.columns(3)
    with c1:
        theme.tile("Outstanding", theme.compact_inr(outstanding), icon="money",
                   foot=f"{open_inv:,} open invoices")
    with c2:
        theme.tile("Buyer accounts", f"{len(accounts):,}", icon="open-case",
                   foot="each gets one contact per rung")
    with c3:
        theme.tile(
            "Oldest overdue",
            f"{int(worst)}d" if pd.notna(worst) else "—",
            icon="hourglass",
            tone="clay" if pd.notna(worst) and int(worst) > 30 else "paper",
            foot="days past its due date",
        )

    # ── The ladder ───────────────────────────────────────────────────────
    st.divider()
    theme.section(
        "The ladder",
        "Where each buyer sits. Rungs climb because escalation climbs, and who "
        "it addresses hardens as it goes.",
    )

    rungs = _ladder_rungs()
    fired = query_db(RUNGS_SQL)
    by_level = (
        {int(r["stage_level"]): r for _, r in fired.iterrows()}
        if fired is not None and not fired.empty
        else {}
    )

    for rung in rungs:
        lvl = int(rung["level"])
        hit = by_level.get(lvl)
        n = int(hit["accounts"]) if hit is not None else 0
        indent = (lvl - 1) * 22
        # Built outside the f-string below: nesting the same quote character
        # inside an f-string is PEP 701, i.e. 3.12+, and this project's floor
        # is 3.11 (CI runs both).
        day_label = "due date" if int(rung["days"]) == 0 else f"day {rung['days']}"
        count_label = f"{n} account{'s' if n != 1 else ''}" if n else "—"
        st.markdown(
            f"<div style='margin-left:{indent}px;display:flex;align-items:center;"
            f"gap:.8rem;padding:.55rem .8rem;border:1px solid {theme.LINE};"
            f"border-radius:9px;margin-bottom:.35rem;"
            f"background:{'rgba(165,129,8,0.06)' if n else 'transparent'};'>"
            f"<span style='font-weight:600;text-transform:capitalize;min-width:5.5rem;'>"
            f"{rung['tone']}</span>"
            f'<span style="font-family:{theme.FONT_MONO};font-size:.78rem;'
            f'color:{theme.SLATE};min-width:5rem;">'
            f"{day_label}</span>"
            f"<span style='color:{theme.SLATE};font-size:.85rem;flex:1;'>"
            f"{rung['addresses']}</span>"
            f'<span style="font-family:{theme.FONT_MONO};font-size:.85rem;'
            f'color:{theme.BRASS_TEXT if n else theme.SLATE};">'
            f"{count_label}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

    if not by_level:
        st.caption(
            "No rung has fired yet. The ladder advances on the scheduler's "
            "consolidation sweep, inside Mon–Fri 09:30–18:30 IST."
        )

    # ── Disputes: the one thing here needing a person ────────────────────
    disputes = query_db(DISPUTES_SQL)
    if disputes is not None and not disputes.empty:
        st.divider()
        open_n = int((disputes["Status"] == "open").sum())
        theme.section(
            "Disputes",
            f"{open_n} open. A disputed invoice is frozen — no chaser touches "
            "it until a human resolves it, because arguing about an invoice is "
            "how a customer is lost rather than recovered.",
        )
        st.dataframe(disputes, use_container_width=True, hide_index=True)

    # ── Aging ────────────────────────────────────────────────────────────
    aging = query_db(AGING_SQL)
    if aging is not None and not aging.empty:
        st.divider()
        theme.section("Aging", "Open invoice value by how far past due it is.")
        aging["bucket"] = aging["bucket"].str.replace(r"^\d ", "", regex=True)
        aging["outstanding"] = aging["outstanding"].apply(theme.inr)
        st.dataframe(
            aging.rename(columns={
                "bucket": "Past due", "invoices": "Invoices",
                "outstanding": "Outstanding",
            }),
            use_container_width=True, hide_index=True,
        )

    # ── Accounts ─────────────────────────────────────────────────────────
    st.divider()
    theme.section("Buyer accounts", "Largest outstanding first.")
    view = accounts.copy()
    view["outstanding"] = view["outstanding"].apply(theme.inr)
    view["worst_days_over"] = view["worst_days_over"].apply(
        lambda v: f"{int(v)}d" if pd.notna(v) else "—"
    )
    st.dataframe(
        view.rename(columns={
            "account_ref": "Account", "display_name": "Name",
            "invoices": "Invoices", "open_invoices": "Open",
            "outstanding": "Outstanding", "worst_days_over": "Oldest",
        }),
        use_container_width=True, hide_index=True,
    )
