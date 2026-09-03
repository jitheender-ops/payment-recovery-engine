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
