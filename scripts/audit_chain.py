"""
Stamp or verify the case_events hash chain.

Usage:
    python scripts/audit_chain.py --stamp     # chain any events written since the last run
    python scripts/audit_chain.py --verify    # epoch-verify: tail recompute + signatures
                                              # + one epoch content-re-checked per run
    python scripts/audit_chain.py --verify-full  # recompute EVERY event from content

--verify is what to run routinely (O(recent history); see
src/audit_checkpoint.py — with checkpoints present it also re-reads one old
epoch per invocation on rotation, so a rewrite inside checkpointed history is
caught within one rotation even when every stored hash was left alone).
--verify-full is the exhaustive audit — forensics, key rotations, or a
deployment small enough not to care about the difference.

The 20-second version for a pitch video: run --stamp once, then --verify
and show it passing. Then, separately, connect to the database and hand-edit
one `detail` field on any case_events row, and run --verify again — it names
the exact row where the chain breaks.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.audit_chain import (  # noqa: E402
    AuditChainNotKeyedError,
    stamp_unhashed_events,
    verify_chain,
)
from src.audit_checkpoint import verify_chain_epoch  # noqa: E402
from src.database import async_session_factory  # noqa: E402


async def _stamp() -> None:
    async with async_session_factory() as session:
        n = await stamp_unhashed_events(session)
        await session.commit()
    print(f"Stamped {n} event(s).")


async def _verify() -> None:
    async with async_session_factory() as session:
        ok, detail, _ = await verify_chain_epoch(session)
        # The rotation marker the epoch path stamps is real work product —
        # commit it so the next run advances the rotation.
        await session.commit()
    print(detail)
    raise SystemExit(0 if ok else 1)


async def _verify_full() -> None:
    async with async_session_factory() as session:
        result = await verify_chain(session)
    print(result.detail)
    raise SystemExit(0 if result.intact else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp", action="store_true", help="Chain unhashed events")
    parser.add_argument(
        "--verify", action="store_true",
        help="Epoch verification: tail recompute + signatures + rotating "
             "content re-check of one old epoch per run",
    )
    parser.add_argument(
        "--verify-full", action="store_true",
        help="Exhaustive: recompute every event from content (forensics)",
    )
    args = parser.parse_args()

    if not (args.stamp or args.verify or args.verify_full):
        parser.print_help()
        raise SystemExit(1)

    try:
        if args.stamp:
            asyncio.run(_stamp())
        if args.verify:
            asyncio.run(_verify())
        if args.verify_full:
            asyncio.run(_verify_full())
    except AuditChainNotKeyedError as e:
        print(f"refused: {e}", file=sys.stderr)
        raise SystemExit(2) from e


if __name__ == "__main__":
    main()
