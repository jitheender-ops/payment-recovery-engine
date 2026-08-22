#!/usr/bin/env sh
# Container entrypoint: bring the schema up, then start the requested process.
#
# `alembic upgrade head` and not init_db()'s create_all(): create_all creates
# missing TABLES and silently ignores missing COLUMNS, so a container deployed
# over an older database starts happily and then fails on the first write to
# the money path. Migrations are the only upgrade path for a schema holding
# payment records.
set -eu

case "${1:-api}" in
  api)
    echo "==> alembic upgrade head"
    # `python -m alembic`, never the bare `alembic` console script. A console
    # script puts its own bin/ directory on sys.path[0]; `-m` puts the CWD
    # there. alembic/env.py does `from src.database import Base`, so the bare
    # form dies with ModuleNotFoundError: No module named 'src'. run.sh uses
    # -m for exactly this reason, and the dashboard below uses it for the
    # sibling reason (streamlit puts the SCRIPT's directory on sys.path).
    python -m alembic upgrade head
    echo "==> uvicorn"
    exec uvicorn src.main:app \
      --host 0.0.0.0 \
      --port "${APP_PORT:-8000}" \
      --workers "${WEB_CONCURRENCY:-1}"
    ;;
  dashboard)
    # No migration here: two services racing `alembic upgrade head` on boot is
    # a lock fight for no reason. The api service owns the schema.
    exec python -m streamlit run dashboard/app.py \
      --server.port "${STREAMLIT_PORT:-8501}" \
      --server.address 0.0.0.0
    ;;
  migrate)
    exec python -m alembic upgrade head
    ;;
  *)
    exec "$@"
    ;;
esac
