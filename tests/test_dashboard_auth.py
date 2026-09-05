"""
The dashboard must not be readable by whoever finds the port.

Streamlit binds a listening socket like any other server, and this one renders
payment identifiers, recovery rates and rupee totals straight out of the
production database — on the same host as a service published through a public
tunnel.

These tests exercise the predicate, not the UI. Driving Streamlit's widget
runtime would test Streamlit; what can actually be got wrong here is the
comparison and the unset-password default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dashboard.auth import dashboard_password, password_is_correct

PASSWORD = "s3cret-demo-password"
REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_right_password_gets_in(monkeypatch: Any) -> None:
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)

    assert password_is_correct(PASSWORD) is True


def test_the_wrong_password_does_not(monkeypatch: Any) -> None:
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)

    assert password_is_correct("hunter2") is False
    # Not a prefix match, not a substring match, not case-insensitive.
    assert password_is_correct(PASSWORD[:-1]) is False
    assert password_is_correct(PASSWORD + "x") is False
    assert password_is_correct(PASSWORD.upper()) is False


def test_an_unset_password_locks_the_door_rather_than_removing_it(monkeypatch: Any) -> None:
    """
    The likeliest way this gate fails is nobody configuring it. Denying is the
    only safe reading: an empty expected value compares equal to an empty
    supplied one, which would make "" the password.
    """
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    assert dashboard_password() == ""
    assert password_is_correct("") is False
    assert password_is_correct("anything") is False
    assert password_is_correct(None) is False


def test_empty_input_against_a_set_password_is_rejected(monkeypatch: Any) -> None:
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)

    assert password_is_correct("") is False
    assert password_is_correct(None) is False


def test_non_ascii_input_is_rejected_not_raised(monkeypatch: Any) -> None:
    """
    compare_digest raises TypeError on a non-ASCII str, and this input comes off
    a public form. Encoding both sides keeps a wrong password a wrong password
    instead of a 'Something went wrong' page — or worse, a traceback.
    """
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)

    assert password_is_correct("pässwörd") is False
    assert password_is_correct("パスワード") is False


def test_one_clients_lockout_does_not_lock_out_another(monkeypatch: Any) -> None:
    """
    EXPLOIT: the throttle was one shared counter, so anyone burning six wrong
    guesses locked the operator out of their own dashboard for five minutes —
    a free "annoy the admin" button. Failures are bucketed per client key now:
    the attacker's bucket locks, the operator's stays open.
    """
    import dashboard.auth as auth

    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    try:
        for _ in range(6):
            assert auth.password_is_correct("wrong", key="attacker") is False
        # The attacker is locked out...
        assert auth.password_is_correct("wrong", key="attacker") is False
        assert auth.lockout_seconds_remaining("attacker") > 0
        # ...but the operator still signs in on the first try.
        assert auth.password_is_correct(PASSWORD, key="operator") is True
        assert auth.lockout_seconds_remaining("operator") == 0
    finally:
        auth.reset_throttle("attacker")
        auth.reset_throttle("operator")


def test_the_throttle_prunes_stale_keys(monkeypatch: Any) -> None:
    """A flood of wrong guesses from many spoofed keys must not grow the
    maps without bound — past the GC threshold, rolled-off buckets and
    expired locks are dropped, while live locks and fresh buckets survive
    untouched, because that is the throttling actually working.
    """
    import time
    from collections import deque

    import dashboard.auth as auth

    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    # The maps are process-global; save and restore so the rest of the suite
    # (which shares them) is unaffected.
    saved_failures = auth._FAILURES
    saved_locked = auth._LOCKED_UNTIL
    auth._FAILURES = {}
    auth._LOCKED_UNTIL = {}
    try:
        monkeypatch.setattr(auth, "_GC_AT", 2)
        now = time.monotonic()

        # Five distinct spoofed keys whose failures rolled off the window and
        # whose locks have expired — pure dead weight.
        for i in range(5):
            key = f"spoofed-{i}"
            auth._FAILURES[key] = deque([now - 120.0])
            auth._LOCKED_UNTIL[key] = now - 1.0
        # A live lockout must survive the sweep or the throttle stops
        # throttling.
        auth._FAILURES["live-lock"] = deque([now])
        auth._LOCKED_UNTIL["live-lock"] = now + 300.0
        # A fresh failure with no lock: bucket stays (its window is live).
        auth._FAILURES["fresh"] = deque([now])

        auth._gc_state(now)

        assert "live-lock" in auth._FAILURES
        assert "live-lock" in auth._LOCKED_UNTIL
        assert "fresh" in auth._FAILURES
        for i in range(5):
            assert f"spoofed-{i}" not in auth._FAILURES
            assert f"spoofed-{i}" not in auth._LOCKED_UNTIL

        # And the write path sweeps too: recording a failure past the
        # threshold drops the stale set.
        auth._FAILURES["stale-again"] = deque([now - 120.0])
        auth._LOCKED_UNTIL["stale-again"] = now - 1.0
        auth._record_failure("new-key", now)
        assert "stale-again" not in auth._FAILURES
        assert "stale-again" not in auth._LOCKED_UNTIL
        assert "new-key" in auth._FAILURES
    finally:
        auth._FAILURES = saved_failures
        auth._LOCKED_UNTIL = saved_locked


def test_the_views_directory_is_not_named_pages(monkeypatch: Any) -> None:
    """
    Streamlit auto-registers every module under a directory named `pages/` next
    to the entrypoint as its own URL. Those URLs render before app.py's gate has
    any say, so re-creating dashboard/pages/ silently republishes the whole
    dashboard unauthenticated. This is the tripwire for that.
    """
    assert (REPO_ROOT / "dashboard" / "views").is_dir()
    assert not (REPO_ROOT / "dashboard" / "pages").exists(), (
        "dashboard/pages/ is Streamlit's magic multipage directory — its modules "
        "become routable URLs that bypass the password gate in app.py"
    )


def test_the_lockout_key_reads_an_attribute_streamlit_actually_has() -> None:
    """
    `_lockout_key()` runs at module scope on every sign-in attempt, before any
    test in this file gets a say, and Streamlit's context object is not part of
    its API contract. It read `st.context.ip` — which does not exist — so the
    console crashed with an AttributeError the moment a request arrived with no
    X-Forwarded-For header, i.e. every local run. Nothing here drives Streamlit,
    so only this asserts the attribute is real.
    """
    import streamlit as st

    assert hasattr(st.context, "ip_address")
    assert "st.context.ip_address" in (
        (REPO_ROOT / "dashboard" / "app.py").read_text()
    )
