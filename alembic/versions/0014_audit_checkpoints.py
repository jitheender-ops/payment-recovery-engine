"""Audit chain checkpoints — O(recent) verification for a growing chain.

Revision ID: 0014_audit_checkpoints
Revises: 0013_promise_mandate
Create Date: 2026-09-03

The case_events hash chain is one global sequence, so full verification
re-reads every row ever written — fine at thousands, minutes-to-hours at
millions. audit_checkpoints stores one keyed anchor per epoch: verification
after an anchor recomputes only the tail and checks older stretches by
signature. Tamper-evidence is unchanged; see src/audit_checkpoint.py.

Additive, inspector-guarded, idempotent like every revision here.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_audit_checkpoints"
down_revision = "0013_promise_mandate"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("audit_checkpoints"):
        # 0014 shipped once without the rotation marker; a second boot of the
        # same (uncommitted) revision adds it rather than skipping the table.
        cols = [
            c["name"]
            for c in sa.inspect(op.get_bind()).get_columns("audit_checkpoints")
        ]
        if "content_verified_at" not in cols:
            op.add_column(
                "audit_checkpoints",
                sa.Column("content_verified_at", sa.DateTime(timezone=True)),
            )
        return
    op.create_table(
        "audit_checkpoints",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("last_event_id", sa.Integer, nullable=False),
        sa.Column("head_event_hash", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("events_through", sa.Integer, nullable=False),
        sa.Column("content_verified_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_audit_checkpoints_last_event_id",
        "audit_checkpoints",
        ["last_event_id"],
    )


def downgrade() -> None:
    if not _has_table("audit_checkpoints"):
        return
    op.drop_index(
        "ix_audit_checkpoints_last_event_id", table_name="audit_checkpoints"
    )
    op.drop_table("audit_checkpoints")
