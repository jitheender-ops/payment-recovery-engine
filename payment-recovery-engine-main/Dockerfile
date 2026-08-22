# syntax=docker/dockerfile:1
#
# Two stages. The builder needs a C toolchain to install anything without an
# aarch64 wheel; the runtime does not, and carrying ~400MB of compiler into a
# container that serves webhooks is pure attack surface and pull time.
#
# 3.13, and it must MATCH the interpreter requirements.lock.txt was frozen from.
# The lock is a pip freeze of the local venv and some pins carry a minimum
# Python of their own — numpy 2.5 requires >=3.12 — so a 3.11 base dies at
# `pip install -r requirements.lock.txt` with "No matching distribution found
# for numpy==2.5.2". pyproject still declares requires-python >=3.11 and that
# claim is still tested: the CI matrix runs 3.11 by resolving from pyproject.

# ── Builder ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Its own venv so the runtime stage can take the whole tree in one COPY.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependencies in their own layer, before the source, so editing a .py file
# does not reinstall xgboost and scikit-learn on every build.
COPY pyproject.toml requirements.lock.txt* ./
RUN if [ -f requirements.lock.txt ]; then pip install -r requirements.lock.txt; fi

COPY src/ src/
COPY eval/ eval/
COPY dashboard/ dashboard/
COPY scripts/ scripts/
COPY alembic/ alembic/
COPY alembic.ini README.md ./

RUN pip install --no-deps -e .

# The XGBoost baseline, trained INTO the image. The artefact is gitignored, so
# the alternative is what shipped before: a container that silently falls back
# to the rule heuristic while every log line and README table calls it XGBoost.
RUN mkdir -p models && python scripts/train_xgboost.py --n-samples 10000 \
    && test -f models/xgboost_baseline.joblib

# ── Runtime ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

# PYTHONUNBUFFERED especially: without it the app's logs sit in a pipe buffer
# and a container that dies takes its last words with it — which is exactly
# when you want them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# libpq5 is the runtime half of libpq-dev — psycopg2 links against it. No
# compiler, no headers.
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 recovery

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=recovery:recovery /app /app
COPY --chown=recovery:recovery docker-entrypoint.sh /app/docker-entrypoint.sh

# Note on what is inside: requirements.lock.txt is a freeze of the DEVELOPMENT
# venv, so pytest, ruff and mypy come along with it. That is a real cost and it
# is not pretended away here — removing them means maintaining a second,
# runtime-only lockfile, and a lockfile that is not the one developers actually
# run is a lockfile that drifts. The trade is deliberate.

# Non-root: the webhook endpoint is the one process here reachable from the
# internet, and it needs write access to nothing outside /tmp.
USER recovery

EXPOSE 8000 8501

# A bare `docker run` previously did nothing at all — the image declared ports
# and no command. ENTRYPOINT applies migrations, then starts the process.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["api"]
