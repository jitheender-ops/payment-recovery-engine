"""
Live payment downtime — routing around a rail before spending an attempt on it.

Today `bank_downtime` is inferred AFTER the fact, from a decline that has
already cost an attempt. Razorpay publishes downtime while it is happening
(`GET /v1/payments/downtimes`), which turns that into something the engine
can act on beforehand: skip a rail the gateway itself reports as down and
switch immediately, rather than learning it the expensive way.

`src/executor/rail_selector.py` has carried a `ponytail:` note asking for
exactly this — "real downtime-aware routing needs a bank-health source we
don't have; wire one in and take `bank` as an argument when it exists."
This is that source.

TWO HONEST CAVEATS, both load-bearing:

1. **Downtime is an on-demand Razorpay feature.** Their docs say it must be
   enabled by contacting support, so a fresh test account may get 401/404
   here. That is why every failure path in this module degrades to "nothing
   is known to be down" rather than raising: an unavailable health feed must
   never stop the engine from chasing, only from being clever.

2. **The response schema was not in the documentation reachable when this
   was written.** The endpoint paths are confirmed; the field names below
   follow Razorpay's usual collection shape (`entity: "collection"`,
   `items: [...]`) and are parsed defensively — anything unrecognised is
   ignored rather than assumed. If the real payload differs, `_parse` is the
   one place to correct.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.config import get_settings, reveal

logger = logging.getLogger(__name__)

DOWNTIMES_URL = "https://api.razorpay.com/v1/payments/downtimes"

# A health signal that is minutes stale is still useful; one that is hours
# stale is worse than none, because it routes confidently around an outage
# that ended. Short, and re-fetched rather than trusted indefinitely.
_TTL = timedelta(minutes=5)

# Razorpay's status values for an active downtime. "resolved" is explicitly
# NOT here — a resolved row is history and must not steer routing.
_ACTIVE = {"started", "scheduled"}


@dataclass(frozen=True)
class Downtime:
    """One reported outage, reduced to what routing actually needs."""

    method: str               # card / upi / netbanking / wallet
    issuer: str | None        # bank or issuer code, when scoped to one
    severity: str | None      # razorpay's own word: high / medium / low
    status: str


@dataclass
class DowntimeSnapshot:
    """What was down at `fetched_at`, and the questions routing asks of it."""

    downtimes: list[Downtime]
    fetched_at: datetime
    available: bool = True

    def is_down(self, method: str | None, bank: str | None = None) -> bool:
        """
        True when the gateway reports this rail as impaired right now.

        A downtime with no issuer is method-wide and matches any bank; one
        scoped to an issuer matches only that issuer. Matching an
        issuer-scoped outage against every bank would route the whole book
        away from a rail because one bank is down, which is worse than the
        after-the-fact inference this replaces.
        """
        if not method:
            return False
        m = method.lower()
        for d in self.downtimes:
            if d.method.lower() != m:
                continue
            if d.issuer is None:
                return True
            if bank and d.issuer.lower() in bank.lower():
                return True
        return False

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "method": d.method,
                "issuer": d.issuer or "all issuers",
                "severity": d.severity or "unspecified",
                "status": d.status,
            }
            for d in self.downtimes
        ]


_EMPTY = DowntimeSnapshot(downtimes=[], fetched_at=datetime.now(UTC), available=False)
_cache: DowntimeSnapshot | None = None


def _parse(payload: dict[str, Any]) -> list[Downtime]:
    """Razorpay's collection shape, read defensively — see the module note."""
    out: list[Downtime] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").lower()
        if status not in _ACTIVE:
            continue
        method = item.get("method")
        if not method:
            continue
        # The issuer sits under different keys depending on the method
        # (card issuer vs netbanking bank vs UPI handle), so take whichever
        # is present rather than assuming one.
        instrument = item.get("instrument")
        issuer = None
        if isinstance(instrument, dict):
            issuer = (
                instrument.get("issuer")
                or instrument.get("bank")
                or instrument.get("psp")
            )
        out.append(Downtime(
            method=str(method),
            issuer=str(issuer) if issuer else None,
            severity=str(item.get("severity")) if item.get("severity") else None,
            status=status,
        ))
    return out


async def current(*, force: bool = False) -> DowntimeSnapshot:
    """
    What is down now, cached for a few minutes.

    Never raises. Every failure — unconfigured keys, the feature not enabled
    on the account, a timeout, a shape we do not recognise — returns a
    snapshot with `available=False`, which `is_down()` answers False for.
    A health feed that can stop the engine chasing is a liability, not a
    feature.
    """
    global _cache
    now = datetime.now(UTC)
    if not force and _cache is not None and now - _cache.fetched_at < _TTL:
        return _cache

    settings = get_settings()
    if settings.demo_mode:
        from src.demo import demo_downtime_payload

        snap = DowntimeSnapshot(_parse(demo_downtime_payload()), now)
        _cache = snap
        return snap

    key_id = settings.razorpay_key_id
    secret = reveal(settings.razorpay_key_secret)
    if not key_id or not secret:
        return _EMPTY

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(DOWNTIMES_URL, auth=(key_id, secret))
        if resp.status_code in (401, 403, 404):
            # Most likely the on-demand feature is not enabled on this
            # account. Log once at info, not error: it is a configuration
            # fact, not a fault, and an ERROR here would cry wolf forever.
            logger.info(
                "Downtime feed unavailable (HTTP %s) — routing falls back to "
                "after-the-fact inference. It is an on-demand Razorpay feature.",
                resp.status_code,
            )
            return _EMPTY
        resp.raise_for_status()
        snap = DowntimeSnapshot(_parse(resp.json()), now)
    except Exception:
        logger.warning("Downtime feed unreachable — continuing without it")
        return _EMPTY

    _cache = snap
    return snap


def reset_cache() -> None:
    """Drop the cached snapshot. For tests and for a forced refresh."""
    global _cache
    _cache = None
