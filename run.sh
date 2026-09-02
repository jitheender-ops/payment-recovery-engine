#!/usr/bin/env bash
#
# One command: clean build, verify, run, and expose a public webhook URL.
#
#   ./run.sh                 build + verify + API + dashboard + public tunnel
#   ./run.sh --demo          everything, offline: SQLite, fake gateway, seeded,
#                            no credentials and no Docker needed
#   ./run.sh --demo --scale  the same, with a few thousand cases instead of
#                            a few dozen — for the batch-recovery story
#   ./run.sh --verify-only   build + ruff/pytest/mypy, start nothing (CI)
#   ./run.sh --no-tunnel     build + verify + run locally, no public URL
#
# Re-runnable: an already-healthy API is reused rather than duplicated, and the
# venv is only rebuilt when it is actually unusable.
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VERIFY_ONLY=false
TUNNEL=true
DEMO=false
SCALE=false
for arg in "$@"; do
  case "$arg" in
    --verify-only) VERIFY_ONLY=true ;;
    --no-tunnel)   TUNNEL=false ;;
    # --demo implies --no-tunnel. The whole point is that nothing leaves the
    # machine, and a public URL fronting a fake gateway is the worst of both.
    --demo)        DEMO=true; TUNNEL=false ;;
    # Thousands of cases rather than dozens. Off by default because the
    # small book seeds in seconds and is enough to see every feature; the
    # big one exists so "recovered X across N cases" is a real measurement.
    --scale)       SCALE=true ;;
    -h|--help)     sed -n '3,11p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg (try --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── 1. Interpreter ───────────────────────────────────────────────────────
# Any 3.11+ works. PY= overrides, because `python3` is often not the one with
# working wheels for numpy/xgboost.
say "Python"
PY="${PY:-}"
if [[ -z "$PY" ]]; then
  for c in python3.13 python3.12 python3.11 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null || continue
    PY="$c"; break
  done
fi
[[ -n "$PY" ]] || die "no Python >= 3.11 found. Install one, or run: PY=/path/to/python3.12 ./run.sh"
ok "$($PY -V) at $(command -v "$PY")"

# ── 2. Config ────────────────────────────────────────────────────────────
# Values are never printed — a leaked webhook secret lets anyone forge a
# payment failure, and a leaked key secret moves money.
say "Configuration"
if $DEMO; then
  # Demo mode answers for itself. It needs no .env, no Razorpay account and
  # no Postgres: the gateway is a local fake (src/demo.py) and the database
  # is a file. The secrets below are real HMAC keys — the engine genuinely
  # signs and verifies with them — but they authenticate this process to
  # itself and are worth nothing anywhere else, which is why they can be
  # written down here.
  #
  # .env is deliberately NOT sourced. `source` overwrites variables that are
  # already exported, so a committed APP_PORT or DASHBOARD_PASSWORD in .env
  # would silently beat one the caller passed on the command line — and a
  # stale .env is exactly what a demo must not depend on. The app still reads
  # .env through pydantic-settings, where real environment variables take
  # precedence, so the values below win there too.
  set -a
  APP_ENV=development
  DEMO_MODE=true
  DATABASE_URL="sqlite+aiosqlite:///$PWD/.demo.sqlite3"
  DATABASE_URL_SYNC="sqlite:///$PWD/.demo.sqlite3"
  RAZORPAY_KEY_ID="${RAZORPAY_KEY_ID:-rzp_test_demo0000000}"
  RAZORPAY_KEY_SECRET="${RAZORPAY_KEY_SECRET:-demo_key_secret_local_only}"
  RAZORPAY_WEBHOOK_SECRET="${RAZORPAY_WEBHOOK_SECRET:-demo_webhook_secret_local_only}"
  RISK_WEBHOOK_SECRET="${RISK_WEBHOOK_SECRET:-demo_risk_secret_local_only}"
  RECOVERY_LINK_SECRET="${RECOVERY_LINK_SECRET:-demo_recovery_link_secret_local_only}"
  AUDIT_CHAIN_SECRET="${AUDIT_CHAIN_SECRET:-demo_audit_secret_local_only}"
  MERCHANT_NAME="${MERCHANT_NAME:-Kirana Supply Co}"
  DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-demo-console-password}"
  APP_PORT="${APP_PORT:-8000}"
  PUBLIC_BASE_URL="http://127.0.0.1:$APP_PORT"
  set +a
  ok "demo mode — SQLite at .demo.sqlite3, gateway faked, nothing leaves this machine"
  warn "every 'recovered' rupee this run reports is fictional"
