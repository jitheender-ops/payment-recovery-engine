"""
FastAPI application entry point.

Configures the app, mounts routes, and manages the database lifecycle.
Run with: uvicorn src.main:app --reload
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI

from src import scheduler
from src.auth import require_api_key
from src.config import get_settings
from src.database import close_db, init_db
from src.ingestion.router import router as webhook_router

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# asyncio holds only a weak reference to a running task, so a bare local would
# be collectable mid-await. The set is what keeps the scheduler alive.
_background: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: init DB, start the scheduler. Shutdown: stop both."""
    logger.info("Starting Payment Recovery Engine (env=%s)", settings.app_env)
    settings.require_razorpay_credentials()
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


@app.get("/health")
async def health_check() -> dict[str, str]:
    """
    Liveness probe. Unauthenticated on purpose — probes cannot send a key.

    It therefore says nothing but "up": the env name it used to return told an
    anonymous caller whether the docs gate above was open.
    """
    return {"status": "healthy"}


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint — confirms the service is running."""
    body = {
        "service": "Payment Failure Recovery Engine",
        "version": "0.1.0",
    }
    if _docs_public:
        body["docs"] = "/docs"
    return body
