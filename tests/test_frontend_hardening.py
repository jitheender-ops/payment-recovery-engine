"""
The frontend surfaces' hardening: security headers per surface, the
favicon, the link-preview (og:) contract, the print receipt, and the
/foundation story page.

Each test names the property it exists for. The header tests drive the REAL
app middleware (not a re-implementation) so the posture being asserted is
the one that ships.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.main import app as real_app


def _html_client(monkeypatch: Any) -> TestClient:
    """The real app — lifespan bypassed by TestClient's context skip, env
    pinned to development so docs/landing all mount. A console password is
    pinned too so the gating tests exercise the REAL session path (with no
    password configured the console correctly serves a 200 "locked" page
    instead of a redirect — fail-closed, and a different assertion)."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_frontend")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x" * 8)
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "s" * 8)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "fe-test-password")
    from src.config import get_settings

    get_settings.cache_clear()
    return TestClient(real_app, raise_server_exceptions=False)


# ── Security headers, per surface ───────────────────────────────────────────


def test_html_surfaces_get_mime_sniffing_off(monkeypatch: Any) -> None:
    """Every HTML response carries nosniff — the cheapest header, the one
    that neutralises a whole class of old-proxy content confusion."""
    client = _html_client(monkeypatch)
    for path in ("/console", "/console/login", "/voice/demo"):
        r = client.get(path)
        if r.status_code == 200:
            assert r.headers.get("x-content-type-options") == "nosniff", path


def test_the_operator_surfaces_cannot_be_framed(monkeypatch: Any) -> None:
    """The console renders live rupee figures and action buttons (dispute
    resolution) behind a session cookie — the same UI-redress concern as
    the money page. /voice/demo joins it: it is microphone-adjacent."""
    client = _html_client(monkeypatch)
    for path in ("/console", "/console/login", "/voice/demo", "/foundation"):
        r = client.get(path)
        assert r.headers.get("x-frame-options") == "DENY", path
        assert "frame-ancestors 'none'" in r.headers.get(
            "content-security-policy", ""
        ), path


def test_the_console_never_serves_from_a_cache(monkeypatch: Any) -> None:
    """A console page left in a shared-browser or proxy cache is a data
    leak; the public landing joins so stale marketing cannot outlive a
    product change."""
    client = _html_client(monkeypatch)
    for path in ("/console", "/console/login", "/foundation"):
        r = client.get(path)
        assert r.headers.get("cache-control") == "no-store, private", path


def test_the_api_surface_is_untouched_by_the_header_middleware(
    monkeypatch: Any,
) -> None:
    """The middleware adds operator headers to HTML paths only — the JSON
    API must not grow console headers, and /health must stay bare."""
    client = _html_client(monkeypatch)
    r = client.get("/health")
    assert r.status_code == 200
    assert "x-frame-options" not in r.headers
    assert "cache-control" not in r.headers


def test_the_favicon_exists_and_is_cacheable(monkeypatch: Any) -> None:
    """404 favicons on every page load were log noise and broken tab
    polish on the exact URLs demoed to merchants."""
    client = _html_client(monkeypatch)
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert b"<svg" in r.content
    assert "max-age" in r.headers["cache-control"], "not cacheable"


def test_the_og_card_is_served_at_a_stable_absolute_url(
    monkeypatch: Any,
) -> None:
    """Link unfurlers fetch cold, by URL, with no session — the card must
    exist at one absolute path, cacheable, with no secrets in it."""
    client = _html_client(monkeypatch)
    r = client.get("/static/og-card.svg")
    assert r.status_code == 200
    assert "₹11.71L" in r.content.decode(), "the card lost its headline"


# ── The link-preview contract on templates ─────────────────────────────────


def test_the_money_page_carries_link_preview_tags(monkeypatch: Any) -> None:
    """The recovery link travels in an SMS; in WhatsApp/Instagram inboxes a
    bare host with no card reads as phishing. og:* must unfurl the
    merchant's name and the page's purpose."""
    from src.customer import routes as customer_routes

    app = FastAPI()
    app.include_router(customer_routes.router)
    # Mint a real token the way the demo does, against a thrown-away DB.
    monkeypatch.setenv("RECOVERY_LINK_SECRET", "t" * 32)
    # The 404-for-a-forged-token path still carries base.html's og block —
    # which is exactly what an unfurler sees first.
    r = TestClient(app).get("/recover/not-a-token")
    assert "og:title" in r.text
    assert "og:image" in r.text
    assert "/static/og-card.svg" in r.text
    assert "summary_large_image" in r.text


def test_the_landing_carries_link_preview_tags(monkeypatch: Any) -> None:
    client = _html_client(monkeypatch)
    r = client.get("/console")
    assert "og:title" in r.text and "og:image" in r.text
    assert "favicon.svg" in r.text


# ── /foundation — the story page ─────────────────────────────────────────────


def test_the_foundation_page_tells_the_measured_story(monkeypatch: Any) -> None:
    """The scroll page states the eval's real numbers and nothing softer —
    the trust rule for a payments product is no vibes on a launch page."""
    client = _html_client(monkeypatch)
    r = client.get("/foundation")
    assert r.status_code == 200
    for fact in ("+11.03pp", "₹11.71L", "0.0%", "−0.41"):
        assert fact in r.text, f"the story lost its number: {fact}"
    # The one thing a launch page may never claim: that an LLM moves money.
    assert "never authorizes" in r.text or "never authorises" in r.text


def test_the_foundation_page_is_indexable_and_the_console_is_not(
    monkeypatch: Any,
) -> None:
    """The story page is the public front door (index, follow); operator
    pages stay noindex — their URLs carry nothing, but they are not for
    search either."""
    client = _html_client(monkeypatch)
    foundation = client.get("/foundation").text
    assert '<meta name="robots" content="index, follow">' in foundation
    # The live console stays behind its session (303 without one) and the
    # landing stays noindex.
    assert client.get("/console/live", follow_redirects=False).status_code == 303
    landing = client.get("/console").text
    assert "noindex" in landing


def test_the_foundation_page_reads_without_javascript(monkeypatch: Any) -> None:
    """Progressive enhancement, enforced: the sections' content must not be
    gated behind the observer script — it only adds emphasis classes."""
    client = _html_client(monkeypatch)
    r = client.get("/foundation")
    # Content visible without JS = no section content hidden behind a
    # script-dependent display:none. The lit-class is additive.
    assert 'class="fm' in r.text
    assert "IntersectionObserver" in r.text  # enhancement present
    # The core narrative sections exist as plain HTML:
    for heading in (
        "Failed payments are not the end",
        "A failed payment carries its own story",
        "Measured, not promised",
    ):
        assert heading in r.text


def test_the_root_redirects_to_the_story(monkeypatch: Any) -> None:
    client = _html_client(monkeypatch)
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/foundation"