else
if [[ ! -f .env ]]; then
  cp .env.example .env
  die ".env created from .env.example — fill in your keys, then re-run."
fi
set -a; source ./.env; set +a

for k in RAZORPAY_KEY_ID RAZORPAY_KEY_SECRET RAZORPAY_WEBHOOK_SECRET; do
  v="${!k:-}"
  [[ -n "$v" ]] || die "$k is empty in .env. The app refuses to start without it (fail closed)."
  if [[ "$v" == REPLACE_WITH_* || "$v" == your_* ]]; then
    warn "$k is still a placeholder — webhooks will be ACCEPTED but any Razorpay"
    warn "  API call (Payment Link creation) will fail 401. Paste the real value"
    warn "  from https://dashboard.razorpay.com → Settings → API Keys."
  else
    ok "$k set (${#v} chars)"
  fi
done
APP_PORT="${APP_PORT:-8000}"
STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"
[[ "${SQL_ECHO:-false}" == "false" ]] || warn "SQL_ECHO is on — it logs customer email/phone/VPA. Turn it off outside debugging."

# The dashboard refuses to render without a password, so an unset one would
# break "one command" for anyone whose .env predates the variable. Generated
# rather than defaulted — a default in a shipped script is a publicly known
# password. Printed exactly once, at generation: after that it is in .env and
# gets the same char-count treatment as every other secret above.
if [[ -z "${DASHBOARD_PASSWORD:-}" ]]; then
  DASHBOARD_PASSWORD="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(18))')"
  export DASHBOARD_PASSWORD
  if grep -q '^DASHBOARD_PASSWORD=' .env; then
    # awk, not `sed -i`: the -i flag takes an argument on BSD and not on GNU.
    # umask 077 because mv would otherwise hand .env the default permissions of
    # the temp file, quietly widening a file full of secrets.
    ( umask 077
      awk -v pw="$DASHBOARD_PASSWORD" \
        '/^DASHBOARD_PASSWORD=/{print "DASHBOARD_PASSWORD=" pw; next} {print}' .env > .env.tmp
    ) && mv .env.tmp .env
  else
    printf '\nDASHBOARD_PASSWORD=%s\n' "$DASHBOARD_PASSWORD" >> .env
  fi
  warn "no DASHBOARD_PASSWORD — generated one and saved it to .env. It is shown"
  warn "  once, here, and nowhere else:"
  printf '\n      \033[1m%s\033[0m\n\n' "$DASHBOARD_PASSWORD"
else
  ok "DASHBOARD_PASSWORD set (${#DASHBOARD_PASSWORD} chars)"
fi
fi   # end of the non-demo configuration branch

STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"

# ── 3. Virtualenv ────────────────────────────────────────────────────────
# Deliberately NOT --system-site-packages, and the probe below enforces that:
# it checks not just that the imports resolve, but that they resolve to files
# INSIDE .venv. A venv created with --system-site-packages passes a plain
# `import fastapi` while holding nothing but pip — every package silently comes
# from the system Python. That venv lies about isolation: `pip freeze` produces
# a lockfile of packages it does not own, and a machine without those globals
# gets ImportError at startup. Checking import success alone does not catch it.
say "Virtualenv"
VPY=".venv/bin/python"
probe() {
  "$VPY" - <<'PY' 2>/dev/null
import pathlib, sys
mods = []
try:
    import fastapi, pydantic_settings, pytest, sqlalchemy, streamlit, xgboost
    mods = [fastapi, sqlalchemy, pydantic_settings, xgboost, streamlit, pytest]
except ImportError:
    sys.exit(1)
venv = pathlib.Path(sys.prefix).resolve()
leaked = [m.__name__ for m in mods if venv not in pathlib.Path(m.__file__).resolve().parents]
if leaked:
    print("leaked: " + ", ".join(leaked))
    sys.exit(2)
PY
}
if [[ -x "$VPY" ]] && probe; then
  ok "existing .venv is complete and isolated — reusing"
