"""
FastAPI application entry point.

Configures the app, mounts routes, and manages the database lifecycle.
Run with: uvicorn src.main:app --reload
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import RedirectResponse

from src import scheduler
from src.auth import require_api_key
from src.config import get_settings
from src.customer.routes import router as customer_router
from src.database import close_db, init_db
from src.ingestion.risk_router import router as risk_router
from src.ingestion.router import router as webhook_router
from src.merchant.receivables_api import router as receivables_api_router
from src.merchant.routes import router as merchant_router

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# The /recover/<token> URL IS the credential, and uvicorn's access log writes
# the request line verbatim — so every page view printed a live bearer token
# into the platform's log store, which is the one copy of it we control.
# recovery_link.py already reasons about SMS logs and browser history; this is
# the same class of leak and the only one we can close outright. The token is
# replaced with a short salted digest: enough to correlate a session's requests
# when debugging, useless to anyone reading the log.
class _RedactRecoveryToken(logging.Filter):
    """Strip recovery tokens out of uvicorn's access log request line."""

    _PATH = re.compile(r"(/recover/)([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access formats (client, method, full_path, http_version,
        # status). Anything else is not ours to rewrite.
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            redacted = self._PATH.sub(
                lambda m: m.group(1)
                + sha256(m.group(2).encode()).hexdigest()[:12]
                + "…redacted",
                args[2],
            )
            if redacted != args[2]:
                record.args = (*args[:2], redacted, *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactRecoveryToken())


# asyncio holds only a weak reference to a running task, so a bare local would
# be collectable mid-await. The set is what keeps the scheduler alive.
_background: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: init DB, start the scheduler. Shutdown: stop both."""
    logger.info("Starting Payment Recovery Engine (env=%s)", settings.app_env)
    settings.require_razorpay_credentials()
    # The other half: what is silently off rather than loudly broken.
    settings.require_production_integrity()
    # create_all only in development. It creates missing TABLES and silently
    # ignores missing COLUMNS, so on any database that predates a model change
    # it "succeeds" and the first write to the money path fails with
    # UndefinedColumn. Everywhere else the schema is Alembic's job, applied
    # before the process starts (docker-entrypoint.sh, run.sh).
    if settings.app_env == "development":
        await init_db()
        logger.info("Database tables initialized (create_all — development only)")
    else:
        logger.info(
            "Skipping create_all (env=%s) — schema is expected to be at "
            "alembic head. Run 'alembic upgrade head' if startup fails on a "
            "missing column.",
            settings.app_env,
        )
    # Fires deferred retry_at attempts, reconciles webhook events whose
    # BackgroundTask never ran, and expires promises to pay. Without it all
    # three are written to the database and never acted on.
    task = scheduler.start(_background)
    yield
    await scheduler.stop(task)
    await close_db()
    logger.info("Shutdown complete")


# The interactive docs enumerate every route, every schema field and every
# example — fine to hand a developer on localhost, not fine to hand whoever
# finds the public tunnel URL. Decided here rather than per-request because
# FastAPI wires these three URLs at construction time.
_docs_public = settings.app_env == "development"

app = FastAPI(
    title="Payment Failure Recovery Engine",
    description=(
        "AI-powered payment failure recovery system. "
        "Deterministic guardrails wrap an LLM policy agent to decide "
        "whether, when, and on which rail to retry a failed payment."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_public else None,
    redoc_url="/redoc" if _docs_public else None,
    openapi_url="/openapi.json" if _docs_public else None,
)

# ── Routes ───────────────────────────────────────────────────────────────
app.include_router(webhook_router, prefix="/webhooks", tags=["webhooks"])

# Merchant-pushed revenue-at-risk events (abandoned carts, failed subscription
# charges, overdue invoices, failed mandate debits). Like the Razorpay webhook,
# this authenticates by HMAC over the raw body (X-Risk-Signature) rather than
# require_api_key, because that is what a merchant's outbound webhook client
# can produce. With RISK_WEBHOOK_SECRET unset every event is rejected — the
# surface is closed until configured on purpose.
app.include_router(risk_router, prefix="/risks", tags=["risks"])

# The receivables merchant API: closures and verdicts the engine cannot
# decide for itself (external payments, dispute resolutions, call tasks).
# Same HMAC surface as /risks — the signer is the same merchant system, so
# the same secret and the same fail-closed rule.
app.include_router(
    receivables_api_router, prefix="/ar", tags=["receivables"]
)

# The customer-facing recovery page. Deliberately NOT behind require_api_key:
# the reader is a member of the public who just had a payment fail, and cannot
# hold an API key. Its authentication is the signed, expiring, single-case token
# in the URL (src/recovery_link.py), and with RECOVERY_LINK_SECRET unset every
# token is rejected, so the surface is closed until it is configured on purpose.
#
# include_in_schema=False: these routes are for humans with a link, not for API
# clients, and listing them in the schema only advertises the shape of the URL.
app.include_router(customer_router, tags=["customer"], include_in_schema=False)

# The merchant-facing surface: a public landing (/console) and a password-gated
# live console (/console/live). Like the customer routes these are HTML for
# humans, not API clients, so they stay out of the schema. The live console
# authenticates with the DASHBOARD_PASSWORD session cookie and fails closed when
# it is unset; the landing renders product facts only and no live numbers.
app.include_router(merchant_router, tags=["merchant"], include_in_schema=False)

# The voice surface: a provider-agnostic webhook (POST /voice/turn) that a
# telephony provider calls with a transcript and reads back a grounded,
# Hinglish reply. HMAC-signed like the other webhook surfaces — closed until
# VOICE_WEBHOOK_SECRET is set. Not in the schema: its caller is a provider's
# bridge, not a browsing human.
from src.voice.webhook import router as voice_router  # noqa: E402

app.include_router(voice_router, include_in_schema=False)

# The Plivo call leg (src/voice/plivo_bridge.py): the XML callbacks Plivo
# fetches during a live call, plus the TTS audio it plays. Same fail-closed
# secret as /voice/turn — every callback body is HMAC-checked, so an unset
# VOICE_WEBHOOK_SECRET closes this surface too. Mounted always: Plivo only
# gets pointed at it when a bridge is actually configured (PLIVO_*), and
# mounting it in demo/development costs nothing when idle.
from src.voice.plivo_bridge import router as plivo_router  # noqa: E402

app.include_router(plivo_router, include_in_schema=False)

# The demo gateway's stub checkout — mounted ONLY in demo mode, so the routes
# do not exist at all on a normal boot. Settings refuses demo_mode outside
# development (see Settings._demo_mode_is_development_only), so this is the
# second of two independent guards rather than the only one.
if settings.demo_mode:
    from src.demo import router as demo_router

    app.include_router(demo_router, include_in_schema=False)
    logger.warning(
        "DEMO MODE ACTIVE — the payment gateway is a local fake. "
        "Every 'recovered' rupee this process reports is fictional."
    )

if not _docs_public:
    # The schema itself stays useful outside development — client codegen and
    # contract tests want it — and unlike a browser loading Swagger UI, those
    # callers can send a header. So it comes back key-guarded instead of
    # vanishing. The two HTML UIs do not: their own fetch of this URL carries no
    # header, so serving them behind the key would just render an empty page.
    @app.get("/openapi.json", include_in_schema=False,
             dependencies=[Depends(require_api_key)])
    async def protected_openapi() -> dict[str, Any]:
        """OpenAPI schema, for authenticated machine clients only."""
        return app.openapi()


# The customer page renders an amount and hosts a POST "Pay" button, and its
# URL is a bearer credential. Neither fact was reflected in a response header:
# nothing stopped the page being framed for a UI-redress attack on the pay and
# opt-out buttons, and nothing stopped a shared browser or an intermediate
# cache keeping a copy of a page reached by a token. base.html already sets
# `referrer: no-referrer` and `noindex` in markup; these are the two that only
# a header can say. Scoped to /recover and /statement so the API surface is
# untouched.
@app.middleware("http")
async def _recovery_page_headers(request: Request, call_next: Any) -> Response:
    response: Response = await call_next(request)
    # /statement carries the same properties as /recover — a capability token
    # in the URL, a money page a stranger can reach — so it needs the same
    # four headers. One tuple, not a second copy of the block.
    if request.url.path.startswith(("/recover", "/statement")):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Referrer-Policy"] = "no-referrer"
    elif request.url.path.startswith(("/console", "/voice/demo", "/foundation")):
        # The operator surfaces carry live rupee figures, case references and
        # action buttons (dispute resolution, link admin) behind a session
        # cookie — the same UI-redress and shared-cache concerns the money
        # page has, minus the token-in-URL (so a lax referrer is fine and
        # `no-store` is the one that matters most: a console page left in a
        # shared-browser or proxy cache is a data leak). The public landing is
        # /console itself and frames safely too — DENY everywhere here is
        # the simpler, stricter posture.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        # Session-authed pages must never come from a cache; the public
        # landing joins them so a stale marketing page cannot outlive a
        # product change.
        response.headers["Cache-Control"] = "no-store, private"
    # Every HTML surface, token-bearing or not: MIME-sniffing off. A
    # template that ever echoes anything content-type-shaped must not be
    # interpreted as something else by an old proxy.
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/health")
async def health_check() -> dict[str, str]:
    """
    Liveness probe. Unauthenticated on purpose — probes cannot send a key.

    It therefore says nothing but "up": the env name it used to return told an
    anonymous caller whether the docs gate above was open.
    """
    return {"status": "healthy"}


# ── Brand assets: favicon + link-preview card ─────────────────────────────
# Served from memory, not a static mount: two small files a template would
# otherwise 404 on every page load (the favicon) or that must exist at a
# stable absolute URL for link unfurlers (the og:image), which cache by URL
# and cannot see session state anyway. Inline Response also keeps CSP simple
# — no new origins anywhere.

_FAVICON: bytes = (Path(__file__).parent / "static" / "favicon.svg").read_bytes()
_OG_IMAGE: bytes = (Path(__file__).parent / "static" / "og-default.svg").read_bytes()


@app.get("/favicon.svg", include_in_schema=False)
async def favicon() -> Response:
    return Response(content=_FAVICON, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/static/og-card.svg", include_in_schema=False)
async def og_card() -> Response:
    """The link-preview image. Absolute, cacheable, no secrets in it."""
    return Response(content=_OG_IMAGE, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Bare domain root — the scroll-told product story, not a JSON blob."""
    return RedirectResponse(url="/foundation")


@app.get("/status")
async def status() -> dict[str, str]:
    """Machine-readable service info — formerly served at '/'. See root()."""
    body = {
        "service": "Payment Failure Recovery Engine",
        "version": "0.1.0",
    }
    if _docs_public:
        body["docs"] = "/docs"
    return body
