"""
Voice recovery — the Hinglish call leg, and what it is allowed to say.

A call is the highest-friction touch the engine can make, so it is off by
default and gated behind its own secret. When it is on, a successful nudge
queues a call task that a telephony provider claims; the agent then answers
what the customer asks about their own case — the amount, the due date, why
the charge failed — in the language they used.

The number that matters on this page is not calls placed. It is what the agent
did when it could not ground an answer, because that is the failure mode with
teeth: a confident invented amount on a call about money is worse than no
answer at all. Four gates stand between a question and a reply — opt-out first,
a retrieval floor that abstains, sanitation of instructions hiding inside
retrieved text, and a grounding check the answer must pass against its cited
passage.

Queue depth is the operational read. Calls stuck in `queued` mean the
telephony leg is not claiming them, which looks like silence everywhere else.
"""

from __future__ import annotations

import streamlit as st

from dashboard import theme
from dashboard.db import query_db

QUEUE_SQL = """
SELECT COUNT(*)                                        AS total,
       COUNT(*) FILTER (WHERE state = 'queued')        AS queued,
       COUNT(*) FILTER (WHERE state = 'claimed')       AS claimed,
       COUNT(*) FILTER (WHERE state = 'done')          AS done,
       COUNT(*) FILTER (WHERE state = 'queued'
                          AND created_at < now() - interval '1 hour')
                                                       AS stuck,
       COALESCE(SUM(amount_paise), 0)                  AS amount
FROM voice_call_queue
"""

BY_TYPE_SQL = """
SELECT risk_type, state, COUNT(*) AS n
FROM voice_call_queue
GROUP BY 1, 2
ORDER BY n DESC
"""

RECENT_SQL = """
SELECT q.created_at AS "Queued", c.subject_ref AS "Case", q.risk_type AS "Type",
       q.amount_paise AS "Amount", q.state AS "State",
       COALESCE(q.claimed_by, '—') AS "Claimed by",
       COALESCE(q.result, '—') AS "Result"
FROM voice_call_queue q
LEFT JOIN recovery_cases c ON c.id = q.recovery_case_id
ORDER BY q.created_at DESC
LIMIT 20
"""

# Promises captured BY voice are the leg's actual payoff — a call that ends
# with a date beats a call that ends politely.
VOICE_PROMISES_SQL = """
SELECT COUNT(*)                                        AS made,
       COUNT(*) FILTER (WHERE status = 'kept')         AS kept,
       COUNT(*) FILTER (WHERE status = 'broken')       AS broken,
       COALESCE(SUM(amount_promised), 0)               AS amount
FROM promises_to_pay
WHERE channel = 'voice'
"""

ATTEMPTS_SQL = """
SELECT COALESCE(language, 'unset') AS language, COUNT(*) AS n
FROM retry_attempts
WHERE channel = 'voice'
GROUP BY 1 ORDER BY n DESC
"""


def render() -> None:
    """Render this page. Called by dashboard/app.py."""
    theme.page_header(
        "VOICE",
        "Hinglish call recovery",
        "The engine answers what the customer asks about their own case — "
        "grounded in that case's facts, or it says it does not know.",
    )

    q = query_db(QUEUE_SQL)
    if q is None or q.empty or int(q.iloc[0]["total"]) == 0:
        theme.empty_state(
            "The voice leg is idle",
            "Calls are off by default — VOICE_CHASER_ENABLED is false, and the "
            "webhook stays closed until VOICE_WEBHOOK_SECRET is set. A call is "
            "the highest-friction touch the engine can make and carries its own "
            "compliance posture: DoT registration and an AI disclosure at call "
            "start. Turn it on deliberately, not by default.",
            icon="pending",
            action_label="Enable on the API service, then redeploy",
            action_code="VOICE_CHASER_ENABLED=true\nVOICE_WEBHOOK_SECRET=<hex>\nSARVAM_API_KEY=<key>",
        )
        return

    row = q.iloc[0]
    stuck = int(row["stuck"])

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        theme.tile("Calls queued", f"{int(row['queued']):,}", icon="pending",
                   foot="waiting to be placed")
    with c2:
        theme.tile("In progress", f"{int(row['claimed']):,}", icon="open-case",
                   foot="claimed by the telephony leg")
    with c3:
        theme.tile("Completed", f"{int(row['done']):,}", icon="check",
                   foot="call finished")
    with c4:
        theme.tile(
            "Stuck > 1h", f"{stuck:,}", icon="warning",
            tone="clay" if stuck else "paper",
            foot="queued but never claimed" if stuck else "queue is draining",
        )

    if stuck:
        st.warning(
            f"{stuck} call task{'s' if stuck != 1 else ''} queued over an hour "
            "ago and never claimed. The engine has done its part — nothing "
            "downstream is picking them up, which looks like silence on every "
            "other page.",
            icon="⚠️",
        )

    # ── What the calls produced ──────────────────────────────────────────
    vp = query_db(VOICE_PROMISES_SQL)
    if vp is not None and not vp.empty and int(vp.iloc[0]["made"]) > 0:
        p = vp.iloc[0]
        made, kept, brk = int(p["made"]), int(p["kept"]), int(p["broken"])
        resolved = kept + brk
        st.divider()
        theme.section(
            "What the calls produced",
            "A call that ends with a date beats a call that ends politely. "
            "These are promises captured on the voice channel specifically.",
        )
        d1, d2, d3 = st.columns(3)
        with d1:
            theme.tile("Promises from calls", f"{made:,}", icon="check",
                       tone="brass", foot=theme.compact_inr(int(p["amount"])) + " promised")
        with d2:
            theme.tile(
                "Kept", f"{(kept / resolved * 100):.0f}%" if resolved else "—",
                icon="trend-up",
                foot=f"{kept:,} of {resolved:,} resolved" if resolved
                else "none resolved yet",
            )
        with d3:
            theme.tile("Broken", f"{brk:,}", icon="cross",
                       tone="clay" if brk else "paper",
                       foot="chase resumed")

    # ── Language ─────────────────────────────────────────────────────────
    langs = query_db(ATTEMPTS_SQL)
    if langs is not None and not langs.empty:
        st.divider()
        theme.section(
            "Language used",
            "The agent answers in the language the customer used. Hinglish is a "
            "real value here, not a fallback to English.",
        )
        st.dataframe(
            langs.rename(columns={"language": "Language", "n": "Attempts"}),
            use_container_width=True, hide_index=True,
        )

    # ── Queue by risk type ───────────────────────────────────────────────
    by_type = query_db(BY_TYPE_SQL)
    if by_type is not None and not by_type.empty:
        st.divider()
        theme.section("Queue by case type", "Which chasers are escalating to a call.")
        pivot = by_type.pivot_table(
            index="risk_type", columns="state", values="n",
            aggfunc="sum", fill_value=0,
        ).reset_index()
        st.dataframe(pivot, use_container_width=True, hide_index=True)

    # ── Recent ───────────────────────────────────────────────────────────
    recent = query_db(RECENT_SQL)
    if recent is not None and not recent.empty:
        st.divider()
        theme.section("Recent call tasks", "Newest first.")
        view = recent.copy()
        view["Amount"] = view["Amount"].apply(theme.inr)
        view["Queued"] = view["Queued"].apply(theme.fmt_ist)
        st.dataframe(view, use_container_width=True, hide_index=True)

    st.caption(
        "Four gates stand between a question and a reply: opt-out honoured "
        "first, a retrieval floor below which the agent abstains, sanitation of "
        "instructions hiding in retrieved text, and a grounding check the answer "
        "must pass against its cited passage. An unverifiable answer is a bug, "
        "never a feature."
    )
