"""The B2B receivables chaser — a staged, account-level dunning layer.

This package is INTEGRATED: the orchestrator links invoice cases to AR
accounts, the scheduler runs the consolidation + dunning sweeps, the
merchant API (/ar) and console panel consume it, and customer plans and
disputes ride on it, per docs/receivables-integration-plan.md.

The module map, in dependency order:

    models.py     the tables (accounts, contacts, plans, disputes, alerts,
                  tasks) — registered on the shared Base, migration pending
    accounts.py   one buyer = one account = one contact budget; the
                  consolidation layer
    ladder.py     the frozen 5-stage dunning ladder + B2B contact window
    statement.py  statement-of-account composer: subject/SMS/email bodies
    disputes.py   dispute open/resolve — the chase freeze + merchant alert
    plans.py      instalment plans built ON the existing promise machinery
    external.py   NEFT/cheque payments closing cases honestly (counted,
                  never claimed)
    tasks.py      human call tasks — merchant-side work, never budget spend
    segments.py   payment-behaviour segments, adaptive INSIDE the envelope
    aging.py      aging buckets, days-to-pay, promise effectiveness
    alerts.py     the merchant writeback queue behind every transition
"""

from __future__ import annotations

from src.receivables.accounts import (
    account_ref_for_case,
    active_contacts,
    add_contact,
    get_or_create_account,
)
from src.receivables.alerts import AlertType, raise_alert
from src.receivables.disputes import open_dispute, resolve_dispute
from src.receivables.external import record_external_payment
from src.receivables.ladder import (
    INVOICE_LADDER,
    LadderStage,
    is_b2b_contact_time,
    next_b2b_window,
    next_stage_gap_hours,
    stage_after_break,
    stage_for_aging,
    stage_for_level,
)
from src.receivables.models import (
    AccountTask,
    ArAccount,
    ArContact,
    ArContactLog,
    CaseDispute,
    MerchantAlert,
    PaymentPlan,
    PlanInstalment,
)
from src.receivables.plans import create_plan, plan_progress, validate_plan_shape
from src.receivables.segments import classify, entry_stage_level, gap_multiplier
from src.receivables.statement import compose_stage_message
from src.receivables.tasks import raise_call_task

__all__ = [
    "AccountTask",
    "AlertType",
    "ArAccount",
    "ArContact",
    "ArContactLog",
    "CaseDispute",
    "INVOICE_LADDER",
    "LadderStage",
    "MerchantAlert",
    "PaymentPlan",
    "PlanInstalment",
    "account_ref_for_case",
    "active_contacts",
    "add_contact",
    "classify",
    "compose_stage_message",
    "create_plan",
    "entry_stage_level",
    "gap_multiplier",
    "get_or_create_account",
    "is_b2b_contact_time",
    "next_b2b_window",
    "next_stage_gap_hours",
    "open_dispute",
    "plan_progress",
    "raise_alert",
    "raise_call_task",
    "record_external_payment",
    "resolve_dispute",
    "stage_after_break",
    "stage_for_aging",
    "stage_for_level",
    "validate_plan_shape",
]
