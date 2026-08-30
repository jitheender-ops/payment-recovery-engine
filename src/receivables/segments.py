"""Payment-behaviour segments — adaptive intensity INSIDE the frozen envelope.

Every mature AR platform (Gaviti's credit-risk tiers, Upflow's customer
insights) adapts HOW it chases per buyer: a chronically-late payer and a
first-time-slow payer get different ladders. The research is equally clear
about the failure mode — an "adaptive" system that can exceed its promises
is an unpredictable system, and a B2B merchant cannot put an unpredictable
contact policy in front of their customers.

So segments SELECT WITHIN the envelope; they never widen it. The frozen
invoice_overdue policy stays the law (4 contacts, 30 days); a segment may
only choose which rungs to use, skip, or stretch:

    prompt       — pays within days of due: skip the courtesy rung, keep
                   the standard gaps (never punish good payers)
    standard     — the ladder exactly as frozen (the default)
    slow         — pays but late: skip the pre-due courtesy rung and start
                   at rung 2 directly; gaps unchanged
    chronic_late — reliably late: skip to rung 2 AND stretch rung gaps by
                   1.5x — the account demonstrably ignores faster cadences,
                   so the same 4 contacts spread across the full window
    default_risk — broken promises or disputes on record: start at rung 2,
                   standard gaps — the account is already at the
                   relationship-preservation stage

Deterministic rules, not ML: the payment rail's XGBoost stays payment-rail
only (documented there), and a receivables segment a merchant can be shown
the rule for beats a score nobody can explain. Recomputed on closed cases
only — a segment never moves mid-chase (that is the unpredictability the
envelope exists to prevent).
"""

from __future__ import annotations

from typing import Literal

Segment = Literal["prompt", "standard", "slow", "chronic_late", "default_risk"]


# Classification thresholds — frozen with the ladder, in days. A buyer is
# judged on their MEDIAN days-to-pay across closed cases, because the mean
# is one holiday-adjacent outlier away from mislabelling a good payer.
PROMPT_DAYS = 3     # median days-to-pay ≤ 3 after due → prompt
CHRONIC_DAYS = 14    # median days-to-pay > 14 → chronic_late


def classify(
    *,
    median_days_to_pay: float | None,
    closed_cases: int,
    broken_promises: int = 0,
    disputes: int = 0,
) -> Segment:
    """
    The segment for one account. Deterministic, total, and readable as a rule.

    Precedence is deliberate and one-directional:
      1. default_risk beats everything — broken promises and disputes are
         facts about this account's CURRENT cycle, not statistics
      2. no history (no closed cases) → standard: the ladder as frozen.
         Guessing "prompt" for an unknown buyer spends trust on nothing.
      3. median days-to-pay decides prompt / standard / slow / chronic_late
    """
    if broken_promises >= 2 or disputes >= 1:
        return "default_risk"
    if not closed_cases or median_days_to_pay is None:
        return "standard"
    if median_days_to_pay <= PROMPT_DAYS:
        return "prompt"
    if median_days_to_pay <= CHRONIC_DAYS:
        return "slow"
    return "chronic_late"


def entry_stage_level(segment: Segment) -> int:
    """The ladder level a segment's chase begins at (rung index, 1-based)."""
    if segment in ("slow", "chronic_late", "default_risk"):
        return 2
    return 1


def gap_multiplier(segment: Segment) -> float:
    """The multiplier a segment applies to the ladder's rung gaps."""
    return 1.5 if segment == "chronic_late" else 1.0
