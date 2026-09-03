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

# Per-key buckets, not one shared counter. A global lockout let anyone burn
# six guesses and lock the operator out of their own dashboard for five
# minutes — a free "annoy the admin" button. Keyed per client; an empty key
# (no proxy header and no socket peer) still gets its own bucket so it can
# never lock out everyone either.
# ponytail: process-global dict, unbounded key count — a flood of spoofed
# keys grows it. Cap with an LRU (or move behind a session store) if the
# dashboard is ever exposed beyond the operator's tunnel.
_FAILURES: dict[str, deque[float]] = {}
_LOCKED_UNTIL: dict[str, float] = {}


def _locked_out(key: str, now: float) -> bool:
    return now < _LOCKED_UNTIL.get(key, 0.0)


def _record_failure(key: str, now: float) -> None:
    bucket = _FAILURES.setdefault(key, deque())
    while bucket and now - bucket[0] > _FAILURE_WINDOW_SECONDS:
        bucket.popleft()
    bucket.append(now)
    if len(bucket) >= _MAX_FAILURES:
        _LOCKED_UNTIL[key] = now + _LOCKOUT_SECONDS
        bucket.clear()


def lockout_seconds_remaining(key: str = "") -> int:
    """Seconds until sign-in reopens, 0 when it is open. For the UI message."""
    return max(0, int(_LOCKED_UNTIL.get(key, 0.0) - time.monotonic()))


def reset_throttle(key: str = "") -> None:
    """Clear the failure history. For tests and for a successful sign-in."""
    _FAILURES.pop(key, None)
    _LOCKED_UNTIL.pop(key, None)


def dashboard_password() -> str:
    """The configured password, or "" when unset."""
    return os.getenv("DASHBOARD_PASSWORD", "")


def password_is_correct(supplied: str | None, key: str = "") -> bool:
    """
    True only when DASHBOARD_PASSWORD is set AND `supplied` matches it exactly.

    Fail-closed on an unset password. This dashboard reads live payment data and
    runs alongside a service published through a public tunnel; a gate that
    waves everyone through when unconfigured is the exact failure it exists to
    prevent, and "someone forgot to set it" is the likeliest way that happens.

    `key` is the per-client identity (X-Forwarded-For entry behind the tunnel
    proxy) that failures are bucketed under — one attacker's guesses no longer
    lock the operator out.
    """
    expected = dashboard_password()
    if not expected or not supplied:
        return False

    # Refuse to even compare while locked out. Checking first and rejecting
    # after would still answer the question the attacker is asking.
    now = time.monotonic()
    if _locked_out(key, now):
        return False

    # Bytes, not str: compare_digest raises TypeError on a str containing
    # non-ASCII, and the input here is whatever a visitor chose to type.
    if hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        reset_throttle(key)
        return True
    _record_failure(key, now)
    return False