else
  if [[ -d .venv ]]; then
    if [[ -x "$VPY" ]] && grep -qi '^include-system-site-packages *= *true' .venv/pyvenv.cfg 2>/dev/null; then
      warn "existing .venv was built with --system-site-packages and owns almost nothing —"
      warn "  its imports come from the system Python. Rebuilding it isolated."
    else
      warn "existing .venv is incomplete — rebuilding from scratch"
    fi
    # Moved aside, not --clear'd. A rebuild needs PyPI; if the network is down
    # the old venv is restored instead of leaving the user with neither.
    rm -rf .venv.bak && mv .venv .venv.bak
  fi
  restore() {
    if [[ -d .venv.bak ]]; then
      rm -rf .venv && mv .venv.bak .venv
      warn "rebuild failed — the previous .venv has been put back"
    fi
  }
  "$PY" -m venv .venv || { restore; die "could not create a virtualenv with $PY"; }
  "$VPY" -m pip install --quiet --upgrade pip setuptools wheel || { restore; die "pip bootstrap failed — is PyPI reachable?"; }
  if [[ -f requirements.lock.txt ]]; then
    ok "installing from requirements.lock.txt (pinned)"
    "$VPY" -m pip install --quiet -r requirements.lock.txt || { restore; die "pinned install failed"; }
    "$VPY" -m pip install --quiet -e . --no-deps || { restore; die "editable install failed"; }
  else
    ok "resolving from pyproject.toml (no lockfile yet)"
    "$VPY" -m pip install --quiet -e ".[dev]" || { restore; die "install failed — is PyPI reachable?"; }
    # Written from what actually installed. Hand-authored pins would be guesses;
    # these are the exact versions this build was verified against.
    # The interpreter goes in the file. A bare freeze is silently tied to
    # whatever Python produced it — pins like numpy 2.5 require >=3.12 — and
    # the Dockerfile and CI both install from here. Recording it is what makes
    # the mismatch findable instead of a build failure three layers down.
    {
      printf '# Frozen from Python %s. pip ignores these comment lines.\n' \
        "$("$VPY" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      printf '#\n# Anything installing this file must use a matching interpreter.\n'
      "$VPY" -m pip freeze --exclude-editable
    } > requirements.lock.txt
    ok "wrote requirements.lock.txt — commit it to pin future builds"
  fi
  probe || { restore; die "install finished but imports still do not resolve inside .venv"; }
  rm -rf .venv.bak
  ok "dependencies installed"
fi

# ── 4. Database ──────────────────────────────────────────────────────────
# Role and database are created if absent, so "one command" is not a lie on a
# fresh Postgres. Tables are created by init_db() at app startup; alembic is
# wired up but has no revisions yet, so there is nothing to upgrade.
#
# Skipped for --verify-only: ruff, pytest and mypy touch no database, and CI
# should not need Postgres to type-check. Skipped for --demo too: that path
# runs on a SQLite file precisely so a laptop needs no Postgres and no Docker,
# and this block would die on a machine that has neither.
if ! $VERIFY_ONLY && ! $DEMO; then
say "Database"
DB_NAME="${DB_NAME:-payment_recovery}"
DB_USER="${DB_USER:-recovery}"
# -w everywhere below: without it psql prompts for a password and a "one
# command" script hangs forever on an invisible prompt. Failing into the
# manual-instructions branch is the better outcome.
export PGCONNECT_TIMEOUT=5
if command -v docker >/dev/null 2>&1 && ! pg_isready -q -h 127.0.0.1 -p 5432 2>/dev/null; then
  [[ -n "${POSTGRES_PASSWORD:-}" ]] || die "docker path needs POSTGRES_PASSWORD in .env (compose has no default, on purpose)."
  docker compose up -d postgres
  for _ in $(seq 30); do pg_isready -q -h 127.0.0.1 -p 5432 2>/dev/null && break; sleep 1; done
