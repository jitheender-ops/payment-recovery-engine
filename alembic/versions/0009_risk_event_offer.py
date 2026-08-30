"""Offer id on risk_events — the merchant incentive relay, cart chases only.

Revision ID: 0009_risk_event_offer
Revises: 0008_promise_capture
Create Date: 2026-08-30

checkout_abandonment events may now carry a Razorpay offer id (the
merchant's own offer, in the merchant's own account). The engine relays
the incentive to the payment link from the SECOND touch on — never the
first, because the research (Klaviyo 2024 benchmarks) is unambiguous
that an incentive on touch 1 trains shoppers to wait for discounts.

One nullable column, no backfill, no index: reads ride the existing
ix_risk_events_reference (risk_type, reference_id) lookup the chaser
already uses. Non-cart events are refused at the schema layer
(RiskEventIn validator), so the column is only ever populated for carts.

Inspector-guarded and idempotent like every revision here.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_risk_event_offer"
down_revision = "0008_promise_capture"
branch_labels = None
depends_on = None


def _has_column(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return name in (c["name"] for c in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("risk_events", "offer_id"):
        op.add_column(
            "risk_events", sa.Column("offer_id", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    if _has_column("risk_events", "offer_id"):
        op.drop_column("risk_events", "offer_id")
