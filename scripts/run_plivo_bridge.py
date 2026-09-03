"""Run the Plivo call bridge — the worker that dials queued voice calls.

    python scripts/run_plivo_bridge.py

Claims one queued call at a time from POST /voice/queue/claim, dials it
through Plivo, and polls again — the XML callbacks in src/voice/plivo_bridge.py
carry the conversation. Every env var it needs is fail-closed checked by
claim_and_dial() itself; this loop just adds patience.

Flags:
    --once     dial at most one call, then exit (a cron/tick-friendly form)
    --dry-run  check configuration and exit — proves the env is right
                without claiming anything.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import logging  # noqa: E402

from src.config import get_settings  # noqa: E402
from src.voice.plivo_bridge import BridgeError, claim_and_dial  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one claim attempt, then exit")
    parser.add_argument("--dry-run", action="store_true", help="verify config and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = get_settings()
    if args.dry_run:
        problems = []
        if not settings.plivo_auth_id:
            problems.append("PLIVO_AUTH_ID is not set")
        if not settings.plivo_auth_token.get_secret_value():
            problems.append("PLIVO_AUTH_TOKEN is not set")
        if not settings.plivo_caller_number:
            problems.append("PLIVO_CALLER_NUMBER is not set (E.164, e.g. +91XXXXXXXXXX)")
        if not settings.plivo_bridge_base_url:
            problems.append("PLIVO_BRIDGE_BASE_URL is not set (public https:// URL)")
        if not settings.voice_webhook_secret.get_secret_value():
            problems.append("VOICE_WEBHOOK_SECRET is not set")
        if not settings.sarvam_api_key.get_secret_value():
            problems.append("SARVAM_API_KEY is not set (STT/TTS on the call)")
        if problems:
            for p in problems:
                print(f"MISSING: {p}")
            return 1
        print("Bridge configuration looks complete.")
        return 0

    poll = max(1, settings.plivo_bridge_poll_seconds)
    while True:
        try:
            call = claim_and_dial()
        except BridgeError as e:
            print(f"bridge error: {e}", file=sys.stderr)
            return 1
        if call is not None:
            # Calls are paced: never claim the next while one is live —
            # the state dict and audio dir are per-call, and a recovery
            # blitz reads as spam on the customer's phone.
            time.sleep(60)
        if args.once:
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
