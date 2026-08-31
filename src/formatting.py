"""Shared display formatting for the FastAPI surfaces.

The customer recovery page and the merchant console both render rupees and
timestamps; both used to carry their own copy. The dashboard keeps its own
richer variants (decimals, compact form) in dashboard/theme.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def money(paise: int) -> str:
    """Rupees grouped the Indian way: 12,34,567 not 1,234,567.

    The sign survives: a negative amount (a refund, a negative delta) must
    not render as its absolute value on a money surface.
    """
    n = int(paise)
    s = str(abs(n) // 100)
    if len(s) > 3:
        s = ",".join(_pair_right(s[:-3])) + "," + s[-3:]
    return f"{'-' if n < 0 else ''}₹{s}"


def _pair_right(head: str) -> list[str]:
    """Group digits in twos from the right: '1234' -> ['12','34'], '123' -> ['1','23']."""
    return [head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1]


def ist(dt: datetime) -> datetime:
    """IST — the timezone the blackout and the bank run on. Naive means UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)
