"""Human tasks — the collection touches a ladder must not automate.

Stage 3 of the ladder calls for a phone call. A call is the highest-
converting B2B collection touch (every dunning platform's published playbook
says so) and it is precisely the touch a software system cannot make. The
row is the queue entry: who to ask for, which account, what is outstanding,
and the script context — the merchant's team works the queue, marks it done,
and the ladder proceeds.

Not counted against the customer's contact budget: the frozen policy bounds
ENGINE-initiated contacts, and a human deciding to call is the merchant's
own action. Mixing the two would quietly rewrite the product's promise.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.receivables.alerts import raise_alert
from src.receivables.models import AccountTask


async def raise_call_task(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    account_ref: str | None,
    detail: dict[str, Any],
    now: datetime | None = None,
) -> AccountTask:
    """Queue one call task and its alert. The caller owns the commit."""
    task = AccountTask(
        account_id=account_id,
        kind="call",
        detail=detail,
        status="pending",
    )
    session.add(task)
    await session.flush()
    await raise_alert(
        session,
        event_type="call_task_raised",
        account_ref=account_ref,
        detail=detail,
    )
    return task


async def complete_task(session: AsyncSession, task: AccountTask) -> AccountTask:
    """Mark a task done. Idempotent: a done task stays done."""
    if task.done_at is None:
        task.done_at = datetime.now(UTC)
        task.status = "done"
        await session.flush()
    return task
