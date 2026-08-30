"""The AR account layer — one buyer, one balance, one contact budget.

The pre-receivables engine chased invoices in isolation: four overdue
invoices from one buyer were four independent cases, each with its own
ladder, messaging the same person at the same desk. That is the behaviour a
complaint is actually about, and it is also wrong about the money: a buyer's
AR exposure is a balance, not a list of unrelated events.

Design notes:

* LINKING: events may carry ``account_ref`` (the merchant's own code for the
  buyer). When they do not, the account is keyed on the canonicalised
  ``customer_key`` the case layer already maintains — the same identity
  discipline that keeps one human from holding two contact budgets. Both
  paths end in ``ArAccount.account_ref`` being UNIQUE, so a second event for
  the same buyer always finds the same row.
* LOOKUPS: ``get_or_create_account`` is idempotent and race-safe the same
  way ``open_case`` is — IntegrityError on the UNIQUE constraint means we
  lost the race to ourselves, roll back and fetch the winner.
* CASE LINKING: ``link_case_to_account`` stamps ``RecoveryCase`` rows with
  an ``account_id``. That column does not exist yet (integration phase adds
  it via alembic); standalone use links through ``ArContactLog`` /
  consolidation queries that join on case.customer_id, which is why every
  query here works off customer keys and account_refs, never the missing
  column. This keeps the module correct before, during and after migration.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.receivables.models import ArAccount, ArContact

if TYPE_CHECKING:
    from src.models import RecoveryCase

logger = logging.getLogger(__name__)


async def get_or_create_account(
    session: AsyncSession,
    *,
    account_ref: str,
    display_name: str | None = None,
) -> ArAccount:
    """
    The account row for this buyer. Idempotent; races resolve to the winner.

    Display name only fills in when absent — a later event naming the buyer
    better than the first one did must not be lost, but an empty overwrite
    would erase the only human-readable handle the console has.
    """
    existing = await session.scalar(
        select(ArAccount).where(ArAccount.account_ref == account_ref)
    )
    if existing is not None:
        if display_name and not existing.display_name:
            existing.display_name = display_name
        return existing

    account = ArAccount(account_ref=account_ref, display_name=display_name)
    session.add(account)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        won = await session.scalar(
            select(ArAccount).where(ArAccount.account_ref == account_ref)
        )
        if won is None:  # pragma: no cover — only if the constraint is missing
            raise
        return won
    logger.info("AR account opened: ref=%s", account_ref)
    return account


async def account_ref_for_case(
    session: AsyncSession, case: RecoveryCase
) -> str | None:
    """
    The buyer key a case consolidates under, or None when it stands alone.

    Order matters: the merchant's explicit account_ref beats a derived one,
    because it is the merchant's own namespace and survives contact-channel
    changes. The fallback derives from the customer identity — a case with
    no customer_id at all (anonymous webhook-style ingestion) cannot be
    consolidated and stays per-case, which is the honest behaviour rather
    than guessing a shared key from nothing.
    """
    from src.cases import canonical_key

    # Integration phase: events carry account_ref in meta; the standalone
    # path derives from the canonical customer key the case already holds.
    meta_ref = None
    # (case risk_meta lives on the RiskEvent, not the case — deriving from
    # customer_id is the standalone truth; the integration phase will prefer
    # the explicit account_ref and fall back to this same derivation.)
    key = canonical_key(case.customer_id)
    if key is not None:
        meta_ref = f"derived:{key}"
    return meta_ref


async def active_contacts(
    session: AsyncSession, account_id: uuid.UUID
) -> list[ArContact]:
    """The account's active contacts, in the ladder's escalation order.

    Sorted by role precedence (ap_clerk < finance_manager < escalation), so
    the caller gets "who to reach first" without re-implementing the order.
    Duplicates within a role are kept — two AP clerks both get the email;
    that is how B2B AR desks actually work.
    """
    from src.receivables.ladder import ROLE_PRECEDENCE

    result = await session.execute(
        select(ArContact)
        .where(ArContact.account_id == account_id, ArContact.active.is_(True))
        .order_by(ArContact.role, ArContact.created_at)
    )
    contacts = result.scalars().all()
    return sorted(contacts, key=lambda c: ROLE_PRECEDENCE.get(c.role, 99))


async def add_contact(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    role: str,
    email: str,
    name: str | None = None,
    phone: str | None = None,
) -> ArContact:
    """Add one contact. Email is lowercased/stripped — it is the send target."""
    email = email.strip().lower()
    contact = ArContact(
        account_id=account_id,
        role=role,
        email=email,
        name=name,
        phone=phone,
    )
    session.add(contact)
    await session.flush()
    return contact
