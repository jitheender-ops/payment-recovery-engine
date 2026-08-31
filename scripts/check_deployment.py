"""
Is a DEPLOYED instance actually working? Read-only, writes nothing.

The other two scripts (simulate_webhooks.py, run_risk_batch.py) prove the
engine by driving traffic through it. That is the right second step and the
wrong first one: if the console is ungated or /risks is accepting unsigned
events, you want to know that BEFORE you push data in, not after.

So this checks the things that are true of a healthy deployment and cost
nothing to ask:

    reachable      /health answers 200
    schema         it answers at all, which means alembic ran on boot
    public page    /console renders product facts without a session
    gate           /console/live REDIRECTS when you are not signed in
    signed in      ... and renders the ledger when you are
    heartbeat      the scheduler has ticked recently (the whole point of
                   Layer 6 — every figure on the console is a frozen
                   snapshot if it has not)
    fail-closed    /risks REJECTS an unsigned event
    schema closed  /docs is 404 outside development

Each is a claim the README or PRODUCT.md makes. A deployment where any of
them is false is broken in a way that is quiet — the page still renders.

Usage:
    python scripts/check_deployment.py --host https://recovery-api-xxxx.onrender.com
    python scripts/check_deployment.py --host https://... --password "$DASHBOARD_PASSWORD"

Exit code is 0 only if every REQUIRED check passed, so it works in CI or a
deploy hook. Checks needing the password are skipped, not failed, when it is
not supplied.
"""

from __future__ import annotations

import argparse
import sys

import httpx

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

_MARK = {PASS: "\033[32m✓\033[0m", FAIL: "\033[31m✗\033[0m",
         WARN: "\033[33m!\033[0m", SKIP: "\033[90m–\033[0m"}


class Report:
    """Collects results so one failure does not hide the checks after it."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        print(f"  {_MARK[status]} {name}" + (f"  — {detail}" if detail else ""))

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == WARN)


def check(host: str, password: str | None, timeout: float) -> Report:
    r = Report()
    # follow_redirects=False on purpose: the console gate is a 303, and
    # following it would turn the security check into "the login page loads".
    with httpx.Client(base_url=host, timeout=timeout, follow_redirects=False) as c:

        # ── Reachable ────────────────────────────────────────────────────
        try:
            resp = c.get("/health")
        except httpx.HTTPError as e:
            r.add(FAIL, "reachable", f"{type(e).__name__}: {e}")
            print("\n  Nothing else can be checked until /health answers.")
            print("  On Render's free plan a cold start takes tens of seconds —")
            print("  if this is the first request in a while, try once more.")
            return r
        if resp.status_code == 200:
            r.add(PASS, "reachable", "/health 200")
        else:
            r.add(FAIL, "reachable", f"/health returned {resp.status_code}")
            return r

        # ── Public landing ───────────────────────────────────────────────
        resp = c.get("/console")
        if resp.status_code != 200:
            r.add(FAIL, "public landing", f"/console returned {resp.status_code}")
        elif "Then it listens" not in resp.text:
            r.add(WARN, "public landing", "renders, but not the current build")
        else:
            r.add(PASS, "public landing", "/console renders product facts")

        # ── The gate must actually gate ──────────────────────────────────
        resp = c.get("/console/live")
        if resp.status_code == 303 and "login" in resp.headers.get("location", ""):
            r.add(PASS, "console gate", "redirects to /console/login when signed out")
        elif resp.status_code == 200 and "Still owed" in resp.text:
            r.add(FAIL, "console gate",
                  "LIVE FIGURES SERVED WITHOUT A SESSION — check DASHBOARD_PASSWORD")
        elif resp.status_code == 200:
            # Fail-closed on an unset password: renders, but shows nothing live.
            r.add(WARN, "console gate",
                  "no session needed, but no live figures either — password unset?")
        else:
            r.add(WARN, "console gate", f"unexpected {resp.status_code}")

        # ── /risks must refuse an unsigned event ─────────────────────────
        resp = c.post("/risks", json={"risk_type": "checkout_abandonment",
                                      "reference_id": "deploy-check",
                                      "amount_paise": 1, "currency": "INR"})
        if resp.status_code in (400, 401, 403):
            r.add(PASS, "risk intake fails closed", f"unsigned event rejected ({resp.status_code})")
        elif resp.status_code in (200, 202):
            r.add(FAIL, "risk intake fails closed",
                  "UNSIGNED EVENT ACCEPTED — RISK_WEBHOOK_SECRET is not set")
        else:
            r.add(WARN, "risk intake fails closed", f"unexpected {resp.status_code}")

        # ── The schema surface should be shut outside development ────────
        resp = c.get("/docs")
        if resp.status_code == 404:
            r.add(PASS, "api docs closed", "/docs 404 (APP_ENV is not development)")
        else:
            r.add(WARN, "api docs closed",
                  f"/docs returned {resp.status_code} — APP_ENV=development on a public URL?")

        # ── Signed-in checks ─────────────────────────────────────────────
        if not password:
            r.add(SKIP, "signed-in console", "pass --password to check the ledger and heartbeat")
            return r

        resp = c.post("/console/login", data={"password": password})
        if resp.status_code != 303:
            r.add(FAIL, "sign in", f"login returned {resp.status_code}, expected 303")
            return r
        r.add(PASS, "sign in", "password accepted")

        resp = c.get("/console/live", cookies=resp.cookies)
        if resp.status_code != 200:
            r.add(FAIL, "signed-in console", f"returned {resp.status_code}")
            return r
        html = resp.text

        if "Can't reach the database" in html:
            r.add(FAIL, "database", "the console cannot read the database")
        elif "The ledger is empty" in html:
            r.add(PASS, "database", "readable; no cases yet (expected on a fresh deploy)")
        elif "Still owed" in html:
            r.add(PASS, "database", "readable, with cases in it")
        else:
            r.add(WARN, "database", "rendered, but in an unrecognised state")

        # The heartbeat is the one that matters most on a sleeping free plan.
        if "Engine running" in html:
            r.add(PASS, "scheduler", "heartbeat fresh — sweeps are firing")
        elif "Engine stopped" in html:
            r.add(WARN, "scheduler",
                  "no recent sweep. Expected if the service just woke; "
                  "if it persists check SCHEDULER_ENABLED")
        elif "The ledger is empty" in html:
            r.add(SKIP, "scheduler", "no heartbeat strip until the ledger has data")
        else:
            r.add(WARN, "scheduler", "no heartbeat strip found")

    return r


def main() -> None:
    p = argparse.ArgumentParser(
        description="Check that a deployed recovery engine is functioning. Writes nothing.")
    p.add_argument("--host", required=True,
                   help="e.g. https://recovery-api-xxxx.onrender.com")
    p.add_argument("--password", default=None,
                   help="DASHBOARD_PASSWORD; without it the ledger checks are skipped")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="seconds; generous by default because a free-plan cold start is slow")
    args = p.parse_args()

    host = args.host.rstrip("/")
    print(f"\nChecking {host}\n")
    r = check(host, args.password, args.timeout)

    print()
    if r.failed:
        print(f"  {r.failed} check(s) FAILED — this deployment is not serving correctly.")
        sys.exit(1)
    if r.warned:
        print(f"  All required checks passed, {r.warned} worth a look.")
    else:
        print("  All checks passed.")
    print("\n  Next: drive real traffic through it —")
    print(f"    python scripts/simulate_webhooks.py --host {host} --count 24")
    print(f"    python scripts/run_risk_batch.py    --host {host} --count 24\n")


if __name__ == "__main__":
    main()
