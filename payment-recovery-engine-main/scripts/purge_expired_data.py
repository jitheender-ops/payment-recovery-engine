"""
Purge customer PII older than DATA_RETENTION_DAYS.

Run manually or on a schedule (cron / a scheduled CI job / Docker Compose
sidecar — this repo has no job scheduler installed, see README known-gaps):

    python -m scripts.purge_expired_data          # purge using configured retention window
    python -m scripts.purge_expired_data --dry-run  # report counts only, no deletes
    python -m scripts.purge_expired_data --days 30  # override the configured window

What gets purged, and why this split:
    - webhook_events   → rows older than the cutoff are deleted outright.
                          The payload column already had PII redacted at
                          ingestion (see src/crypto.py redact_payload_pii),
                          but the row is still linked 1:1 to a real
                          transaction, so it doesn't need to live forever.
    - payment_failures → PII columns (customer_email, customer_contact, vpa)
                          are overwritten with NULL in place; the row itself
                          (amount, failure_class, error_code, timestamps) is
                          KEPT, because the eval harness and the dashboard's
                          historical metrics depend on aggregate failure data
                          that contains no PII once these three columns are
                          cleared.
    - retry_attempts / retry_ledger → no PII columns, left untouched.

This is a deliberately narrow, auditable script rather than a blanket
`DELETE ... WHERE created_at < X` across every table, because the retention
requirement is specifically about *customer-identifying* data, not about
losing the recovery-rate history the whole project is evaluated on.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import delete, func, select, update

from src.config import get_settings
from src.database import async_session_factory
from src.models import PaymentFailure, WebhookEvent

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)


async def purge(retention_days: int, dry_run: bool) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    logger.info(
        "Purging data older than %s (%d day retention window)%s",
        cutoff.isoformat(),
        retention_days,
        " [DRY RUN]" if dry_run else "",
    )

    async with async_session_factory() as session:
        # ── webhook_events: delete outright ─────────────────────────────
        webhook_count = await session.scalar(
            select(func.count()).select_from(WebhookEvent).where(WebhookEvent.received_at < cutoff)
        )
        logger.info("webhook_events: %d row(s) eligible for deletion", webhook_count or 0)
        if not dry_run and webhook_count:
            await session.execute(delete(WebhookEvent).where(WebhookEvent.received_at < cutoff))

        # ── payment_failures: null out PII columns, keep the row ────────
        pii_count = await session.scalar(
            select(func.count())
            .select_from(PaymentFailure)
            .where(
                PaymentFailure.failed_at < cutoff,
                (
                    PaymentFailure.customer_email.is_not(None)
                    | PaymentFailure.customer_contact.is_not(None)
                    | PaymentFailure.vpa.is_not(None)
                ),
            )
        )
        logger.info("payment_failures: %d row(s) eligible for PII redaction", pii_count or 0)
        if not dry_run and pii_count:
            await session.execute(
                update(PaymentFailure)
                .where(PaymentFailure.failed_at < cutoff)
                .values(customer_email=None, customer_contact=None, vpa=None)
            )

        if dry_run:
            await session.rollback()
        else:
            await session.commit()
            logger.info("Purge complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Override DATA_RETENTION_DAYS from settings.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be purged without deleting/redacting anything.",
    )
    args = parser.parse_args()

    settings = get_settings()
    retention_days = args.days if args.days is not None else settings.data_retention_days
    asyncio.run(purge(retention_days, args.dry_run))


if __name__ == "__main__":
    main()