fi
if pg_isready -q -h 127.0.0.1 -p 5432 2>/dev/null; then
  ok "Postgres reachable on 127.0.0.1:5432"
  if psql -w -h 127.0.0.1 -lqt 2>/dev/null | cut -d\| -f1 | grep -qw "$DB_NAME"; then
    ok "database '$DB_NAME' exists"
  elif psql -w -h 127.0.0.1 -d postgres -v ON_ERROR_STOP=1 -q >/dev/null 2>&1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$DB_USER') THEN
    CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_USER';
  END IF;
END \$\$;
SQL
  then
    createdb -w -h 127.0.0.1 -O "$DB_USER" "$DB_NAME" 2>/dev/null && ok "created database '$DB_NAME' owned by '$DB_USER'"
  else
    warn "could not auto-create '$DB_NAME' (no superuser access). Create it manually:"
    warn "  createdb -O $DB_USER $DB_NAME"
  fi
else
  die "Postgres is not running. Start it with one of:
    brew services start postgresql@15
    docker compose up -d postgres"
fi
fi

# ── 5. Verify ────────────────────────────────────────────────────────────
# The build is not "clean" because it installed — it is clean because these
# three pass. All are run before anything is exposed to the internet.
say "Verifying"
.venv/bin/ruff check . || die "ruff failed"
ok "ruff"
# `env -u DEMO_MODE`: the suite verifies the PRODUCT, not the demo harness, so
# it must not inherit the demo's environment. It would not merely skew a
# result — Settings refuses to construct when DEMO_MODE meets a non-development
# APP_ENV (and .env commonly sets one), so src.main would fail at import and
# every test touching it would die for a reason unrelated to the code.
env -u DEMO_MODE "$VPY" -m pytest -q || die "tests failed"
ok "pytest"
# `src scripts eval`, matching .github/workflows/ci.yml — not `mypy .`, which
# this line used to say. Under strict=true that also swept tests/ and
# alembic/, where 47 pre-existing errors live, so the local gate failed on a
# clean checkout while CI passed. A gate that is red for everyone is a gate
# nobody reads.
env -u DEMO_MODE "$VPY" -m mypy src scripts eval || die "mypy failed"
ok "mypy --strict"
# `|| die`, not `&& ok`. Under `set -e` a failure inside an && list does not
# exit, so the old form skipped the tick and carried on — and the script it
# calls returned None regardless of what it found, so nothing could fail
# anyway. Both halves of that are fixed: an unmapped Razorpay decline reason
# now stops the build, because it means cases are being abandoned unattempted.
"$VPY" scripts/seed_error_codes.py || die "error-code taxonomy check failed (see above)"
ok "error-code taxonomy validated"

# The XGBoost baseline needs a trained model or it silently runs the rule
# heuristic instead — which is the state the repo shipped in, with the README
# comparing an LLM against a pile of if-statements while calling it XGBoost.
# The artefact is gitignored (1MB binary), so a fresh clone trains its own.
MODEL_PATH="${XGBOOST_MODEL_PATH:-models/xgboost_baseline.joblib}"
if [[ -f "$MODEL_PATH" ]]; then
  ok "XGBoost model present ($MODEL_PATH)"
else
  say "Training XGBoost baseline (no model at $MODEL_PATH)"
  mkdir -p "$(dirname "$MODEL_PATH")"
  "$VPY" scripts/train_xgboost.py --n-samples 10000 --output "$MODEL_PATH" >/dev/null 2>&1 \
    || die "XGBoost training failed"
  ok "XGBoost model trained"
fi

if $VERIFY_ONLY; then
  say "Verify-only: clean build confirmed. Nothing started."
  exit 0
fi

# ── 6. Schema ────────────────────────────────────────────────────────────
# After the verify-only exit on purpose: this is the one step that needs
# Postgres, and --verify-only is the CI path that deliberately runs without it.
#
# Alembic, not init_db()'s create_all — which is development-only in
# src/main.py now, and which never added a missing COLUMN anyway, so it was
# never an upgrade path for a database that already existed.
if $DEMO; then
  # Not alembic: the migrations carry Postgres JSONB columns SQLite cannot
  # compile. The demo takes the app's own development path instead —
  # src/main.py runs create_all when APP_ENV=development, and the JSONB
  # shim in src/database.py makes the schema portable. Deleting
  # .demo.sqlite3 is the whole reset story.
  say "Database"
  ok "SQLite at .demo.sqlite3 — tables created on startup, no migrations to run"
