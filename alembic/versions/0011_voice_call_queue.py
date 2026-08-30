"""Voice call queue — the telephony leg's work items.

Revision ID: 0011_voice_call_queue
Revises: 0010_receivables
Create Date: 2026-08-30

The engine never dials a phone. When a chase's touch should be a voice call,
the orchestrator writes a voice_call_queue row in the SAME transaction as the
chase attempt (write-ahead, the same correctness property as RetryAttempt);
a telephony leg claims rows via POST /voice/queue/claim and places the calls
through its own provider. The queue is gated by VOICE_CHASER_ENABLED at
queue time, so the table can exist empty forever without changing behavior.

Additive, inspector-guarded, idempotent like every revision here.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011_voice_call_queue"
down_revision = "0010_receivables"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("voice_call_queue"):
        return
    op.create_table(
        "voice_call_queue",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "recovery_case_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("recovery_cases.id"),
            nullable=False,
        ),
        sa.Column("retry_attempt_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_contact", sa.String(20)),
        sa.Column("risk_type", sa.String(40), nullable=False),
        sa.Column("amount_paise", sa.Integer, nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_by", sa.String(120)),
        sa.Column("result", sa.String(40)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_voice_call_queue_recovery_case_id",
        "voice_call_queue",
        ["recovery_case_id"],
    )
    op.create_index(
        "ix_voice_call_queue_state_created",
        "voice_call_queue",
        ["state", "created_at"],
    )


def downgrade() -> None:
    if not _has_table("voice_call_queue"):
        return
    op.drop_index("ix_voice_call_queue_state_created", table_name="voice_call_queue")
    op.drop_index("ix_voice_call_queue_recovery_case_id", table_name="voice_call_queue")
    op.drop_table("voice_call_queue")
