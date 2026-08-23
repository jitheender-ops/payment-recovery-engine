"""
Password gate for the Streamlit dashboard.

The predicate lives here rather than in app.py because app.py IS a Streamlit
script: importing it executes the page top to bottom. A test asking "does the
wrong password get in" should not need a Streamlit runtime to answer.

Read through os.getenv, not src.config, because the dashboard is a separate
process that deliberately holds no import on the service package — run.sh
exports .env into the environment of both.
"""

from __future__ import annotations

import hmac
import os


def dashboard_password() -> str:
    """The configured password, or "" when unset."""
    return os.getenv("DASHBOARD_PASSWORD", "")


def password_is_correct(supplied: str | None) -> bool:
    """
    True only when DASHBOARD_PASSWORD is set AND `supplied` matches it exactly.

    Fail-closed on an unset password. This dashboard reads live payment data and
    runs alongside a service published through a public tunnel; a gate that
    waves everyone through when unconfigured is the exact failure it exists to
    prevent, and "someone forgot to set it" is the likeliest way that happens.
    """
    expected = dashboard_password()
    if not expected or not supplied:
        return False
    # Bytes, not str: compare_digest raises TypeError on a str containing
    # non-ASCII, and the input here is whatever a visitor chose to type.
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
