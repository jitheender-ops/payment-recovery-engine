"""
Validate the configured LLM before spending quota on a full eval run.

Three questions, in the order they can fail:

  1. Does the key work at all?          -> lists the models the endpoint offers
  2. Does the configured model exist?   -> a wrong id is a 404, not a bad score
  3. Does it emit parseable JSON?       -> the only quality that matters here

(3) is the one worth running. PolicyAgent.decide() swallows malformed output,
retries once with a correction, then returns a heuristic action — so a model
that cannot hold the JSON contract does not error, it quietly produces a full
run of fallbacks that the eval harness then refuses to publish. Better to learn
that from 5 calls than from 2,700.

Usage:
    python scripts/check_llm.py
    python scripts/check_llm.py --samples 10
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent.actions import FailureContext  # noqa: E402
from src.config import get_settings, reveal  # noqa: E402

# Spread across retryable and non-retryable classes: a model that answers well
# on easy cases and mangles hard declines is the dangerous kind.
CLASSES = [
    "insufficient_funds",
    "3ds_dropoff",
    "bank_downtime",
    "fraud_block",
    "upi_collect_timeout",
]


def _context(failure_class: str, i: int) -> FailureContext:
    now = datetime.now(UTC)
    return FailureContext(
        payment_id=f"pay_check_{i:03d}",
        failure_class=failure_class,
        error_code="BAD_REQUEST_ERROR",
        amount=50000 + i * 1000,
        method="card",
        bank="HDFC",
        customer_id=f"check{i}@example.com",
        failed_at=now,
        current_time=now,
        hour_of_day=14,
        day_of_week=2,
        is_retryable=failure_class not in ("fraud_block",),
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()

    settings = get_settings()
    key = reveal(settings.openai_api_key)
    print(f"provider  {settings.llm_provider}")
    print(f"base_url  {settings.llm_base_url or '(provider default)'}")
    print(f"model     {settings.llm_model}")
    print(f"key       {'set (' + str(len(key)) + ' chars)' if key else 'MISSING'}")
    print()

    if not key:
        print("No OPENAI_API_KEY in .env. Nothing to check.")
        return 1

    # ── 1. Does the key work, and what models does it offer? ─────────────
    try:
        import openai

        client = openai.AsyncOpenAI(
            api_key=key, base_url=settings.llm_base_url or None, timeout=30.0
        )
        listing = await client.models.list()
        available = sorted(m.id for m in listing.data)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        print(f"Could not list models{f' (HTTP {status})' if status else ''}: {exc}")
        print("\nA 401/403 means the key is wrong or not enabled for this endpoint.")
        return 1

    print(f"{len(available)} models available. Ones that look suitable:")
    for mid in available:
        if any(w in mid.lower() for w in ("flash", "mini", "lite")):
            print(f"  {mid}")
    print()

    configured = settings.llm_model
    # Endpoints vary on whether ids carry a "models/" prefix; compare on the tail.
    tails = {m.rsplit("/", 1)[-1] for m in available}
    if configured.rsplit("/", 1)[-1] not in tails:
        print(f"LLM_MODEL={configured!r} is NOT in that list — every call will 404.")
        print("Set LLM_MODEL in .env to one of the ids above, then re-run this.")
        return 1
    print(f"LLM_MODEL={configured!r} exists.\n")

    # ── 2/3. Real decisions, and whether they parsed ─────────────────────
    from src.agent.policy_agent import PolicyAgent

    agent = PolicyAgent()
    print(f"Making {args.samples} real decisions...\n")
    for i in range(args.samples):
        fc = CLASSES[i % len(CLASSES)]
        action = await agent.decide(_context(fc, i))
        fell_back = action.reason.startswith("Fallback:")
        mark = "fallback" if fell_back else "ok      "
        print(f"  [{mark}] {fc:<22} -> {action.action:<15} {action.reason[:52]}")

    fallbacks = agent.fallback_count
    rate = fallbacks / agent.call_count * 100 if agent.call_count else 0.0
    print(f"\n{agent.call_count} calls, {fallbacks} fallbacks ({rate:.0f}%)")

    if agent.last_error_status is not None:
        print(f"\nProvider error HTTP {agent.last_error_status} — the eval would abort on this.")
        return 1
    if rate > 20:
        print(
            "\nToo many fallbacks. The model is reachable but is not holding the "
            "JSON contract, so an eval run would be mostly heuristic and the "
            "harness would drop the LLM row. Try a larger model."
        )
        return 1

    print("\nGood to run:  python -m eval.runner --scenarios 300 --seeds 3")
    print("That is 2,700 calls (scenarios x 3 attempts x 3 seeds).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
