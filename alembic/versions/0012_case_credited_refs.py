"""Credited refs — every capture ever credited to a recovery case.

Revision ID: 0012_case_credited_refs
Revises: 0011_voice_call_queue
Create Date: 2026-09-02

recovery_cases.recovered_ref holds only the LATEST capture. A case paid in
three or more parts had no list to test membership against, so an earlier
distinct capture replaying through the reconcile path could credit twice.
credited_refs is that list; cases.py seeds it from recovered_ref where that
column is already set, so existing rows keep the replay protection they had.

Additive, inspector-guarded, idempotent like every revision here.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_case_credited_refs"
down_revision = "0011_voice_call_queue"
branch_labels = None
depends_on = None


def _columns(table: str) -> list[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return []
    return [c["name"] for c in inspector.get_columns(table)]


def upgrade() -> None:
    cols = _columns("recovery_cases")
    if "credited_refs" in cols:
        return
    # NOT NULL with a server_default of '[]' from the start: no post-add
    # ALTER (SQLite cannot ALTER COLUMN) and no backfill gap — rows get the
    # empty list the model default would have given them.
    #
    # sa.text, not the bare string: SQLAlchemy renders a plain-string
    # server_default by wrapping it in quotes, so "'[]'" became '''[]''' on
    # Postgres — invalid JSON, and the first deploy died on it. The migration
    # chain check runs on SQLite, which does not validate JSON defaults, so
    # nothing local caught it. sa.text passes the literal through verbatim.
    op.add_column(
        "recovery_cases",
        sa.Column(
            "credited_refs",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    # Seed from recovered_ref so existing single-capture cases keep their
    # replay refusal; rows with no capture stay empty-listed. A JSON text
    # literal, not a jsonb_build_array() call: the same statement must run
    # on Postgres and on the SQLite the migration chain check uses.
    op.execute(
        "UPDATE recovery_cases "
        "SET credited_refs = '[\"' || recovered_ref || '\"]' "
        "WHERE recovered_ref IS NOT NULL"
    )


def downgrade() -> None:
    if "credited_refs" not in _columns("recovery_cases"):
        return
    op.drop_column("recovery_cases", "credited_refs")