else
  say "Applying migrations"
  "$VPY" -m alembic upgrade head || die "alembic upgrade failed"
  ok "schema at head"
fi

# ── 7. Run ───────────────────────────────────────────────────────────────
PIDS=()
cleanup() {
  printf '\n'
  say "Shutting down"
  for p in "${PIDS[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
healthy()   { curl -fsS --max-time 2 "http://127.0.0.1:$APP_PORT/health" >/dev/null 2>&1; }

say "API"
# Bound to loopback, not 0.0.0.0. The tunnel connects from this machine, so
# 0.0.0.0 would additionally expose the payments API to every device on the
# LAN — the same reason docker-compose does not publish Postgres' port.
if healthy; then
  ok "already running and healthy on :$APP_PORT — reusing"
elif port_busy "$APP_PORT"; then
  die "port $APP_PORT is taken by something that is not this API. Free it, or set APP_PORT in .env."
else
  # No --reload: an edit mid-demo would restart the server behind a live
  # public URL, and the reloader's child process survives killing the parent.
  "$VPY" -m uvicorn src.main:app --host 127.0.0.1 --port "$APP_PORT" &
  PIDS+=($!)
  for _ in $(seq 40); do healthy && break; sleep 0.5; done
  healthy || die "API did not become healthy. Scroll up for the uvicorn traceback."
  ok "started on :$APP_PORT"
fi

# ── 7b. Seed the demo ────────────────────────────────────────────────────
# Only once: a re-run of --demo reuses the database it already built, so the
# links printed last time keep working and the recovered figures do not reset
# under you mid-demo. Delete .demo.sqlite3 to start over.
if $DEMO && [[ ! -f .demo.seeded ]]; then
  say "Seeding demo data"
  # Payment rail first (it opens cases the chasers do not), then the four
  # chaser risk types, then the preview rows for page states and the B2B
  # account the statement page needs. Output is summarised, not silenced —
  # a seed that half-failed should be visible.
  "$VPY" scripts/simulate_webhooks.py --host "http://127.0.0.1:$APP_PORT" \
    --count 24 2>&1 | tail -20 || warn "payment-rail seed had problems (see above)"
  "$VPY" scripts/run_risk_batch.py --host "http://127.0.0.1:$APP_PORT" \
    --count 16 2>&1 | tail -20 || warn "risk-batch seed had problems (see above)"
  "$VPY" scripts/recovery_links.py --seed-states >/dev/null 2>&1 \
    || warn "preview-state seed had problems"
  if $SCALE; then
    # Routed through the real classifier, agent and guardrail — only the
    # persistence is bulk. See scripts/seed_bulk.py.
    "$VPY" scripts/seed_bulk.py --count 2500 2>/dev/null \
      || warn "bulk seed had problems"
  fi
  touch .demo.seeded
  ok "seeded"
fi

say "Dashboard"
if port_busy "$STREAMLIT_PORT"; then
  ok "already running on :$STREAMLIT_PORT — reusing"
else
  "$VPY" -m streamlit run dashboard/app.py \
    --server.port "$STREAMLIT_PORT" --server.address 127.0.0.1 \
    --server.headless true --browser.gatherUsageStats false >/dev/null 2>&1 &
  PIDS+=($!)
  ok "starting on :$STREAMLIT_PORT"
fi

# ── 8. Public URL ────────────────────────────────────────────────────────
# ngrok if it is already authed (its URL is readable from the local API);
# otherwise cloudflared, which needs no account.
PUBLIC_URL=""
if $TUNNEL; then
  say "Public tunnel"
  if command -v ngrok >/dev/null 2>&1 && [[ -f "$HOME/Library/Application Support/ngrok/ngrok.yml" || -f "$HOME/.config/ngrok/ngrok.yml" ]]; then
    if curl -fsS --max-time 2 http://127.0.0.1:4040/api/tunnels >/dev/null 2>&1; then
      ok "reusing the ngrok agent already running"
    else
      ngrok http "$APP_PORT" --log stdout >.ngrok.log 2>&1 &
      PIDS+=($!)
    fi
    for _ in $(seq 30); do
      PUBLIC_URL=$(curl -fsS --max-time 2 http://127.0.0.1:4040/api/tunnels 2>/dev/null \
        | "$VPY" -c 'import json,sys; t=json.load(sys.stdin)["tunnels"]; print(next((x["public_url"] for x in t if x["public_url"].startswith("https")), ""))' 2>/dev/null) || true
      [[ -n "$PUBLIC_URL" ]] && break
      sleep 1
    done
  elif command -v cloudflared >/dev/null 2>&1; then
    cloudflared tunnel --url "http://127.0.0.1:$APP_PORT" --no-autoupdate >.cloudflared.log 2>&1 &
    PIDS+=($!)
    for _ in $(seq 30); do
      PUBLIC_URL=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' .cloudflared.log 2>/dev/null | head -1) || true
      [[ -n "$PUBLIC_URL" ]] && break
      sleep 1
    done
  else
    warn "no tunnel binary. brew install cloudflared   (or ngrok)"
  fi
  [[ -n "$PUBLIC_URL" ]] && ok "$PUBLIC_URL" || warn "tunnel did not report a URL — see .ngrok.log / .cloudflared.log"
fi

# ── 9. Where things are ──────────────────────────────────────────────────
# The docs trio only exists when APP_ENV=development (src/main.py decides it at
# FastAPI() construction), so printing the URL unconditionally would advertise a
# 404 — and under a tunnel it advertises a public route enumeration.
if [[ "${APP_ENV:-development}" == "development" ]]; then
  DOCS_LINE="    API docs     http://127.0.0.1:$APP_PORT/docs"
  [[ -z "$PUBLIC_URL" ]] || {
    warn "APP_ENV=development, so /docs, /redoc and /openapi.json are open to"
    warn "  anyone with the tunnel URL — they enumerate every route and schema."
    warn "  Set APP_ENV=staging and API_KEY in .env to close them."
  }
else
  DOCS_LINE="    API docs     disabled (APP_ENV=$APP_ENV); /openapi.json needs X-API-Key"
fi
if $DEMO; then DASH_HINT="password below"; else DASH_HINT="password from .env"; fi
cat <<BANNER

  $(printf '\033[1;32m●\033[0m') Payment Failure Recovery Engine is up

    API          http://127.0.0.1:$APP_PORT
$DOCS_LINE
    Dashboard    http://127.0.0.1:$STREAMLIT_PORT  ($DASH_HINT)
BANNER
if [[ -n "$PUBLIC_URL" ]]; then
  cat <<BANNER
    Public URL   $PUBLIC_URL

  Paste this into the Razorpay dashboard → Settings → Webhooks:

    $PUBLIC_URL/webhooks/razorpay

  Subscribe to: payment.failed, payment.captured
  Use the same secret as RAZORPAY_WEBHOOK_SECRET — signatures are verified
  and a mismatch is rejected with 401 before the payload is read.
BANNER
fi
if $DEMO; then
  cat <<BANNER

  ────────────────────────────────────────────────────────────────
   OPEN THIS ONE PAGE — everything is on it:

       http://127.0.0.1:$APP_PORT/demo

   Every capability, the B2B statements, the merchant console
   (password $DASHBOARD_PASSWORD) and the analytics dashboard,
   all linked from there.
  ────────────────────────────────────────────────────────────────

  The same links as text, if you would rather copy them:
BANNER
  # The link printer derives each page's state by calling routes._view_state
  # itself, so these labels cannot drift from what the page actually renders.
  "$VPY" scripts/recovery_links.py || warn "could not print links"
  cat <<'BANNER'

  Re-seed from scratch:  rm .demo.sqlite3 .demo.seeded && ./run.sh --demo
  Ctrl-C stops everything.
BANNER
else
cat <<'BANNER'

  Send yourself a test failure:
    .venv/bin/python scripts/simulate_webhooks.py --count 20

  Ctrl-C stops everything.
BANNER
fi

# Nothing was started by this run — everything was already up. Blocking on
# `wait` with no children returns instantly, which would look like a crash.
if [[ ${#PIDS[@]} -eq 0 ]]; then
  echo "  (all services were already running — this script started nothing to stop)"
  trap - EXIT
  exit 0
fi
wait
