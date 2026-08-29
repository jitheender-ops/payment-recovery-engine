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
    """Rupees grouped the Indian way: 12,34,567 not 1,234,567."""
    whole = abs(int(paise)) // 100
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"₹{s}"


def ist(dt: datetime) -> datetime:
    """IST — the timezone the blackout and the bank run on. Naive means UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)
