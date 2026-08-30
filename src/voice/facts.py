"""
Case facts for the voice agent — the only source of customer-specific truth.

The voice pipeline is allowed to speak two kinds of sentence: product
knowledge (knowledge.py) and case facts (this module). Nothing else. The
purpose of this file is to make that boundary physical: every fact the
agent can state about a case exists here as a pre-built, exact string —
formatted, PII-bounded, and logged in the case's audit trail — so the
grounding gate verifies against strings the system actually produced,
never against a number the LLM improvised.

Identity comes from the caller's lookup: the telephony provider passes a
customer_id (or the case's subject reference) with the webhook. No amount
is ever stated before identity is bound to a case row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.cases import outstanding_paise
from src.formatting import ist, money
from src.models import RecoveryCase


@dataclass(frozen=True)
class CaseFacts:
    case_id: uuid.UUID
    risk_type: str
    subject_ref: str
    amount_at_risk: str
    amount_recovered: str
    # What is still owed — at_risk minus recovered (cases.outstanding_paise).
    # Its own field rather than a swap of amount_at_risk: the two are
    # different facts, a part-paid case has to be able to state both, and the
    # grounding gate verifies against whichever string the answer used.
    amount_outstanding: str
    state: str
    recovered_at_ist: str | None
    attempts_used: int
    max_attempts: int

    def as_passages(self, merchant_name: str) -> list[str]:
        """
        Every statement about this case the voice agent may make — each one a
        self-contained passage the grounding gate can check an answer against.
        """
        label = self.risk_type.replace("_", " ")
        passages = [
            (
                f"customer's {label} reference {self.subject_ref} with "
                f"{merchant_name} is pending, amount at risk "
                f"{self.amount_at_risk}."
            ),
            (
                f"recovery status of the {label}: case state is {self.state}, "
                f"{self.attempts_used} of {self.max_attempts} recovery "
                f"attempts used."
            ),
        ]
        # Only when a part payment has actually landed. With nothing recovered
        # the outstanding IS the amount at risk, and a second passage saying
        # the same rupees twice is noise the grounding gate has to weigh.
        if self.amount_outstanding != self.amount_at_risk:
            passages.append(
                f"part payment received on the {label}: "
                f"{self.amount_recovered} paid, "
                f"{self.amount_outstanding} still outstanding."
            )
        if self.recovered_at_ist:
            passages.append(

                    f"the {label} was recovered on {self.recovered_at_ist} "
                    f"IST, amount recovered {self.amount_recovered}."

            )
        return passages


async def load_facts(
    session: AsyncSession, *, case_id: uuid.UUID | None = None, subject_ref: str | None = None
) -> CaseFacts | None:
    """One case's facts by id, or by the merchant's own reference."""
    stmt = sa.select(RecoveryCase)
    if case_id is not None:
        stmt = stmt.where(RecoveryCase.id == case_id)
    elif subject_ref is not None:
        stmt = stmt.where(RecoveryCase.subject_ref == subject_ref).limit(1)
    else:
        return None
    case = (await session.execute(stmt.limit(1))).scalars().first()
    if case is None:
        return None
    return CaseFacts(
        case_id=case.id,
        risk_type=case.risk_type,
        subject_ref=case.subject_ref,
        amount_at_risk=money(case.amount_at_risk),
        amount_recovered=money(case.amount_recovered),
        amount_outstanding=money(outstanding_paise(case)),
        state=case.state,
        recovered_at_ist=(
            ist(case.recovered_at).strftime("%d %b %Y, %H:%M") if case.recovered_at else None
        ),
        attempts_used=case.attempts_used,
        max_attempts=case.max_attempts,
    )
