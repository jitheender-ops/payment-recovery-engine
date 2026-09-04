"""Prove the migration chain builds the schema the ORM expects.

Three paths, because they fail differently and only one of them was ever run:

  1. EMPTY -> head, then diff against the ORM schema. This is the deploy path,
     and it is the one that shipped broken: 0001/0002 only ALTER the core tables,
     so on a genuinely empty database `upgrade head` reported success and left a
     schema with `recovery_cases` and no `retry_attempts`. Invisible on any dev
     box, fatal on first deploy — the one place the database is always empty.
  2. create_all -> head, which must no-op. This is the dev-box path, and it
     catches the opposite drift: a model gains a column, the migration does not.
  3. head -> base -> head, so `downgrade` is something that has actually run.

Runs on SQLite by default, so CI needs no service container for the logic this
checks: the inspector guards, the ordering, and completeness against the models.

That default was not enough. Twice now a migration passed here and died on the
first Postgres deploy, on SQL SQLite does not type-check: a JSONB server_default
SQLAlchemy quoted into `'''[]'''`, and a text literal assigned to a jsonb column
(`SET credited_refs = '["' || recovered_ref || '"]'`) that Postgres rejects at
plan time. SQLite stores both happily. So `--postgres` runs the same three paths
against the dialect that actually deploys; it creates its own scratch databases
on that server and drops them, so it can be pointed at any Postgres — including
a dev box — without touching an existing schema.

    python scripts/check_migrations.py
    python scripts/check_migrations.py --postgres postgresql://user:pw@localhost/postgres
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from argparse import Namespace
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

sys.path.insert(0, str(Path(__file__).parent.parent))

from alembic.config import Config  # noqa: E402

import src.models  # noqa: E402,F401  (registers every table on Base.metadata)
import src.receivables.models  # noqa: E402,F401  (receivables tables, same metadata)
from alembic import command  # noqa: E402
from src.database import Base  # noqa: E402


# Same shim as tests/conftest.py: the models declare Postgres JSONB, which the
# SQLite type compiler cannot render. The migrations use with_variant instead, so
# this is needed only for the create_all half of the comparison.
@compiles(JSONB, "sqlite")
def _compile_jsonb_on_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


Snapshot = dict[str, dict[str, Any]]


def _config(url: str) -> Config:
    # `-x url=` rather than set_main_option: alembic/env.py deliberately
    # overwrites sqlalchemy.url from Settings (so a container does not migrate
    # against itself), and honours only this override. Setting the main option
    # here would be silently discarded and the check would run against whatever
    # DATABASE_URL_SYNC happens to be — Postgres, on a real machine.
    cfg = Config(
        str(Path(__file__).parent.parent / "alembic.ini"),
        cmd_opts=Namespace(x=[f"url={url}"]),
    )
    return cfg


@contextmanager
def _workspace(postgres_url: str | None) -> Iterator[Callable[[str], str]]:
    """Hand back a `db(name) -> url` factory over throwaway databases.

    SQLite gets files in a temp directory. Postgres gets real databases created
    on the target server and dropped on the way out — never the database in the
    URL itself, which is only ever connected to as the admin handle. A leftover
    from a killed run is dropped rather than reused, so a half-migrated scratch
    database cannot make the next run pass.
    """
    if postgres_url is None:
        with tempfile.TemporaryDirectory() as tmp:
            yield lambda name: f"sqlite:///{tmp}/{name}.db"
        return

    # AUTOCOMMIT: CREATE/DROP DATABASE cannot run inside a transaction block.
    admin = sa.create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    base = sa.engine.make_url(postgres_url)
    made: list[str] = []

    # WITH (FORCE), because the engines this script opens against a scratch
    # database keep pooled connections alive past the phase that used them and
    # a plain DROP fails with "is being accessed by other users". Postgres 13+,
    # which every deploy target here is.
    drop = 'DROP DATABASE IF EXISTS "{}" WITH (FORCE)'

    def db(name: str) -> str:
        dbname = f"migcheck_{os.getpid()}_{name}"
        with admin.connect() as conn:
            conn.exec_driver_sql(drop.format(dbname))
            conn.exec_driver_sql(f'CREATE DATABASE "{dbname}"')
        made.append(dbname)
        # render_as_string, not str(): SQLAlchemy's __str__ masks the password
        # as *** and the scratch database would be unreachable.
        return base.set(database=dbname).render_as_string(hide_password=False)

    try:
        yield db
    finally:
        for dbname in made:
            with admin.connect() as conn:
                conn.exec_driver_sql(drop.format(dbname))
        admin.dispose()


def _snapshot(url: str) -> Snapshot:
    """Column nullability, index names and unique constraints, per table."""
    inspector = sa.inspect(sa.create_engine(url))
    out: Snapshot = {}
    for table in sorted(inspector.get_table_names()):
        if table == "alembic_version":
            continue
        out[table] = {
            "cols": {c["name"]: bool(c["nullable"]) for c in inspector.get_columns(table)},
            "idx": {i["name"] for i in inspector.get_indexes(table)},
            "uq": {u["name"] for u in inspector.get_unique_constraints(table)},
        }
    return out


def _diff(chain: Snapshot, orm: Snapshot) -> list[str]:
    problems = []
    if missing := set(orm) - set(chain):
        problems.append(f"tables the chain never creates: {sorted(missing)}")
    if extra := set(chain) - set(orm):
        problems.append(f"tables with no model: {sorted(extra)}")

    for table in sorted(set(chain) & set(orm)):
        chain_cols, orm_cols = chain[table]["cols"], orm[table]["cols"]
        if gap := set(orm_cols) - set(chain_cols):
            problems.append(f"{table}: model columns the chain never adds: {sorted(gap)}")
        if gap := set(chain_cols) - set(orm_cols):
            problems.append(f"{table}: chain columns with no model: {sorted(gap)}")
        for col in sorted(set(chain_cols) & set(orm_cols)):
            if chain_cols[col] != orm_cols[col]:
                problems.append(
                    f"{table}.{col}: nullable chain={chain_cols[col]} model={orm_cols[col]}"
                )
        for kind in ("idx", "uq"):
            if chain[table][kind] != orm[table][kind]:
                problems.append(
                    f"{table}: {kind} mismatch {sorted(chain[table][kind] ^ orm[table][kind])}"
                )
    return problems


def main(postgres_url: str | None = None) -> int:
    failures: list[str] = []
    print(f"dialect: {'postgresql' if postgres_url else 'sqlite'}")
    with _workspace(postgres_url) as db:
        chain_url = db("chain")
        orm_url = db("orm")

        # 1. The deploy path.
        try:
            command.upgrade(_config(chain_url), "head")
            print("PASS  empty database -> head")
        except Exception as exc:
            print(f"FAIL  empty database -> head: {type(exc).__name__}: {exc}")
            return 1  # nothing downstream is meaningful if the chain will not run

        Base.metadata.create_all(sa.create_engine(orm_url))
        if problems := _diff(_snapshot(chain_url), _snapshot(orm_url)):
            failures.extend(problems)
            print(f"FAIL  migrated schema differs from the ORM ({len(problems)})")
        else:
            print("PASS  migrated schema matches the ORM exactly")

        # 3. Down and back up. Ordering bugs in downgrade only show up here.
        try:
            command.downgrade(_config(chain_url), "base")
            command.upgrade(_config(chain_url), "head")
            print("PASS  head -> base -> head")
        except Exception as exc:
            failures.append(f"downgrade/re-upgrade: {type(exc).__name__}: {exc}")
            print(f"FAIL  head -> base -> head: {type(exc).__name__}: {exc}")

        # 2. The dev-box path: every guarded step must no-op, not crash.
        noop_url = db("noop")
        Base.metadata.create_all(sa.create_engine(noop_url))
        try:
            command.upgrade(_config(noop_url), "head")
            print("PASS  create_all schema -> head is a no-op")
        except Exception as exc:
            failures.append(f"create_all -> head: {type(exc).__name__}: {exc}")
            print(f"FAIL  create_all schema -> head: {type(exc).__name__}: {exc}")

    print()
    if failures:
        for problem in failures:
            print(f"  - {problem}")
        return 1
    print("Migration chain is consistent with the models.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres",
        metavar="URL",
        default=os.environ.get("MIGRATION_CHECK_POSTGRES_URL"),
        help=(
            "run the three paths on this Postgres server instead of SQLite. "
            "Scratch databases are created beside the one named in the URL and "
            "dropped afterwards; the named database itself is never modified."
        ),
    )
    sys.exit(main(parser.parse_args().postgres))
