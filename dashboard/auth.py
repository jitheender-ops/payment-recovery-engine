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
import time
from collections import deque

# How many wrong passwords, over how long, before the door stops answering.
# A single shared static password with unlimited guesses is a password that
# falls to a script: compare_digest closes the timing channel and nothing
# closed the guessing one. Six tries a minute is invisible to someone typing
# and useless to someone iterating a wordlist.
_MAX_FAILURES = 6
_FAILURE_WINDOW_SECONDS = 60.0
_LOCKOUT_SECONDS = 300.0

# ponytail: process-global, not per-client. Streamlit gives this predicate no
# reliable client identity (the websocket session id is whatever a reconnecting
# client asks for), and there is exactly ONE password here — so the thing worth
# rate-limiting is guesses against that password, not guesses per visitor. The
# cost is that a burst of wrong entries locks out the legitimate operator for
# five minutes too. If that becomes a real irritation, move to per-IP buckets
# behind a proxy that supplies one.
_FAILURES: deque[float] = deque()
_LOCKED_UNTIL = 0.0


def _locked_out(now: float) -> bool:
    return now < _LOCKED_UNTIL


def _record_failure(now: float) -> None:
    global _LOCKED_UNTIL
    while _FAILURES and now - _FAILURES[0] > _FAILURE_WINDOW_SECONDS:
        _FAILURES.popleft()
    _FAILURES.append(now)
    if len(_FAILURES) >= _MAX_FAILURES:
        _LOCKED_UNTIL = now + _LOCKOUT_SECONDS
        _FAILURES.clear()


def lockout_seconds_remaining() -> int:
    """Seconds until sign-in reopens, 0 when it is open. For the UI message."""
    return max(0, int(_LOCKED_UNTIL - time.monotonic()))


def reset_throttle() -> None:
    """Clear the failure history. For tests and for a successful sign-in."""
    global _LOCKED_UNTIL
    _FAILURES.clear()
    _LOCKED_UNTIL = 0.0


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

    # Refuse to even compare while locked out. Checking first and rejecting
    # after would still answer the question the attacker is asking.
    now = time.monotonic()
    if _locked_out(now):
        return False

    # Bytes, not str: compare_digest raises TypeError on a str containing
    # non-ASCII, and the input here is whatever a visitor chose to type.
    if hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        reset_throttle()
        return True
    _record_failure(now)
    return False
