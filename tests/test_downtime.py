"""
Live downtime routing.

The feature is worth having because `bank_downtime` is otherwise inferred
only AFTER a decline has already cost an attempt. The feed lets the engine
skip a rail the gateway itself reports as impaired.

Two properties matter more than the routing:

**An unavailable feed must never stop the engine.** Downtime is an on-demand
Razorpay feature, so a fresh account gets 401/404. Every failure has to
degrade to "nothing is known to be down", because a health signal that can
talk the engine out of chasing is a liability.

**A resolved outage must not steer anything.** It is history, and routing
around an outage that ended is worse than not knowing about it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.downtime import Downtime, DowntimeSnapshot, _parse, current, reset_cache
from src.executor.rail_selector import select_alternative_rail


@pytest.fixture(autouse=True)
def _clean() -> Any:
    reset_cache()
    yield
    reset_cache()


def _snap(*downs: Downtime) -> DowntimeSnapshot:
    return DowntimeSnapshot(list(downs), datetime.now(UTC))


def test_a_method_wide_outage_matches_any_bank() -> None:
    snap = _snap(Downtime(method="netbanking", issuer=None, severity="high",
                          status="started"))
    assert snap.is_down("netbanking", "HDFC")
    assert snap.is_down("netbanking", None)
    assert not snap.is_down("upi", "HDFC")


def test_an_issuer_scoped_outage_matches_only_that_issuer() -> None:
    """
    The distinction that keeps this from being harmful: matching an
    issuer-scoped outage against every bank would route the entire book off
    a rail because one bank is down.
    """
    snap = _snap(Downtime(method="netbanking", issuer="PNB", severity="high",
                          status="started"))
    assert snap.is_down("netbanking", "PNB Bank")
    assert not snap.is_down("netbanking", "HDFC")


def test_a_resolved_outage_is_not_parsed() -> None:
    parsed = _parse({"items": [
        {"method": "upi", "status": "resolved", "instrument": {}},
        {"method": "card", "status": "started", "instrument": {}},
    ]})
    assert [d.method for d in parsed] == ["card"]


def test_an_unrecognised_payload_yields_nothing_rather_than_raising() -> None:
    """The schema was not in the reachable docs, so this must not be brittle."""
    for payload in ({}, {"items": None}, {"items": [1, 2, 3]},
                    {"items": [{"status": "started"}]}):
        assert _parse(payload) == []  # type: ignore[arg-type]


def test_routing_avoids_a_rail_the_gateway_says_is_down() -> None:
    snap = _snap(Downtime(method="upi", issuer=None, severity="high",
                          status="started"))
    # 3ds_dropoff normally prefers UPI. With UPI reported down it must not.
    assert select_alternative_rail("card", "3ds_dropoff") == "upi"
    assert select_alternative_rail("card", "3ds_dropoff", downtime=snap) != "upi"


def test_routing_still_picks_something_when_everything_is_down() -> None:
    """
    A degraded rail is a worse bet than a healthy one and a better bet than
    not trying. The feed narrows the choice; it must never empty it.
    """
    snap = _snap(
        Downtime(method="upi", issuer=None, severity="high", status="started"),
        Downtime(method="netbanking", issuer=None, severity="high", status="started"),
        Downtime(method="wallet", issuer=None, severity="high", status="started"),
    )
    assert select_alternative_rail("card", "3ds_dropoff", downtime=snap) is not None


def test_no_feed_leaves_routing_exactly_as_it_was() -> None:
    """The feature is additive: without a feed, the old heuristic stands."""
    for fclass in ("3ds_dropoff", "issuer_decline", "upi_collect_timeout",
                   "bank_downtime", "insufficient_funds"):
        assert (
            select_alternative_rail("card", fclass)
            == select_alternative_rail("card", fclass, downtime=None)
        )


async def test_an_unconfigured_account_reports_nothing_down(monkeypatch: Any) -> None:
    """
    The most likely real-world state: downtime is on-demand and not enabled.
    It must read as "nothing known to be down", never as an error that stops
    a chase.
    """
    from src.config import get_settings

    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "")
    get_settings.cache_clear()
    try:
        snap = await current(force=True)
    finally:
        get_settings.cache_clear()
    assert snap.available is False
    assert snap.is_down("upi") is False
    assert snap.is_down("card", "HDFC") is False


async def test_demo_mode_serves_the_documented_shape(monkeypatch: Any) -> None:
    from src.config import get_settings

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    try:
        snap = await current(force=True)
    finally:
        get_settings.cache_clear()
    # Two active outages; the resolved one in the payload must be dropped.
    assert snap.is_down("netbanking", "PNB")
    assert snap.is_down("wallet")
    assert not snap.is_down("upi"), "a resolved outage steered routing"
