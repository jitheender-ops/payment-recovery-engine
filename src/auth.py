"""
API key authentication for the non-webhook surface.

Kept in its own module rather than in main.py so routers can depend on it
without importing the app object back — that import would be circular.

The webhook route does NOT use this. Razorpay sends what Razorpay sends, and a
custom header is not on the list, so that endpoint authenticates by HMAC over
the raw request body (src/ingestion/signature.py). Two surfaces, two mechanisms,
each matched to what the caller can actually prove.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging

from fastapi import Header, HTTPException, Request, status

from src.config import get_settings, reveal

logger = logging.getLogger(__name__)


def _addr_in_cidrs(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, cidrs: str) -> bool:
    """
    Is `addr` inside any comma-separated IP/CIDR entry of `cidrs`?

    A malformed entry is skipped with a warning rather than raising: this runs
    on request paths, and a typo in one CIDR must not turn every request into
    a 500.
    """
    for entry in cidrs.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            logger.warning("Ignoring malformed allowlist entry %r", entry)
    return False


def client_ip(request: Request) -> str:
    """
    The client IP rate limits and lockouts key on.

    Behind a trusted proxy the RIGHTMOST X-Forwarded-For entry is the one the
    egress proxy added — the only hop a client cannot forge. The LEFTMOST is
    whatever the client sent, so trusting it lets an attacker rotate one header
    value per request and walk around every limit. Each trusted hop APPENDS the
    peer it saw, so the client sits `trusted_proxy_hops` entries from the
    right; assuming exactly one hop is wrong the moment a CDN goes in front of
    the platform LB. With no trusted proxy — or fewer entries than trusted
    hops, meaning the header was not written by the chain we expect — the
    header is attacker-controlled and ignored: the socket peer is the truth.

    THE PRECONDITION, AND WHO ENFORCES IT: reading the header at all assumes
    this request arrived through the proxy chain, because a request that did
    not has an attacker-controlled header and `entries[-hops]` is then the
    attacker's choice. The chain being the only way in is a network property
    no header can prove, so when TRUSTED_PROXY_IPS is set, client_ip() checks
    the socket peer against it first: a peer outside the list did NOT come
    through a trusted proxy, its header is discarded, and the socket peer is
    returned — a direct connection padding X-Forwarded-For cannot impersonate
    an address. When TRUSTED_PROXY_IPS is empty the header is trusted from
    any peer (Render's LB case), and the deployment precondition — the app
    must not be reachable outside the chain — is documented in
    Settings.trusted_proxy_ips and warned about at boot.
    """
    settings = get_settings()
    if settings.behind_trusted_proxy:
        hops = max(1, settings.trusted_proxy_hops)
        peer = request.client.host if request.client else "unknown"
        if settings.trusted_proxy_ips:
            try:
                peer_addr = ipaddress.ip_address(peer)
            except ValueError:
                peer_addr = None
            if peer_addr is None or not _addr_in_cidrs(
                peer_addr, settings.trusted_proxy_ips
            ):
                # Not one of our proxies: the whole header is attacker-typed.
                return peer
        entries = [
            part.strip()
            for part in request.headers.get("x-forwarded-for", "").split(",")
            if part.strip()
        ]
        if len(entries) >= hops:
            return entries[-hops]
        return peer
    return request.client.host if request.client else "unknown"


def ip_allowed(request: Request, allowlist: str) -> bool:
    """
    Is this request's client IP inside the configured allowlist?

    True when the allowlist is empty — an unset allowlist is OFF, not
    "deny everything" (see Settings.webhook_ip_allowlist for why this one
    guard deliberately does not fail closed).

    Entries are IPs or CIDRs, comma-separated. The address compared is
    client_ip()'s, so this inherits the trusted-proxy handling above rather
    than re-deriving it — and inherits its safety property too: with no
    trusted proxy configured, X-Forwarded-For is ignored and the socket peer
    is used, so nobody can spoof their way past this with a header.

    A malformed entry is skipped with a warning rather than raising. This runs
    on the webhook path, and a typo in one CIDR must not turn every incoming
    payment event into a 500.
    """
    if not allowlist.strip():
        return True

    peer = client_ip(request)
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        # "unknown" from client_ip when there is no peer, or something
        # unparseable. Fail closed HERE: the allowlist is on, and an address
        # we cannot evaluate is not an address we can vouch for.
        logger.warning("Webhook from unparseable client address %r — refused", peer)
        return False

    return _addr_in_cidrs(addr, allowlist)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """
    FastAPI dependency — 401 unless the caller presents the configured key.

    Fail-closed on an unset key. An unconfigured guard that waves everyone
    through is indistinguishable from having no guard at all, and this exists
    precisely for the deployment where someone forgot to set it.
    """
    expected = reveal(get_settings().api_key)
    # Compare bytes, not str: compare_digest raises TypeError on a str
    # containing non-ASCII, which an attacker controls via the header and which
    # would surface as a 500 instead of a 401.
    if (
        not expected
        or not x_api_key
        or not hmac.compare_digest(x_api_key.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key",
        )
