"""
FastAPI application entry point.

Configures the app, mounts routes, and manages the database lifecycle.
Run with: uvicorn src.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI

from src.config import get_settings
from src.database import close_db, init_db
from src.ingestion.router import router as webhook_router

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: init DB. Shutdown: close connections."""
    logger.info("Starting Payment Recovery Engine (env=%s)", settings.app_env)
    await init_db()
    logger.info("Database tables initialized")
    yield
    await close_db()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Payment Failure Recovery Engine",
    description=(
        "AI-powered payment failure recovery system. "
        "Deterministic guardrails wrap an LLM policy agent to decide "
        "whether, when, and on which rail to retry a failed payment."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# ── Routes ───────────────────────────────────────────────────────────────
app.include_router(webhook_router, prefix="/webhooks", tags=["webhooks"])


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Simple health check endpoint."""
    return {"status": "healthy", "env": settings.app_env}


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint — confirms the service is running."""
    return {
        "service": "Payment Failure Recovery Engine",
        "version": "0.1.0",
        "docs": "/docs",
    }
