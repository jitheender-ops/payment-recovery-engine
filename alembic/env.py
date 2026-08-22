"""Alembic env.py — auto-generates migrations from ORM models."""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from src.config import get_settings
from src.database import Base
from src.models import *  # noqa: F401,F403 — import all models for autogenerate

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The URL comes from settings, overriding alembic.ini's hardcoded
# postgresql://...@localhost:5432/... — which is right for a laptop and wrong
# everywhere else. In a container "localhost" is the container itself, so
# migrations died with "connection to server at localhost (::1) port 5432
# failed: Connection refused" while Postgres sat healthy one DNS name away.
#
# Reading it here also routes it through Settings' normalisation, so a
# platform-injected `postgresql://` or Heroku's legacy `postgres://` lands with
# the right driver without anyone editing the ini. An explicit -x url= still
# wins, for pointing a migration at a scratch database.
_url_override = context.get_x_argument(as_dictionary=True).get("url")
config.set_main_option(
    "sqlalchemy.url", _url_override or get_settings().database_url_sync
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
