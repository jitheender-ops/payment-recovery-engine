"""
SQLAlchemy async engine, session factory, and Base class.

Uses asyncpg as the async Postgres driver. All database access goes through
the async session factory returned by get_session().
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase

from src.config import Settings, get_settings


# The models declare Postgres JSONB, which the SQLite type compiler cannot
# render at all — `create_all` raises CompileError before a single table is
# made. This one shim makes the whole schema portable.
#
# It lives here rather than in tests/conftest.py (its original home) because
# it now has two callers: the test harness, and the local demo, which runs
# the real app against a SQLite file so it needs no Postgres server. A second
# copy in the demo path would be one fact in two places, free to drift — and
# the failure mode of that drift is "the demo works and the tests do not",
# or worse, the reverse.
#
# Postgres is unaffected: @compiles registers this for the sqlite dialect
# only, so a real deployment still gets a real JSONB column.
@compiles(JSONB, "sqlite")
def _compile_jsonb_on_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    pass


_settings = get_settings()


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    """
    Engine options for the deployment this process is actually running in.

    Two shapes, and the difference is not cosmetic:

    LOCAL / DEDICATED POSTGRES — a normal pool. Every connection is ours for
    its lifetime, so pooling in the app is the whole story.

    A POOLER IN FRONT (Supabase Supavisor, PgBouncer) — the pooler is already
    the pool, and a second one underneath it holds server-side connections
    hostage on a plan that meters them. So: a small pool, recycled often, and
    `statement_cache_size=0`. That last one is the subtle one — asyncpg
    prepares statements by name on a connection it believes is its own, and a
    TRANSACTION-mode pooler hands the next transaction a different backend
    where that name does not exist. The failure ("prepared statement
    _asyncpg_stmt_1 does not exist") is intermittent, load-dependent, and
    reads like a data bug rather than a configuration one.

    The SQLite test harness accepts none of these arguments, so it takes the
    bare path — the same reason the JSONB shim in conftest exists.
    """
    if settings.database_url.startswith("sqlite"):
        return {}

    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if settings.db_behind_pooler:
        kwargs.update(
            pool_size=2,
            max_overflow=3,
            pool_recycle=300,
            connect_args={"statement_cache_size": 0},
        )
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
    return kwargs


engine = create_async_engine(
    _settings.database_url,
    echo=_settings.sql_echo,
    **_engine_kwargs(_settings),
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async session, auto-closes on exit."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all tables — for development only. Use Alembic in production."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Dispose of the engine connection pool."""
    await engine.dispose()
