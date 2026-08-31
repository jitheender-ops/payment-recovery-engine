"""
Database access for the dashboard, in one place.

Extracted from app.py so the view modules can query without importing the app —
app.py imports the views, so a view importing app.py back is a cycle that only
happens to work because the imports sit at the bottom of the file.

`no_data()` is the other half of why this module exists. Two pages used to draw
hardcoded numbers (a funnel of 1247 and np.random success rates) with no
indication they were invented, which is worse than an empty chart: an empty
chart says "nothing here yet" and a fabricated one says "here is your business".
Every page now routes its empty case through one honest message.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pandas as pd
import streamlit as st

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

DEFAULT_URL = "postgresql://recovery:recovery@localhost:5432/payment_recovery"


@st.cache_resource
def get_db_engine() -> Engine | None:
    """Connect to Postgres, or None if unreachable."""
    try:
        from sqlalchemy import create_engine

        engine = create_engine(os.getenv("DATABASE_URL_SYNC", DEFAULT_URL))
        engine.connect().close()
        return engine
    except Exception:
        return None


@st.cache_data(ttl=30)
def query_db(
    query: str, params: dict[str, Any] | None = None
) -> pd.DataFrame | None:
    """
    Run a read query. None when the database is unreachable or the query fails.

    `params` are bound parameters (SQLAlchemy named style), so a filtered page
    never formats user-adjacent values into SQL text.
    """
    engine = get_db_engine()
    if engine is None:
        return None
    try:
        return pd.read_sql(query, engine, params=params or {})
    except Exception:
        return None


def no_data(what: str) -> bool:
    """
    Render the honest empty state and return True when there is nothing to draw.

    NOT a predicate, despite the name — it always renders and always returns
    True. The caller decides there is nothing to draw; this only says so well.
    The idiom is therefore:

        df = query_db(SQL)
        if df is None or df.empty:
            no_data("engine activity")
            return

    and never `if no_data(...): return`, which stops every page unconditionally.

    Two distinct moments, because they mean different things (2026 empty-state
    guidance: distinguish "not configured" from "no data yet"): the database is
    unreachable, or the tables are simply quiet.
    """
    if get_db_engine() is None:
        from dashboard import theme

        theme.empty_state(
            "Database not connected",
            f"No {what} to show. This console renders live data only — start "
            "Postgres and reload.",
            icon="disconnected",
        )
        return True

    from dashboard import theme

    theme.empty_state(
        f"No {what} recorded yet",
        "The engine is honest about zero: an empty chart says 'nothing yet', "
        "a fabricated one says 'here is your business'. Drive the webhook "
        "endpoint and this page fills with measured reality.",
        # An icon NAME from theme._ICONS, never a literal glyph. This was "◌",
        # which is not a key, so theme.icon() raised KeyError and took the whole
        # page down — on precisely the path meant to handle emptiness gracefully.
        icon="pending",
        action_code="python scripts/simulate_webhooks.py --count 20",
    )
    return True


def frame_is_empty(df: pd.DataFrame | None) -> bool:
    """True when a query returned nothing usable."""
    return df is None or df.empty or bool(df.iloc[:, -1].fillna(0).eq(0).all())
