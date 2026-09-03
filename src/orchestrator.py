"""
Payment Recovery Orchestrator — ties all five layers together.

Flow: classify → check retryability → build context → agent decision →
guardrail validation → execute/nudge → log everything.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src import downtime, recovery_link
from src.agent.actions import FailureContext, RetryAction
from src.agent.policy_agent import PolicyAgent
from src.cases import (
    CaseEventType,
    PromiseScore,
    attach_attempt,
    canonical_key,
    close_case,
    customer_key,
    customer_promise_score,
    ledger_keys,
    log_event,
    open_case,
    stop_reason,
)
from src.chasers.policy import RiskPolicy, policy_for
from src.classifier.mapper import ClassifierMapper
from src.config import get_settings
from src.executor.rail_selector import resolve_target_rail
from src.executor.retry_executor import RetryExecutor
from src.guardrail.gate import GuardrailGate
from src.guardrail.rules import IST, clamp_retry_at_out_of_blackout, is_in_blackout
from src.messaging.nudge_generator import NudgeGenerator
from src.models import (
    PaymentFailure,
    PromiseToPay,
    RecoveryCase,
    RetryAttempt,
    RetryLedger,
    RiskEvent,
    VoiceCallQueue,
    WebhookEvent,
)

# Module level, alongside the blackout rule above: chase_case consults both
# clocks and they belong at the same seam. ladder.py imports nothing from the
# app, so there is no cycle to work around — the function-local import this
# replaces also forced tests to patch the ladder module itself, which broke
# next_b2b_window() for the tests that exercise the rule directly.
from src.receivables.ladder import is_b2b_contact_time, next_b2b_window

if TYPE_CHECKING:
    from src.agent.xgboost_baseline import XGBoostBaseline

logger = logging.getLogger(__name__)


def _aware(dt: datetime | None) -> datetime | None:
    """
    Coerce a DB-returned timestamp to timezone-aware, assuming UTC when naive.

    Postgres timestamptz comes back aware; the SQLite test harness comes back
    naive. Every arithmetic against now() goes through this so the window math
    cannot blow up on one dialect and work on the other — the same coercion
    check_consent_window() already performs at its own boundary.
    """
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)


def cart_summary_from_meta(meta: dict[str, Any] | None) -> str | None:
    """
    One bounded, printable line naming what was left in the cart.

    Merchant meta is untrusted free-form (see sanitize_meta for the prompt
    discipline); this is the same reduction for the message path. Accepts
    either a list of item names or a single string; anything else is not a
    cart we can name honestly, and None renders exactly the old nudge.
    """
    if not isinstance(meta, dict):
        return None
    raw = meta.get("cart_items")
    if isinstance(raw, str):
        parts = [raw]
    elif isinstance(raw, (list, tuple)):
        parts = [p for p in raw if isinstance(p, str) and p.strip()]
    else:
        return None
    if not parts:
        return None
    line = ", ".join(" ".join(p.split()) for p in parts)
    return line[:80].strip() or None


def _stop_event_type(stop: str) -> CaseEventType:
    """
    "deferred" and "stopped" are different facts and the audit has to keep
    them apart: one case is waiting out an escalation gap, the other is
    finished. Reading both as "we did nothing" is how a bounded workflow
    gets mistaken for a broken one.
    """
    return "deferred" if stop.startswith("next action not due") else "stopped"


class PaymentRecoveryOrchestrator:
    """
    Central orchestrator — Layer 1 (ingestion) calls into this after
    storing the webhook event and classifying the failure.
    """

    def __init__(self) -> None:
        self._classifier = ClassifierMapper()
        self._guardrail = GuardrailGate()
        self._nudge_gen = NudgeGenerator()
        self._executor = RetryExecutor()
        self._agent: PolicyAgent | None = None
        self._xgboost: XGBoostBaseline | None = None

    def _get_agent(self) -> PolicyAgent | None:
        """Lazy-init agent (needs API keys at runtime). None if init failed."""
        if self._agent is None:
            try:
                self._agent = PolicyAgent()
            except Exception:
                logger.warning("Failed to init PolicyAgent — will use XGBoost fallback")
        return self._agent

    def _get_xgboost(self) -> XGBoostBaseline:
        """Cached baseline: the old path reloaded the model from disk on every
        fallback decision. Imported lazily so tests that patch the class keep
        working against a freshly constructed orchestrator."""
        if self._xgboost is None:
            from src.agent.xgboost_baseline import XGBoostBaseline

            self._xgboost = XGBoostBaseline()
        return self._xgboost

    async def _decide_action(
        self, context: FailureContext, subject: str
    ) -> tuple[RetryAction, str]:
        """
        Agent decision with XGBoost fallback. Returns (action, agent_type).

        agent_type must record who ACTUALLY decided, not who was configured to.
        PolicyAgent.decide() catches its own LLM errors and returns a heuristic
        action, so "an agent object exists" says nothing about whether the LLM
        answered. Comparing its fallback counter across the call is what
        distinguishes a real LLM decision from a silent degradation — without
        it the audit trail claims an LLM made calls it never saw.

        Shared by every decision path (live webhook, chaser sweep, fired
        retry) so the distinction lives in exactly one place.
        """
        agent = self._get_agent()
        action: RetryAction | None = None
        agent_type = "xgboost"
        if agent:
            fallbacks_before = agent.fallback_count
            try:
                candidate = await agent.decide(context)
                if agent.fallback_count == fallbacks_before:
                    action, agent_type = candidate, "llm"
                else:
                    # The LLM degraded. decide() has already swallowed the error
                    # and handed back its own private heuristic, which abandons
                    # everything that is not a network error or bank downtime —
                    # so accepting `candidate` here means a missing API key
                    # quietly turns the engine into "give up on ~70% of
                    # recoverable payments".
                    #
                    # That is also why the XGBoost path was unreachable in
                    # practice and the README's "falls back to XGBoost" was not
                    # true: decide() never raises, so the `except` below never
                    # fired. The counter told us it degraded; now we act on it.
                    logger.warning(
                        "LLM degraded to its internal heuristic — using XGBoost instead: %s",
                        candidate.reason[:120],
                    )
            except Exception:
                logger.exception("Agent raised, using XGBoost: %s", subject)

        if action is None:
            action = self._get_xgboost().predict(context)
            agent_type = "xgboost"
        return action, agent_type

    async def process_payment_failure(
        self, event: WebhookEvent, session: AsyncSession
    ) -> None:
        """
        Main pipeline entry point.

        1. Parse payment entity from webhook payload
        2. Classify failure (deterministic)
        3. Create PaymentFailure record
        4. If hard decline → abandon (no agent call)
        5. Build FailureContext
        6. Agent decision
        7. Guardrail validation
        8. Execute (retry/nudge) or log rejection
        9. Update ledger
        """
        payload = event.payload
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})

        if not payment_entity:
            logger.error("No payment entity in webhook payload: event=%s", event.razorpay_event_id)
            return

        payment_id = payment_entity.get("id", "unknown")
        logger.info("Processing payment failure: %s", payment_id)

        # ── Step 2: Classify ──────────────────────────────────────────────
        error_code = payment_entity.get("error_code", "UNKNOWN")
        error_desc = payment_entity.get("error_description")
        error_source = payment_entity.get("error_source")
        error_step = payment_entity.get("error_step")
        error_reason = payment_entity.get("error_reason")

        failure_class, is_retryable = self._classifier.classify(
            error_code, error_desc, error_source, error_step, error_reason
        )

        # ── Step 3: Create PaymentFailure record ──────────────────────────
        card_info = payment_entity.get("card", {}) or {}
        failure_record = PaymentFailure(
            payment_id=payment_id,
            order_id=payment_entity.get("order_id"),
            amount=payment_entity.get("amount", 0),
            currency=payment_entity.get("currency", "INR"),
            method=payment_entity.get("method", "unknown"),
            bank=payment_entity.get("bank"),
            wallet=payment_entity.get("wallet"),
            vpa=payment_entity.get("vpa"),
            card_network=card_info.get("network"),
            card_type=card_info.get("type"),
            card_issuer=card_info.get("issuer"),
            error_code=error_code,
            error_description=error_desc,
            error_source=error_source,
            error_step=error_step,
            error_reason=error_reason,
            failure_class=failure_class.value,
            is_retryable=is_retryable,
            customer_email=payment_entity.get("email"),
            customer_contact=payment_entity.get("contact"),
            webhook_event_id=event.id,
            failed_at=datetime.fromtimestamp(
                payment_entity.get("created_at", 0), tz=UTC
            ),
        )
        session.add(failure_record)
        await session.flush()

        logger.info(
            "Classified: payment=%s class=%s retryable=%s",
            payment_id, failure_class.value, is_retryable,
        )

        # ── Step 3b: Open (or find) the recovery case ─────────────────────
        # Everything downstream spends this case's budget and credits its
        # recovered amount. Opening it here rather than after the agent call
        # means a case exists even for failures we decide not to chase — the
        # audit trail has to show the ones we declined, not only the ones we
        # acted on.
        case = await open_case(
            session,
            risk_type="payment_failure",
            subject_ref=payment_id,
            amount_at_risk=failure_record.amount,
            currency=failure_record.currency,
            customer_id=customer_key(
                email=failure_record.customer_email,
                contact=failure_record.customer_contact,
            ),
            # A day is the natural batch for live traffic, so batch_summary()
            # answers "money recovered across a batch" without anyone having to
            # pass an id. A bulk replay can set its own by opening cases first.
            batch_id=datetime.now(UTC).strftime("%Y-%m-%d"),
        )

        # ── Step 4: Hard decline fast path ────────────────────────────────
        if failure_class.is_hard_decline:
            logger.info("Hard decline — abandoning without agent call: %s", payment_id)
            close_case(case, "abandoned", f"hard decline: {failure_class.value}")
            log_event(
                session,
                case,
                "closed",
                actor="deterministic",
                state="abandoned",
                reason=f"hard decline: {failure_class.value}",
                error_code=error_code,
            )
            abandon_key = f"abandon_{payment_id}"
            if await self._attempt_exists(abandon_key, session):
                logger.info("Abandon already recorded for %s — skipping", payment_id)
                await session.commit()
                return
            attempt = RetryAttempt(
                payment_failure_id=failure_record.id,
                payment_id=payment_id,
                idempotency_key=abandon_key,
                attempt_number=0,
                action_type="abandon",
                agent_reasoning=f"Hard decline: {failure_class.value}",
                agent_type="deterministic",
                guardrail_passed=True,
                result="skipped",
            )
            attempt.recovery_case_id = case.id  # not attach_attempt: spends no budget
            session.add(attempt)
            await session.commit()
            return

        # ── Step 4b: Stopping rule ────────────────────────────────────────
        # Ahead of the agent call, so an exhausted or opted-out case costs no
        # LLM tokens. The guardrail checks the same budget from the attempt
        # side; this checks it from the case side, which is the one that also
        # covers cases closed early by opt-out or expiry.
        stop = stop_reason(case, await self._get_ledger(case.customer_id, session))
        if stop is not None:
            logger.info("Case stopped, no further action: payment=%s reason=%s", payment_id, stop)
            # "deferred" and "stopped" are different facts and the audit has to
            # keep them apart: one case is waiting out an escalation gap, the
            # other is finished. Reading both as "we did nothing" is how a
            # bounded workflow gets mistaken for a broken one.
            log_event(
                session,
                case,
                _stop_event_type(stop),
                reason=stop,
                attempts_used=case.attempts_used,
                max_attempts=case.max_attempts,
            )
            await session.commit()
            return

        # ── Step 5: Build context ─────────────────────────────────────────
        context = await self._build_failure_context(failure_record, session)

        # ── Step 5b: Release the ledger lock before the agent call ────────
        # _get_ledger() took a FOR UPDATE row lock while the context was
        # built, and the transaction would otherwise stay open across the LLM
        # call — up to a minute with the timeout plus the transient retry —
        # serialising every webhook for this customer behind one inference
        # call and holding a database connection the whole time. Committing
        # here persists the failure record and the case (good in itself: the
        # audit trail exists even if the process dies mid-decision) and
        # releases the lock. The counts the context carries are re-read fresh
        # under the lock again below, right before the guardrail runs — so
        # the serialisation guarantee moves from "hold the lock across the
        # LLM" to "hold it from validation to commit", which is the span the
        # TOCTOU actually lives in.
        await session.commit()

        # ── Step 6: Agent decision ────────────────────────────────────────
        # agent_type must record who ACTUALLY decided, not who was configured
        # to — see _decide_action, which owns that distinction for every
        # decision path (live webhook, chaser sweep, fired retry).
        action, agent_type = await self._decide_action(context, payment_id)

        # ── Step 6b: Resolve the target rail ──────────────────────────────
        # Before the guardrail, so Layer 4 validates the action that actually
        # executes rather than the one the agent first proposed. Every decision
        # path funnels through here — LLM, LLM-fallback and XGBoost — so this is
        # the one place it has to hold.
        if action.action == "switch_rail":
            resolved = resolve_target_rail(
                context.method, action.rail, context.failure_class,
                bank=context.bank,
                downtime=await downtime.current(),
            )
            if resolved != action.rail:
                # Logged, not silent: the agent's original choice is otherwise
                # unrecoverable from the ledger, which stores only the rail used.
                logger.info(
                    "Rail override: payment=%s agent_chose=%s using=%s (just failed on %s)",
                    payment_id,
                    action.rail,
                    resolved,
                    context.method,
                )
                # None is possible in principle and correct to write: the
                # guardrail's schema check then rejects the switch outright,
                # which is the fail-closed answer when there is no rail to move to.
                action.rail = resolved

        # ── Step 6c: Clamp a deferred retry out of the blackout window ────
        # The guardrail validates the CURRENT hour, so a deferral approved at
        # 22:30 lands unvalidated at 23:05 — inside the 23–7 IST window — and
        # the scheduler's fire-time re-validation rejects it. The attempt slot
        # was spent either way: without this clamp every "wait 30 minutes"
        # decided near the boundary burns budget on a retry that could never
        # fire. Forward-only; see clamp_retry_at_out_of_blackout.
        if action.action == "retry_at" and action.retry_at is not None:
            action.retry_at = clamp_retry_at_out_of_blackout(action.retry_at)

        # ── Step 7: Idempotency key ───────────────────────────────────────
        # Deterministic by construction: (payment, attempt number) fully
        # determines the key. A random component would make every key unique
        # and therefore make the UNIQUE constraint on retry_attempts.
        # idempotency_key unfireable — an idempotency key you cannot collide
        # is not an idempotency key.
        attempt_count = await self._get_attempt_count(payment_id, session)
        idem_key = f"retry_{payment_id}_{attempt_count}"

        # Check-before-execute. This is where the double-charge is actually
        # prevented: razorpay-python has no idempotency header, so the
        # guarantee has to be enforced at our boundary, not theirs.
        if await self._attempt_exists(idem_key, session):
            logger.info("Attempt %s already executed — skipping (idempotent replay)", idem_key)
            await session.commit()
            return

        # ── Step 7b: Re-validate under the lock ───────────────────────────
        # The lock was released for the agent call, and the world moved in
        # that window (see _revalidate_under_lock for the full reasoning and
        # the accidental-guarantee history). The lock is now held through
        # execution and the ledger bump — the span that has to be serialised.
        context, stop = await self._revalidate_under_lock(case, context, session)
        if stop is not None:
            logger.info(
                "Case stopped while the agent was deciding: payment=%s reason=%s",
                payment_id, stop,
            )
            log_event(
                session,
                case,
                _stop_event_type(stop),
                reason=stop,
                attempts_used=case.attempts_used,
                max_attempts=case.max_attempts,
            )
            await session.commit()
            return

        # ── Step 8: Guardrail validation ──────────────────────────────────
        guardrail_result = self._guardrail.validate(
            action, context, idem_key, attempt_count
        )

        # ── Step 9: Create RetryAttempt record ────────────────────────────
        attempt = self._make_attempt(
            action=action,
            agent_type=agent_type,
            guardrail_result=guardrail_result,
            idem_key=idem_key,
            attempt_number=attempt_count + 1,
            payment_failure_id=failure_record.id,
            payment_id=payment_id,
        )
        # Binds the attempt to the case and spends one unit of its budget.
        # Called for rejected attempts too: a rejection is a decision the case
        # made, and not counting it lets a payment that keeps tripping the
        # guardrail re-enter the agent forever.
        attach_attempt(case, attempt)

        if guardrail_result.passed:
            # ── Step 10: Defer, or execute now ────────────────────────────
            # `retry_at` is the whole point of this branch. It used to fall
            # through to the executor, which maps retry_at onto the same
            # _create_payment_link as retry_now — so "retry in 4 hours" created
            # the link immediately and `scheduled_at` was decorative. The row is
            # now parked as "scheduled" and src/scheduler.py fires it when the
            # time comes, re-running the guardrail at that point: the blackout
            # window, the consent window and the attempt budget can all have
            # changed in four hours, and the decision to wait is exactly the
            # decision that outlives its own validation.
            if action.action == "retry_at" and action.retry_at is not None:
                attempt.result = "scheduled"
                session.add(attempt)
                log_event(
                    session,
                    case,
                    "deferred",
                    actor=agent_type,
                    action="retry_at",
                    scheduled_at=action.retry_at.isoformat(),
                    reason=action.reason,
                )
                await self._update_ledger_and_commit(context.customer_id, action, session)
                logger.info(
                    "Retry scheduled: payment=%s at=%s key=%s",
                    payment_id, action.retry_at.isoformat(), idem_key,
                )
                return

            await self._execute_and_record(
                attempt=attempt,
                case=case,
                failure_record=failure_record,
                action=action,
                idem_key=idem_key,
                actor=agent_type,
                session=session,
            )
        else:
            attempt.result = "rejected"
            logger.warning(
                "Guardrail rejected: payment=%s reasons=%s",
                payment_id, guardrail_result.rejection_reasons,
            )

        # No-op for the executed path (already added and committed above); this
        # is what persists the abandon / guardrail-rejected rows, which never
        # touch Razorpay and so need no write-ahead.
        session.add(attempt)
        if guardrail_result.passed:
            await self._update_ledger_and_commit(context.customer_id, action, session)
        else:
            # A rejected action contacted nobody. Counting it against the
            # customer's 24h tallies burned real-world quota on a contact that
            # never happened — the ledger exists to bound actual outreach, not
            # decisions the guardrail vetoed.
            await session.commit()
        logger.info(
            "Pipeline complete: payment=%s action=%s guardrail=%s result=%s",
            payment_id, action.action, guardrail_result.passed, attempt.result,
        )

    def _make_attempt(
        self,
        *,
        action: RetryAction,
        agent_type: str,
        guardrail_result: Any,
        idem_key: str,
        attempt_number: int,
        payment_failure_id: Any = None,
        payment_id: str | None = None,
    ) -> RetryAttempt:
        """
        The RetryAttempt row both decision paths record. Deterministic-keyed
        and guardrail-stamped, with the write-ahead ordering owned by the
        CALLERS (see _execute_and_record / _execute_case_and_record — the row
        is committed before Razorpay is called, never after).

        scheduled_at is persisted UTC, always. The agent hands back an
        IST-aware time and Postgres would normalise it, but SQLite's DATETIME
        drops the zone and stores the wall clock it was given — so an IST
        value read back naive is 5h30m adrift, which is enough to move a
        retry into the blackout window it was just clamped out of.
        """
        return RetryAttempt(
            payment_failure_id=payment_failure_id,
            payment_id=payment_id,
            idempotency_key=idem_key,
            attempt_number=attempt_number,
            action_type=action.action,
            target_rail=action.rail,
            scheduled_at=(
                action.retry_at.astimezone(UTC) if action.retry_at else None
            ),
            agent_reasoning=action.reason,
            agent_type=agent_type,
            agent_confidence=action.confidence,
            guardrail_passed=guardrail_result.passed,
            guardrail_rejection_reason=(
                "; ".join(guardrail_result.rejection_reasons)
                if guardrail_result.rejection_reasons else None
            ),
        )

    async def _update_ledger_and_commit(
        self, customer_id: str | None, action: RetryAction, session: AsyncSession
    ) -> None:
        """Step 12 — bump the per-customer rate-limit tally, then commit."""
        if customer_id:
            await self._update_retry_ledger(customer_id, action, session)
        await session.commit()

    async def _revalidate_under_lock(
        self,
        case: RecoveryCase,
        context: FailureContext,
        session: AsyncSession,
    ) -> tuple[FailureContext, str | None]:
        """
        Steps 7b / chase-recheck — the world moved while the agent decided,
        so re-read the case and the ledger under the lock, patch the
        context's counts, and re-run the stopping rule.

        A capture may have recovered the case, an opt-out may have closed
        it, other webhooks may have spent the customer's tally. The lock
        (released for the agent call, deliberately — see step 5b) is taken
        again here and stays held through execution and the ledger bump,
        which is the span that actually has to be serialised.

        Returns (possibly-updated context, stop reason or None). The caller
        decides what a stop means on its rail and owns the commit.
        """
        await session.refresh(case)
        fresh_ledger = None
        if context.customer_id:
            fresh_ledger = await self._get_ledger(context.customer_id, session)
            if fresh_ledger is not None:
                retry_count, nudge_count = self._effective_counts(
                    fresh_ledger, datetime.now(UTC)
                )
                context = context.model_copy(
                    update={
                        "retry_count_24h": retry_count,
                        "nudge_count_24h": nudge_count,
                    }
                )
        return context, stop_reason(case, fresh_ledger)

    # ── Chaser-driven risk types ──────────────────────────────────────────
    # A card decline announces itself through a webhook; an abandoned cart, a
    # halted subscription, an overdue invoice and a failed mandate debit do
    # not. Those are pushed to /risks, opened as cases with a next_action_at,
    # and chased by chase_case() — which runs the SAME pipeline as the payment
    # rail (agent → guardrail → write-ahead execution → attribution) bounded
    # by the per-type policy in src/chasers/policy.py. One pipeline, five
    # doors in.

    async def _latest_risk_event(
        self, case: RecoveryCase, session: AsyncSession
    ) -> RiskEvent | None:
        """The newest risk event that opened/fed this case, for its meta and
        the customer's email/contact (needed to mint a link days later)."""
        result = await session.execute(
            select(RiskEvent)
            .where(
                RiskEvent.risk_type == case.risk_type,
                RiskEvent.reference_id == case.subject_ref,
            )
            .order_by(RiskEvent.received_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _build_case_context(
        self,
        case: RecoveryCase,
        policy: RiskPolicy | None,
        session: AsyncSession,
        *,
        now: datetime | None = None,
        meta: dict[str, Any] | None = None,
        promise_score: PromiseScore | None = None,
    ) -> FailureContext:
        """
        Assemble a FailureContext from a case with no PaymentFailure behind it.

        The case's opened_at is the consent-window anchor: for a risk type we
        only learn about when the merchant tells us, "when may we stop chasing"
        runs from when we started, not from when the money was due.
        """
        now = now or datetime.now(UTC)

        retry_count, nudge_count = 0, 0
        if case.customer_id:
            ledger_row = await self._get_ledger(case.customer_id, session)
            if ledger_row:
                retry_count, nudge_count = self._effective_counts(ledger_row, now)

        prev_attempts = await session.execute(
            select(RetryAttempt.result)
            .where(RetryAttempt.recovery_case_id == case.id)
            .order_by(RetryAttempt.created_at.desc())
            .limit(5)
        )
        previous_outcomes = [r for (r,) in prev_attempts.all() if r]

        # Only consulted by the mandate pre-debit notification rule
        # (guardrail rule 12) — cheap to compute for every case, but only
        # ever non-None for risk_type=mandate_failure.
        last_notification = await session.execute(
            select(RetryAttempt.executed_at)
            .where(
                RetryAttempt.recovery_case_id == case.id,
                RetryAttempt.action_type == "nudge_customer",
                RetryAttempt.result == "success",
            )
            .order_by(RetryAttempt.executed_at.desc())
            .limit(1)
        )
        last_notification_sent_at = last_notification.scalar_one_or_none()

        local_now = now.astimezone(IST)
        opened_at = _aware(case.opened_at) or now

        method = "unknown"
        if isinstance(meta, dict):
            raw_method = meta.get("method")
            if isinstance(raw_method, str) and raw_method.strip():
                method = raw_method.strip()[:50]

        return FailureContext(
            risk_type=case.risk_type,
            payment_id=case.subject_ref,
            order_id=None,
            # policy is None for risk_type="payment_failure": that rail is
            # webhook-driven and its bounds live in config, not RISK_POLICIES
            # (see chasers/policy.py). It still has a failure class of its own
            # and the global consent window, so a promise made on a payment
            # case can be reminded and collected like any other. Before this,
            # every promise-side caller returned "no policy" and did nothing
            # for the engine's single biggest rail.
            failure_class=policy.failure_class if policy else "payment_failure",
            error_code=(
                policy.failure_class.upper() if policy else "PAYMENT_FAILURE"
            ),
            amount=case.amount_at_risk,
            currency=case.currency,
            method=method,
            customer_id=case.customer_id,
            retry_count_24h=retry_count,
            nudge_count_24h=nudge_count,
            previous_retry_outcomes=previous_outcomes,
            failed_at=opened_at,
            current_time=now,
            hour_of_day=local_now.hour,
            day_of_week=local_now.weekday(),
            consent_window_hours=(
                policy.consent_window_hours
                if policy
                else get_settings().consent_window_hours
            ),
            risk_meta=meta,
            is_retryable=True,
            original_failure_id=None,
            last_notification_sent_at=last_notification_sent_at,
            promise_kept=promise_score.kept if promise_score else 0,
            promise_broken=promise_score.broken if promise_score else 0,
            promise_pending=promise_score.pending if promise_score else 0,
        )

    async def chase_case(
        self,
        case: RecoveryCase,
        session: AsyncSession,
        *,
        actor: str = "chaser",
        now: datetime | None = None,
    ) -> None:
        """
        Run ONE bounded chase step for a chaser-driven case.

        Called by the risk-event background task (first touch) and by the
        scheduler's chase_due_cases sweep (every later rung). Mirrors the
        payment rail's discipline exactly: stopping rule before the agent,
        lock released across the LLM call, re-validation under the lock,
        guardrail, write-ahead execution, ledger bump.
        """
        now = now or datetime.now(UTC)
        policy = policy_for(case.risk_type)
        if policy is None:
            # Not a chaser-driven risk type (payment_failure is webhook-driven;
            # unknown strings fail closed). The due-case sweep reports these,
            # never chases them.
            return

        # Work off this session's own copy of the case. Callers may hand in a
        # row loaded elsewhere, and the case can also have moved between their
        # read and this call (closed by opt-out, recovered by a capture); the
        # re-read is what makes the stopping rule below see the truth.
        fresh_case = await session.get(RecoveryCase, case.id)
        if fresh_case is None:  # pragma: no cover — vanished mid-chase
            return
        case = fresh_case

        # Money-safety first, same rule as the customer page: never chase
        # while an earlier attempt may still be alive. A pending write-ahead
        # row means Razorpay might already have a live link for this case.
        latest = await session.execute(
            select(RetryAttempt)
            .where(RetryAttempt.recovery_case_id == case.id)
            .order_by(RetryAttempt.created_at.desc())
            .limit(1)
        )
        latest_attempt = latest.scalar_one_or_none()
        if latest_attempt is not None and latest_attempt.result == "pending":
            case.next_action_at = now + timedelta(minutes=30)
            log_event(
                session, case, "deferred", actor=actor,
                reason="earlier attempt still pending — not chasing on top of it",
            )
            await session.commit()
            return

        ledger = await self._get_ledger(case.customer_id, session)
        stop = stop_reason(case, ledger)
        if stop is not None:
            log_event(
                session, case,
                _stop_event_type(stop),
                actor=actor, reason=stop,
                attempts_used=case.attempts_used, max_attempts=case.max_attempts,
            )
            await session.commit()
            return

        # The consent window is a hard edge, not a guardrail afterthought:
        # nothing else ever expires an open case, so without this the sweep
        # keeps knocking past the window, burning budget slots on attempts the
        # guardrail only rejects — noisy, wasteful, and a case that can never
        # be chased again stays "open" in every count. Close it as expired
        # BEFORE spending an agent call or an attempt slot on it.
        opened = _aware(case.opened_at) or now
        window_end = opened + timedelta(hours=policy.consent_window_hours)
        if now > window_end:
            close_case(
                case, "expired",
                f"consent window closed ({policy.consent_window_hours}h "
                f"from {opened.isoformat()})",
            )
            log_event(
                session, case, "closed", actor=actor,
                state="expired", reason=case.close_reason,
            )
            await session.commit()
            logger.info(
                "Case expired past its consent window: case=%s risk=%s",
                case.id, case.risk_type,
            )
            return

        # An open dispute freezes this case. The customer said the money is
        # not owed; chasing them anyway is how a billing disagreement becomes
        # a legal one, and the recovery page's dispute button promises exactly
        # this freeze ("the freeze is total"). Until now that promise was kept
        # in ONE place — the AR statement composer skipped disputed cases —
        # while this pipeline, the only path that actually contacts anybody,
        # never looked at the table at all.
        #
        # Checked AFTER the consent-window expiry above, so a disputed case
        # still ages out and closes on its own clock; a freeze that also
        # suspended expiry would leave disputed cases open forever. And
        # deferred rather than closed, because resolve_dispute has to be able
        # to hand the case back to the chase — a terminal state cannot.
        from src.receivables.models import CaseDispute

        dispute = await session.scalar(
            select(CaseDispute).where(
                CaseDispute.case_id == case.id,
                CaseDispute.status == "open",
            )
        )
        if dispute is not None:
            case.next_action_at = now + timedelta(hours=policy.re_chase_hours)
            log_event(
                session, case, "deferred", actor=actor,
                reason="open dispute — chase frozen until a human resolves it",
                dispute_id=str(dispute.id),
                next_action_at=case.next_action_at.isoformat(),
            )
            await session.commit()
            logger.info(
                "Chase frozen by an open dispute: case=%s dispute=%s",
                case.id, dispute.id,
            )
            return

        # Invoices are a B2B contact: a person at a desk, Mon–Fri 09:30–18:30
        # IST. That window used to live only in the AR consolidation sweep,
        # which meant every invoice case reachable through THIS path — an
        # unlinked invoice, or any invoice once the sweep order slipped — was
        # chaseable on a Sunday morning. Same defer-don't-burn shape as the
        # blackout below: move to the window's edge, spend no budget slot.
        if case.risk_type == "invoice_overdue":
            if not is_b2b_contact_time(now):
                case.next_action_at = next_b2b_window(now)
                log_event(
                    session, case, "deferred", actor=actor,
                    reason="outside the B2B contact window — deferred to its edge",
                    next_action_at=case.next_action_at.isoformat(),
                )
                await session.commit()
                logger.info(
                    "Chase deferred past the B2B window: case=%s until=%s",
                    case.id, case.next_action_at.isoformat(),
                )
                return

        # The blackout is a timing rule, not a case verdict — and the chaser
        # picks its own moment, unlike the payment rail whose events arrive
        # when they arrive. Walking into the quiet hours and letting the
        # guardrail reject the attempt would burn a budget slot on a wall we
        # knew was there (a cart gone cold at 23:30 could lose both its slots
        # to blackout rejections without one message going out). Defer to the
        # window's edge instead: no agent call, no attempt row, budget intact.
        # The guardrail still validates at execution time — this only stops
        # the sweep from scheduling into a closed window.
        if is_in_blackout(now.astimezone(IST).hour):
            case.next_action_at = clamp_retry_at_out_of_blackout(now)
            log_event(
                session, case, "deferred", actor=actor,
                reason="IST blackout window — deferred to its end",
                next_action_at=case.next_action_at.isoformat(),
            )
            await session.commit()
            logger.info(
                "Chase deferred past IST blackout: case=%s until=%s",
                case.id, case.next_action_at.isoformat(),
            )
            return

        event = await self._latest_risk_event(case, session)
        meta = event.meta if event is not None else None
        customer_email = event.customer_email if event is not None else None
        customer_contact = event.customer_contact if event is not None else None
        # The merchant's incentive, if any: only from the SECOND touch on.
        # attempts_used is 0 on the first touch — the research is clear that
        # an incentive on touch 1 trains discount-waiting, so touch 1 never
        # carries one, whatever the event said.
        offer_id = (
            event.offer_id
            if event is not None
            and event.offer_id
            and case.attempts_used >= 1
            else None
        )

        # The kept-rate signal, read into the context: the one thing the
        # promises table exists to feed into the next contact decision.
        # Raw counts, never a derived rate — the prompt block stays
        # auditable against the rows.
        promise_score = await customer_promise_score(session, case.customer_id)

        context = await self._build_case_context(
            case, policy, session, now=now, meta=meta,
            promise_score=promise_score,
        )

        # Release the ledger lock before the agent call — same reasoning as
        # step 5b on the payment path. The counts are re-read fresh under the
        # lock again below, right before the guardrail runs.
        await session.commit()

        action, agent_type = await self._decide_action(context, case.subject_ref)

        # Resolve the target rail before the guardrail, same as the payment
        # path: the gate validates the action that actually executes.
        if action.action == "switch_rail":
            resolved = resolve_target_rail(
                context.method, action.rail, context.failure_class,
                bank=context.bank,
                downtime=await downtime.current(),
            )
            if resolved != action.rail:
                logger.info(
                    "Rail override: case=%s agent_chose=%s using=%s",
                    case.id, action.rail, resolved,
                )
                action.rail = resolved

        # RBI e-mandate framework: a collection attempt needs a pre-debit
        # notice at least 24h old. Guardrail rule 12 rejects one without it —
        # and a rejection still spends an attempt slot, so a mandate case
        # (budget: 3) could burn its way to "exhausted" without ever sending
        # the notice the rule is asking for. Both decision paths walk into it:
        # the prompt tells the LLM "retry_at next day is usually right" and
        # the XGBoost heuristic hardcodes the same, and neither is told about
        # last_notification_sent_at at all.
        #
        # So the prerequisite is supplied deterministically rather than hoped
        # for. A nudge IS the pre-debit notification (see rule 12's own
        # docstring), which makes this a downgrade to the action that had to
        # happen first, not a substitution of a different intent. Same idiom
        # as the rail override above: the gate validates what executes, and
        # the agent's original choice is logged rather than lost.
        if case.risk_type == "mandate_failure" and action.action in (
            "retry_now", "retry_at",
        ):
            notice_ok, _ = self._guardrail._rules.check_mandate_predebit_notification(
                case.risk_type, "retry_now",
                context.last_notification_sent_at, now,
            )
            if not notice_ok:
                logger.info(
                    "Mandate pre-debit notice missing or stale: case=%s "
                    "agent_chose=%s sending the notice instead of collecting",
                    case.id, action.action,
                )
                action.action = "nudge_customer"
                action.retry_at = None

        if action.action == "retry_at" and action.retry_at is not None:
            action.retry_at = clamp_retry_at_out_of_blackout(action.retry_at)

        # The policy's rail preference, applied when the agent expressed none.
        # Subscriptions and mandates fail on card OTPs the way one-off
        # payments do, which is why their policy names UPI — but that
        # preference only ever reached the recovery page and the console, and
        # the engine's OWN link minted generic, so the chase contradicted the
        # page it pointed at. Only fills a gap: an explicit agent choice has
        # context this default does not and is left alone. Set before the
        # guardrail so the gate validates the action that actually executes.
        if action.rail is None and policy.recommended_rail is not None:
            action.rail = policy.recommended_rail

        # Deterministic idempotency key: (case, attempts_used) fully determines
        # it, so two workers chasing the same case collide onto the same key
        # and the UNIQUE constraint hands the attempt to exactly one of them.
        idem_key = f"chase_{case.risk_type}_{case.subject_ref}_{case.attempts_used}"
        if await self._attempt_exists(idem_key, session):
            logger.info("Chase attempt %s already recorded — skipping", idem_key)
            await session.commit()
            return

        # Re-validate under the lock: the world moved while the agent decided
        # (see _revalidate_under_lock — same reasoning as the payment path's
        # step 7b).
        context, stop = await self._revalidate_under_lock(case, context, session)
        if stop is not None:
            log_event(
                session, case,
                _stop_event_type(stop),
                actor=actor, reason=stop,
                attempts_used=case.attempts_used, max_attempts=case.max_attempts,
            )
            await session.commit()
            return

        guardrail_result = self._guardrail.validate(
            action, context, idem_key, case.attempts_used
        )

        attempt = self._make_attempt(
            action=action,
            agent_type=agent_type,
            guardrail_result=guardrail_result,
            idem_key=idem_key,
            attempt_number=case.attempts_used + 1,
        )
        attach_attempt(case, attempt)

        if guardrail_result.passed:
            if action.action == "retry_at" and action.retry_at is not None:
                attempt.result = "scheduled"
                session.add(attempt)
                log_event(
                    session, case, "deferred", actor=agent_type,
                    action="retry_at",
                    scheduled_at=action.retry_at.isoformat(),
                    reason=action.reason,
                )
                # The case's next rung is when the parked retry fires. The
                # fire sweep re-validates at that moment, same as the payment
                # rail's deferred retries.
                case.next_action_at = action.retry_at.astimezone(UTC)
                await self._update_ledger_and_commit(context.customer_id, action, session)
                logger.info(
                    "Chase retry scheduled: case=%s at=%s key=%s",
                    case.id, action.retry_at.isoformat(), idem_key,
                )
                return

            await self._execute_case_and_record(
                attempt=attempt,
                case=case,
                policy=policy,
                action=action,
                idem_key=idem_key,
                actor=agent_type,
                session=session,
                customer_email=customer_email,
                customer_contact=customer_contact,
                cart_summary=cart_summary_from_meta(meta),
                offer_id=offer_id,
            )
        else:
            attempt.result = "rejected"
            logger.warning(
                "Guardrail rejected chase: case=%s reasons=%s",
                case.id, guardrail_result.rejection_reasons,
            )

        session.add(attempt)

        # Where does the ladder go next? attach_attempt already pushed
        # next_action_at out for a nudge (escalation backoff); every other
        # outcome needs a floor so the due-case sweep does not re-chase this
        # case on the very next tick. And if the budget is spent, the case
        # CLOSES — leaving it open with a stale next_action_at would have the
        # sweep knocking on a finished case forever.
        if case.state == "open":
            if case.attempts_used >= case.max_attempts:
                close_case(
                    case, "exhausted",
                    f"attempt budget spent ({case.attempts_used}/{case.max_attempts})",
                )
                log_event(
                    session, case, "closed", actor=actor,
                    state="exhausted", reason=case.close_reason,
                )
            else:
                next_at = _aware(case.next_action_at)
                if next_at is None or next_at <= now:
                    case.next_action_at = now + timedelta(hours=policy.re_chase_hours)

        if guardrail_result.passed:
            await self._update_ledger_and_commit(context.customer_id, action, session)
        else:
            await session.commit()
        logger.info(
            "Chase complete: case=%s risk=%s action=%s guardrail=%s result=%s",
            case.id, case.risk_type, action.action,
            guardrail_result.passed, attempt.result,
        )

    def _record_execution_outcome(
        self,
        session: AsyncSession,
        case: RecoveryCase,
        attempt: RetryAttempt,
        action: RetryAction,
        exec_result: dict[str, Any],
        *,
        actor: str,
        nudge_message: str | None,
    ) -> None:
        """
        Resolve a write-ahead row from the executor's result — shared by both
        execute paths, one copy on purpose. The attribution join key
        (external_ref = the Payment Link id) is the only place this id and
        the eventual capture appear together; not persisting it is what made
        recovered revenue unattributable.

        The channel that actually reached them: a nudge notifies by SMS and
        email both; only the first is recorded, which is enough for
        per-channel limits and not enough to reconstruct a two-channel send —
        split this into its own contacts table if that becomes the question.
        """
        attempt.executed_at = datetime.now(UTC)
        attempt.result = "success" if exec_result.get("success") else "failed"
        attempt.result_details = exec_result
        link_id = exec_result.get("payment_link_id")
        if link_id:
            attempt.external_ref = str(link_id)
        if nudge_message:
            attempt.nudge_sent = exec_result.get("nudge_sent", False)
        channels = exec_result.get("channels") or []
        attempt.channel = channels[0] if channels else "payment_link"

        if attempt.result == "success" and action.action == "nudge_customer":
            log_event(
                session, case, "escalated", actor=actor,
                level=case.escalation_level, channel=attempt.channel,
                next_action_at=(
                    case.next_action_at.isoformat() if case.next_action_at else None
                ),
            )
        elif attempt.result == "success":
            log_event(
                session, case, "contacted", actor=actor,
                action=action.action, channel=attempt.channel,
                external_ref=attempt.external_ref,
            )

    async def _execute_case_and_record(
        self,
        *,
        attempt: RetryAttempt,
        case: RecoveryCase,
        # None on the payment rail — see _build_case_context. The two reads
        # below fall back to the customer's own words for a failed payment.
        policy: RiskPolicy | None,
        action: RetryAction,
        idem_key: str,
        actor: str,
        session: AsyncSession,
        customer_email: str | None,
        customer_contact: str | None,
        cart_summary: str | None = None,
        offer_id: str | None = None,
    ) -> None:
        """
        Case-driven twin of _execute_and_record: generate any nudge, write the
        attempt ahead, call Razorpay, record what happened. The write-ahead
        ordering is the same correctness property — committed BEFORE the
        Razorpay call, never after — and is not reordered here.
        """
        nudge_message: str | None = None

        # Every case action that is not abandon delivers a link, and a link
        # without a message is a bare demand for money — so the message is
        # generated for all of them, not just nudge_customer. (The payment
        # rail generates only for nudge/switch because its retry actions
        # re-present a charge silently; there is no silent path here.)
        if action.action != "abandon":
            try:
                page = recovery_link.url_for(case.id)
                next_step = (
                    f"Check your {policy.subject_noun if policy else 'payment'} "
                    f"and pay securely here: {page}"
                    if page
                    else "Please try again using a different payment method."
                )
                nudge_message = await self._nudge_gen.generate(
                    failure_class=policy.failure_class if policy else "payment_failure",
                    amount=case.amount_at_risk,
                    method="unknown",
                    next_step=next_step,
                    customer_name=None,
                    merchant_name=get_settings().merchant_name or "the merchant",
                    # The risk type selects honest situation wording — three of
                    # the four never attempted a payment, and the prompt must
                    # not open with "your payment failed" for those.
                    risk_type=case.risk_type,
                    # Carts only: naming the items is the personalization the
                    # research says lifts the message; None elsewhere keeps the
                    # old wording byte-for-byte.
                    cart_summary=cart_summary if case.risk_type == "checkout_abandonment" else None,
                )
                attempt.nudge_message = nudge_message
            except Exception:
                logger.warning("Nudge generation failed — proceeding without nudge")

        if action.action == "abandon":
            attempt.result = "skipped"
            return

        # Write-ahead intent log — committed BEFORE the Razorpay call, exactly
        # as on the payment rail. See _execute_and_record for why this order
        # is a money-safety property, not a style choice.
        attempt.result = "pending"
        attempt.result_details = {"phase": "write_ahead"}
        session.add(attempt)
        try:
            await session.commit()
        except IntegrityError:
            # Lost the idempotency race to another worker chasing the same
            # case. The UNIQUE constraint fires BEFORE Razorpay is called; the
            # winner's attempt is the attempt.
            await session.rollback()
            logger.info(
                "Idempotency race lost on %s — the other worker owns this attempt",
                idem_key,
            )
            return

        try:
            exec_result = await self._executor.execute_case_action(
                case=case,
                action_type=action.action,
                target_rail=action.rail,
                idempotency_key=idem_key,
                nudge_message=nudge_message,
                customer_email=customer_email,
                customer_contact=customer_contact,
                offer_id=offer_id,
            )
            self._record_execution_outcome(
                session, case, attempt, action, exec_result,
                actor=actor, nudge_message=nudge_message,
            )
            await self._queue_voice_call(
                session, case=case, attempt=attempt, customer_contact=customer_contact,
                action=action,
            )
        except Exception:
            logger.exception("Execution failed for case %s", case.id)
            attempt.result = "failed"
            attempt.result_details = {"error": "Execution exception"}

    async def _queue_voice_call(
        self,
        session: AsyncSession,
        *,
        case: RecoveryCase,
        attempt: RetryAttempt,
        customer_contact: str | None,
        action: RetryAction,
    ) -> None:
        """
        After a successful chase touch, queue a voice follow-up call.

        Opt-in (VOICE_CHASER_ENABLED, default off) because a phone call is
        the highest-friction contact the engine can make: it doubles the
        touches a chase makes on the customer, needs its own compliance
        posture (DoT/TCPB, AI disclosure), and must never appear silently
        because someone deployed with a new env var set. The queue row
        joins the SAME session (and thus the same commit) as the attempt
        outcome — write-ahead like every other intent the engine records.

        Only after a SUCCESSFUL nudge_customer — a call that follows a
        failed message is a second annoyance chasing a first failure, and
        the voice loop's own opt-out handling inherits this case's caps.
        """
        settings = get_settings()
        if not settings.voice_chaser_enabled:
            return
        if action.action != "nudge_customer" or attempt.result != "success":
            return
        if not customer_contact:
            return
        # One queued call per attempt: the attempt id is the natural key.
        existing = await session.execute(
            select(VoiceCallQueue.retry_attempt_id).where(
                VoiceCallQueue.retry_attempt_id == attempt.id
            )
        )
        if existing.scalar_one_or_none() is not None:
            return
        session.add(
            VoiceCallQueue(
                recovery_case_id=case.id,
                retry_attempt_id=attempt.id,
                customer_contact=customer_contact,
                risk_type=case.risk_type,
                amount_paise=case.amount_at_risk,
                state="queued",
            )
        )
        log_event(
            session, case, "contacted", actor="system",
            action="voice_call_queued",
            channel="voice",
            attempt_id=str(attempt.id),
        )

    async def send_promise_reminder(
        self,
        case: RecoveryCase,
        promise: PromiseToPay,
        session: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> str:
        """
        One pre-due reminder for a pending promise. Returns its outcome.

        The 48h-before reminder is the kept-rate research's single biggest
        lift, but a reminder is a CONTACT — it must pay the same toll every
        other contact pays or the promise feature becomes a free nagging
        lane the compliance rules never saw. So it rides the exact chase
        discipline: stopping rule, guardrail subset, write-ahead attempt,
        ledger bump. No agent call — the content is the promise itself
        (amount + date + link), not a fresh decision, and a mid-promise LLM
        invention is precisely what the silence invariant exists to prevent.

        The promise's silence still holds: the reminder never moves
        next_action_at (it is not an escalation rung), and reminded_at is
        stamped by the CALLER whatever the outcome, so "guardrail refused"
        cannot become "remind again next tick" — that would nag on a case
        the gate already said to leave alone.
        """
        now = now or datetime.now(UTC)
        # policy is None on the payment rail, and that used to return
        # "skipped_no_policy" — so a promise made on the customer recovery page
        # for a failed payment, the engine's single biggest source of promises,
        # never got its 48h reminder at all. _build_case_context now serves a
        # None policy from the payment rail's own config, so the reminder fires
        # for every risk type. This also gates collection: the reminder IS the
        # RBI pre-debit notice, so without it a promise-backed mandate on a
        # payment case could never lawfully be debited.
        policy = policy_for(case.risk_type)

        ledger = await self._get_ledger(case.customer_id, session)
        stop = stop_reason(case, ledger)
        # The one stop the reminder legitimately trips is "not due until
        # ..." — every other stop (terminal, budget, opt-out) means no
        # contact of any kind is allowed.
        if stop is not None and "not due until" not in stop:
            return f"skipped_stop:{stop}"

        action = RetryAction(
            action="nudge_customer",
            reason=(
                f"Pre-due reminder for promise {promise.id} "
                f"(₹{promise.amount_promised // 100} due "
                f"{promise.due_at.isoformat()})"
            ),
        )
        # Same deterministic-key construction as a chase: two workers
        # reminding one promise collide onto one attempt row.
        idem_key = (
            f"reminder_{case.risk_type}_{case.subject_ref}_{promise.id}"
        )
        if await self._attempt_exists(idem_key, session):
            return "already_sent"

        context = await self._build_case_context(
            case, policy, session, now=now,
            promise_score=await customer_promise_score(session, case.customer_id),
        )
        guardrail_result = self._guardrail.validate(
            action, context, idem_key, case.attempts_used
        )
        attempt = self._make_attempt(
            action=action,
            agent_type="promise_reminder",
            guardrail_result=guardrail_result,
            idem_key=idem_key,
            attempt_number=case.attempts_used + 1,
        )
        attach_attempt(case, attempt)
        session.add(attempt)

        if not guardrail_result.passed:
            attempt.result = "rejected"
            await session.commit()
            return "skipped_guardrail"

        event = await self._latest_risk_event(case, session)
        await self._execute_case_and_record(
            attempt=attempt,
            case=case,
            policy=policy,
            action=action,
            idem_key=idem_key,
            actor="promise_reminder",
            session=session,
            customer_email=event.customer_email if event else None,
            customer_contact=event.customer_contact if event else None,
        )
        await self._update_ledger_and_commit(context.customer_id, action, session)

        # attach_attempt pushed next_action_at out (escalation backoff for a
        # nudge) — but the PROMISE owns the silence, and the reminder must not
        # extend the case's quiet past the promise date nor pull it earlier.
        # The promise's due_at was the floor before the reminder ran; keep it.
        case_next = _aware(case.next_action_at) or now
        promise_due = _aware(promise.due_at) or now
        case.next_action_at = max(case_next, promise_due)
        await session.commit()
        return attempt.result or "sent"

    async def charge_promise_mandate(
        self,
        case: RecoveryCase,
        promise: PromiseToPay,
        session: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> str:
        """
        Debit the UPI Autopay mandate a customer authorised against a promise.

        The only path in this engine that takes money without the customer
        present, so it pays every toll the reminder pays and one more: the
        guardrail sees `is_mandate_debit=True` on the context, which arms the
        RBI pre-debit rule (rules.py:check_mandate_predebit_notification). That
        rule has existed and been tested since long before anything could reach
        it — it guarded an action that minted a Payment Link. This is the
        caller it was written for.

        No agent call, same reasoning as the reminder: the decision was made
        when the customer authorised a specific amount on a specific date. An
        LLM re-deciding it at debit time would be inventing consent.

        Returns an outcome string for the sweep to log. Never raises: a promise
        whose debit fails is a promise that stays pending and breaks on the
        normal clock, which is exactly what would have happened without a
        mandate at all.
        """
        now = now or datetime.now(UTC)
        policy = policy_for(case.risk_type)

        ledger = await self._get_ledger(case.customer_id, session)
        stop = stop_reason(case, ledger)
        # Same single tolerated stop as the reminder: the promise itself set
        # next_action_at to its due date, so "not due until" is this sweep's
        # own silence and not a refusal. Every other stop — terminal, budget
        # spent, opted out — means no collection.
        if stop is not None and "not due until" not in stop:
            return f"skipped_stop:{stop}"

        action = RetryAction(
            action="retry_now",
            reason=(
                f"Promise mandate debit for promise {promise.id} "
                f"(₹{promise.amount_promised // 100} due "
                f"{promise.due_at.isoformat()})"
            ),
        )
        # One promise, one debit, forever. The promise id is in the key rather
        # than an attempt counter precisely so a retry of this sweep cannot
        # mint a second charge against the same authorisation.
        idem_key = f"mandate_debit_{promise.id}"
        if await self._attempt_exists(idem_key, session):
            return "already_charged"

        context = await self._build_case_context(
            case, policy, session, now=now,
            promise_score=await customer_promise_score(session, case.customer_id),
        )
        context = context.model_copy(
            update={"is_mandate_debit": True, "amount": promise.amount_promised}
        )
        guardrail_result = self._guardrail.validate(
            action, context, idem_key, case.attempts_used
        )
        attempt = self._make_attempt(
            action=action,
            agent_type="promise_mandate",
            guardrail_result=guardrail_result,
            idem_key=idem_key,
            attempt_number=case.attempts_used + 1,
        )
        # NOT attach_attempt, and the difference matters. attach_attempt spends
        # an attempt slot and widens the escalation backoff — both correct for
        # a CONTACT, both wrong here. A debit sends the customer nothing; it
        # collects money they authorised for this exact date. Charging it
        # against the contact budget would mean a case that used its touches on
        # chasing then declines to collect the mandate those touches earned,
        # and would push next_action_at out as if we had just messaged someone.
        #
        # The bound still holds: stop_reason's budget check runs above, so a
        # case already out of attempts is not collected from. This only stops
        # the debit from CONSUMING the budget.
        attempt.recovery_case_id = case.id
        session.add(attempt)

        if not guardrail_result.passed:
            attempt.result = "rejected"
            await session.commit()
            logger.info(
                "Mandate debit refused by guardrail: promise=%s reasons=%s",
                promise.id, guardrail_result.rejection_reasons,
            )
            return "skipped_guardrail"

        # WRITE-AHEAD. Identical ordering and identical reason to
        # _execute_and_record: this row must be committed before the gateway
        # call, or a crash between the debit and the commit leaves money taken
        # with nothing in our database saying so — and the next sweep, seeing
        # no attempt, debits the customer a second time.
        attempt.result = "pending"
        attempt.result_details = {"phase": "write_ahead"}
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            logger.info("Mandate debit already claimed: promise=%s", promise.id)
            return "already_charged"

        event = await self._latest_risk_event(case, session)

        # Order first, and COMMITTED first. The order id is the only join key a
        # mandate capture carries, so recording it after the debit would mean a
        # timeout on the charge leaves an attempt with no order id — and if the
        # money did move, the capture arrives quoting an id nothing here has
        # ever seen. Same law as the write-ahead row above, one level down.
        try:
            order_id = await self._executor.create_mandate_order(
                amount_paise=promise.amount_promised, idempotency_key=idem_key
            )
        except Exception as exc:  # noqa: BLE001 — no money has moved yet
            logger.warning("Mandate order failed: promise=%s err=%s", promise.id, exc)
            attempt.result = "failed"
            attempt.result_details = {"phase": "order", "error": str(exc)[:500]}
            await session.commit()
            return "failed"

        attempt.external_ref = order_id
        attempt.result_details = {"phase": "charging", "order_id": order_id}
        await session.commit()

        try:
            result = await self._executor.charge_mandate(
                order_id=order_id,
                mandate_token=promise.mandate_token or "",
                gateway_customer_id=promise.mandate_customer_ref or "",
                amount_paise=promise.amount_promised,
                customer_email=event.customer_email if event else None,
                customer_contact=event.customer_contact if event else None,
                description=f"Payment for {policy.subject_noun if policy else 'your payment'}",
            )
        except Exception as exc:  # noqa: BLE001 — recorded, never raised at the sweep
            # AMBIGUOUS, and recorded as such. The charge may have reached
            # Razorpay before the error; external_ref is already saved, so if
            # it did, the capture still finds this case. It is never retried:
            # the idempotency key is UNIQUE and _attempt_exists sees this row,
            # so the failure mode is "collected once, recorded as failed",
            # never "charged twice".
            logger.warning("Mandate debit failed: promise=%s err=%s", promise.id, exc)
            attempt.result = "failed"
            attempt.result_details = {
                "phase": "charging",
                "order_id": order_id,
                "error": str(exc)[:500],
            }
            await session.commit()
            return "failed"

        attempt.result = "success"
        attempt.result_details = {
            "order_id": order_id,
            "payment_id": result.get("payment_id"),
        }
        # Read by expire_promises: the money has left the customer's account,
        # so this promise must not be called broken while the capture webhook
        # is still in flight.
        promise.mandate_status = "charged"
        promise.mandate_charged_at = now
        # Read by expire_promises: the money has left the customer's account,
        # so this promise must not be called broken while the capture webhook
        # is still in flight.
        promise.mandate_status = "charged"
        promise.mandate_charged_at = now
        log_event(
            session,
            case,
            "mandate_post_debit_confirmation",
            actor="promise_mandate",
            promise_id=str(promise.id),
            amount=promise.amount_promised,
            order_id=order_id,
        )
        await session.commit()
        logger.info("Mandate debited for promise %s: order=%s", promise.id, order_id)
        return "charged"

    async def process_risk_event(self, event: RiskEvent, session: AsyncSession) -> None:
        """
        Ingestion entry point for a merchant-pushed risk event.

        Opens (or finds) the case with the per-type policy's budget and first
        touch time, then — for types whose first action is immediate — runs
        the first chase step right away. Types with a first_action delay are
        left for the scheduler's chase_due_cases sweep to pick up when their
        next_action_at arrives.
        """
        policy = policy_for(event.risk_type)
        if policy is None:
            logger.warning("No chase policy for risk type %s — skipping", event.risk_type)
            return

        occurred = (
            event.occurred_at if event.occurred_at.tzinfo is not None
            else event.occurred_at.replace(tzinfo=UTC)
        )
        first_touch = occurred + timedelta(hours=policy.first_action_hours)

        case = await open_case(
            session,
            risk_type=event.risk_type,  # type: ignore[arg-type]
            subject_ref=event.reference_id,
            amount_at_risk=event.amount,
            currency=event.currency,
            # Same precedence as the payment rail (email → phone → merchant id),
            # so one person hit by both a card decline and an overdue invoice is
            # ONE customer with one contact budget and one opt-out.
            customer_id=customer_key(
                email=event.customer_email,
                contact=event.customer_contact,
                external_id=event.customer_id,
            ),
            batch_id=datetime.now(UTC).strftime("%Y-%m-%d"),
            max_attempts=policy.max_attempts,
            due_at=event.due_at,
            next_action_at=first_touch,
        )

        # Link the case to its AR account (B2B receivables layer). Explicit
        # merchant account_ref wins; else the account is derived from the
        # canonical customer key. A case with neither stays unlinked and
        # chases per-case, exactly as before the receivables layer existed.
        # Invoice cases only: the other chaser types are consumer-shaped
        # (a cart has no buyer organisation behind it).
        if case.account_id is None and event.risk_type == "invoice_overdue":
            from src.receivables.accounts import (
                get_or_create_account,
            )

            canonical = customer_key(
                email=event.customer_email,
                contact=event.customer_contact,
                external_id=event.customer_id,
            )
            if event.account_ref or canonical:
                account = await get_or_create_account(
                    session,
                    account_ref=(
                        f"ref:{event.account_ref}"
                        if event.account_ref
                        else f"derived:{canonical}"
                    ),
                    display_name=event.meta.get("account_name")
                    if isinstance(event.meta, dict)
                    else None,
                )
                case.account_id = account.id
                logger.info(
                    "Case %s linked to AR account %s", case.id, account.account_ref
                )

        await session.commit()

        # An account-linked case is NEVER chased inline, however overdue it is.
        #
        # invoice_overdue carries first_action_hours=0 ("already overdue when we
        # hear about it"), so this used to chase every invoice the moment it
        # arrived — one contact per invoice. A buyer pushing four overdue
        # invoices in one batch got four separate messages, which is the exact
        # harm src/receivables exists to prevent, and the consolidation sweep
        # never saw them: by the time it ran, chase_case had already pushed
        # every next_action_at forward.
        #
        # Leaving them for chase_due_accounts costs at most one tick (their
        # next_action_at is already in the past, so the very next sweep picks
        # them up) and buys the guarantee back: one carrier case per account,
        # every other invoice deferred, one statement listing all of them.
        # An invoice with no account_ref has nothing to consolidate with, so it
        # keeps the immediate path.
        if policy.first_action_hours <= 0 and case.account_id is None:
            await self.chase_case(case, session, actor="chaser")

    async def _execute_and_record(
        self,
        *,
        attempt: RetryAttempt,
        case: RecoveryCase,
        failure_record: PaymentFailure,
        action: RetryAction,
        idem_key: str,
        actor: str,
        session: AsyncSession,
    ) -> None:
        """
        Generate any nudge, write the attempt ahead, call Razorpay, record what
        happened.

        Shared by the live webhook path and by src/scheduler.py firing a
        previously deferred `retry_at`. One copy on purpose: this is the block
        that moves money, and two copies of it would drift — the write-ahead
        ordering below is a correctness property, not a style choice, and it has
        to hold on both paths.
        """
        payment_id = failure_record.payment_id
        nudge_message: str | None = None

        if action.action in ("nudge_customer", "switch_rail"):
            try:
                # Point them at the recovery page rather than straight at a
                # payment link. A bare link asks for money again and explains
                # nothing — the page answers "has my money already gone" first,
                # and refuses to offer payment at all while an earlier attempt
                # is still confirming.
                #
                # Returns None when RECOVERY_LINK_SECRET or PUBLIC_BASE_URL is
                # unset, and the wording then falls back to exactly what it said
                # before. Configuring the page is what turns this on; nothing
                # about the existing flow changes until it is.
                page = recovery_link.url_for(case.id)
                next_step = (
                    f"Check your payment and pay securely here: {page}"
                    if page
                    else "Please try again using a different payment method."
                )
                nudge_message = await self._nudge_gen.generate(
                    failure_class=failure_record.failure_class,
                    amount=failure_record.amount,
                    method=failure_record.method,
                    next_step=next_step,
                    customer_name=None,
                    # The merchant's name is the trust anchor in an SMS that
                    # asks for money — an unnamed link reads as phishing.
                    # Empty setting falls back to the neutral phrase rather
                    # than blocking the send.
                    merchant_name=get_settings().merchant_name or "the merchant",
                )
                attempt.nudge_message = nudge_message
            except Exception:
                logger.warning("Nudge generation failed — proceeding without nudge")

        if action.action == "abandon":
            attempt.result = "skipped"
            return

        # Write-ahead intent log. This row MUST be committed BEFORE the Razorpay
        # call, not after it. Recording the attempt afterwards leaves a window
        # where money has moved and nothing in our database says so: crash
        # between the API call and the commit and the attempt vanishes, the
        # count-derived idem_key stays free, and the next payment.failed for
        # this payment reuses the same slot and charges the customer twice.
        #
        # Committing first makes the failure mode a recorded unknown instead of
        # a silent one — the row survives as "pending", it occupies its attempt
        # slot, and it counts against max_retries_per_payment so a crash-looping
        # payment stops rather than being hammered. Resolving a pending row to
        # success/failed is reconciliation's job, not this path's.
        #
        # The phase marker is the boundary the stale sweep reads (see
        # scheduler.reconcile_stale_attempts): a row marked phase=write_ahead
        # may or may not have reached Razorpay, so it resolves fail-closed. A
        # row still carrying only the scheduler's claim marker crashed BEFORE
        # the API call was even attempted — that one is safe to re-park.
        attempt.result = "pending"
        attempt.result_details = {"phase": "write_ahead"}
        session.add(attempt)
        try:
            await session.commit()
        except IntegrityError:
            # Lost the race on retry_attempts.idempotency_key. Two webhooks for
            # one payment arriving together both counted the same number of
            # existing attempts, both built the same key, and both passed the
            # check-before-execute — a TOCTOU the pre-check cannot close on its
            # own, because the gap between the count and the insert is where the
            # other transaction commits.
            #
            # The UNIQUE constraint is what actually prevents the double charge,
            # and it fires here, BEFORE Razorpay is called. Catching it turns
            # what was an unhandled exception (logged as a generic background
            # failure, with the event left looking dropped) into the same clean
            # skip the pre-check produces. The winner's attempt is the attempt.
            await session.rollback()
            logger.info(
                "Idempotency race lost on %s — the other worker owns this attempt",
                idem_key,
            )
            return

        try:
            exec_result = await self._executor.execute_retry(
                payment_failure=failure_record,
                action_type=action.action,
                target_rail=action.rail,
                idempotency_key=idem_key,
                nudge_message=nudge_message,
            )
            # The attribution join key and the escalation trail are recorded
            # by the shared outcome resolver — see _record_execution_outcome.
            self._record_execution_outcome(
                session, case, attempt, action, exec_result,
                actor=actor, nudge_message=nudge_message,
            )
        except Exception:
            logger.exception("Execution failed for %s", payment_id)
            attempt.result = "failed"
            attempt.result_details = {"error": "Execution exception"}

    async def _build_failure_context(
        self, failure: PaymentFailure, session: AsyncSession, *, now: datetime | None = None
    ) -> FailureContext:
        """Assemble FailureContext from payment record + DB lookups."""
        now = now or datetime.now(UTC)
        customer_id = customer_key(
            email=failure.customer_email, contact=failure.customer_contact
        )

        retry_count, nudge_count = 0, 0

        # Get previous outcomes for this payment — per-payment context, so it is
        # read regardless of whether the customer is identifiable. A webhook
        # without email/contact still describes a payment whose earlier attempts
        # the agent should know about.
        prev_attempts = await session.execute(
            select(RetryAttempt.result).where(
                RetryAttempt.payment_id == failure.payment_id
            ).order_by(RetryAttempt.created_at.desc()).limit(5)
        )
        previous_outcomes = [r for (r,) in prev_attempts.all() if r]

        if customer_id:
            ledger_row = await self._get_ledger(customer_id, session)
            if ledger_row:
                # The rolling window, not the raw column: a tally that only ever
                # increments turns "5 retries per 24h" into a lifetime ban.
                retry_count, nudge_count = self._effective_counts(ledger_row, now)

        # IST wall clock. India is UTC+5:30; deriving the hour from whole-hour
        # arithmetic put every :30–:59 minute one hour off — which matters here,
        # because this hour feeds the guardrail's blackout check at both ends of
        # the 23–7 IST window.
        local_now = now.astimezone(IST)

        promise_score = (
            await customer_promise_score(session, customer_id)
            if customer_id else None
        )

        return FailureContext(
            payment_id=failure.payment_id,
            order_id=failure.order_id,
            failure_class=failure.failure_class,
            error_code=failure.error_code,
            error_description=failure.error_description,
            error_source=failure.error_source,
            error_reason=failure.error_reason,
            amount=failure.amount,
            currency=failure.currency,
            method=failure.method,
            bank=failure.bank or failure.card_issuer,
            card_network=failure.card_network,
            card_type=failure.card_type,
            customer_id=customer_id,
            customer_email=failure.customer_email,
            customer_contact=failure.customer_contact,
            retry_count_24h=retry_count,
            nudge_count_24h=nudge_count,
            previous_retry_outcomes=previous_outcomes,
            failed_at=failure.failed_at,
            current_time=now,
            hour_of_day=local_now.hour,
            day_of_week=local_now.weekday(),
            is_retryable=failure.is_retryable,
            original_failure_id=str(failure.id),
            promise_kept=promise_score.kept if promise_score else 0,
            promise_broken=promise_score.broken if promise_score else 0,
            promise_pending=promise_score.pending if promise_score else 0,
        )

    @staticmethod
    def _effective_counts(
        ledger: RetryLedger, now: datetime
    ) -> tuple[int, int]:
        """
        The ledger's counters as they count NOW, not as they were left.

        Two reset rules, deliberately:

        ANCHORED (preferred, rows written after migration 0004): the window
        opened at retries/nudges_window_started_at and closes
        window-length after that instant — a true fixed window per customer.
        Contacts spaced just inside the window cannot keep it alive forever,
        which the old rule allowed.

        LEGACY (rows predating the anchor columns, anchor is NULL): fall back
        to the last-contact rule — if the most recent contact of a kind is
        older than the window, its tally has rolled off. Without some reset,
        a customer's fifth retry EVER trips the guardrail forever: the limit
        named "per 24h" was quietly a permanent ban.

        Reads and writes must agree on which rule applies (see
        _update_retry_ledger), or the guardrail sees one number while the
        context reports another.
        """
        window = timedelta(hours=get_settings().rate_limit_window_hours)
        retries = ledger.total_retries_24h or 0
        nudges = ledger.total_nudges_24h or 0

        retry_anchor = _aware(ledger.retries_window_started_at)
        if retry_anchor is not None:
            if now - retry_anchor > window:
                retries = 0
        else:
            last_retry = _aware(ledger.last_retry_at)
            if last_retry is None or now - last_retry > window:
                retries = 0

        nudge_anchor = _aware(ledger.nudges_window_started_at)
        if nudge_anchor is not None:
            if now - nudge_anchor > window:
                nudges = 0
        else:
            last_nudge = _aware(ledger.last_nudge_at)
            if last_nudge is None or now - last_nudge > window:
                nudges = 0

        return retries, nudges

    async def _attempt_exists(self, idempotency_key: str, session: AsyncSession) -> bool:
        """True if an attempt with this exact key was already recorded."""
        result = await session.execute(
            select(RetryAttempt.id).where(RetryAttempt.idempotency_key == idempotency_key)
        )
        return result.first() is not None

    async def _get_ledger(
        self, customer_id: str | None, session: AsyncSession
    ) -> RetryLedger | None:
        """
        The customer's rate-limit and consent row, or None if they have none.

        with_for_update(): the contact limits are read, then bumped only
        after execution, so two concurrent webhooks for one customer could
        both read 4/5 and both send — the same TOCTOU shape as the
        idempotency race, which the UNIQUE constraint closes. There is no
        constraint equivalent for a tally, so the row lock is what closes
        it. Postgres honours it; SQLite ignores it (and is single-writer
        anyway).

        The lock is deliberately NOT held across the agent call: the
        transaction commits before the LLM runs (step 5b) and this method
        re-reads under the lock afterwards (step 7b), where the case and
        the counts are re-validated together. The serialised span is
        validation → execution → ledger bump, which is where the TOCTOU
        lives; holding it across inference only serialised latency.
        """
        # Match the canonical key OR the raw value it came from. Migration
        # 0006 rewrites persisted rows, but a row written by an older process
        # mid-deploy — or by a caller still holding a legacy `case.customer_id`
        # — must not read as "no ledger": that hands the customer a FRESH
        # contact budget, which is the exact failure the canonical key exists
        # to prevent. Canonical first, so a migrated row always wins over a
        # legacy one that outlived it.
        #
        # scalars().first(), not scalar_one_or_none(): during that same window
        # both rows can legitimately exist, and the strict form raised
        # MultipleResultsFound on the money path rather than picking the right
        # one.
        for candidate in ledger_keys(customer_id):
            result = await session.execute(
                select(RetryLedger)
                .where(RetryLedger.customer_id == candidate)
                .with_for_update()
            )
            ledger = result.scalars().first()
            if ledger is not None:
                return ledger
        return None

    async def _get_attempt_count(self, payment_id: str, session: AsyncSession) -> int:
        """
        Count the retry attempts a payment has actually consumed.

        Excludes attempt_number = 0 rows — the deterministic `abandon_*`
        markers the hard-decline path writes. Those record a decision the
        case deliberately made WITHOUT spending budget, but the old count
        included them, so the guardrail's "max retries per payment" ran one
        slot tighter than the case's own attempts_used — the two budget
        sources of truth this module is required to agree. With the filter,
        count-attempts and case-budget answer the same question.
        """
        result = await session.execute(
            select(func.count()).where(
                RetryAttempt.payment_id == payment_id,
                RetryAttempt.attempt_number > 0,
            )
        )
        return result.scalar_one()

    async def _update_retry_ledger(
        self, customer_id: str, action: RetryAction, session: AsyncSession
    ) -> None:
        """Update per-customer rate-limiting ledger."""
        # Look up with the ORIGINAL value: _get_ledger tries the canonical key
        # and then the raw one, and canonicalising here first would throw that
        # raw fallback away — creating a SECOND row beside the legacy one. That
        # is the split ledger this key exists to prevent, and it would have been
        # invisible: two rows, each comfortably under its own limit.
        canonical = canonical_key(customer_id)
        if canonical is None:
            return
        ledger = await self._get_ledger(customer_id, session)

        now = datetime.now(UTC)
        window = timedelta(hours=get_settings().rate_limit_window_hours)

        if ledger is None:
            # New rows are always canonical. An existing legacy row keeps its
            # own key — migration 0006 is what rewrites those, and racing it
            # here could collide with the canonical row it is creating.
            ledger = RetryLedger(customer_id=canonical)
            session.add(ledger)

        if action.action in ("retry_now", "retry_at", "switch_rail"):
            # ANCHORED window (see _effective_counts): reset when the anchor
            # itself has aged out of the window; otherwise increment and keep
            # the anchor where it is — that anchor is what makes the window
            # deterministic instead of "24h from the last contact". Legacy
            # rows with no anchor fall back to the last-contact rule and gain
            # an anchor on this very write, upgrading them to the anchored
            # behaviour from their next window on.
            last_retry = _aware(ledger.last_retry_at)
            anchor = _aware(ledger.retries_window_started_at)
            if anchor is not None:
                if now - anchor > window:
                    ledger.total_retries_24h = 0
                    ledger.retries_window_started_at = now
            elif last_retry is None or now - last_retry > window:
                ledger.total_retries_24h = 0
                ledger.retries_window_started_at = now
            else:
                ledger.retries_window_started_at = last_retry
            # `default=0` on the column is a FLUSH-time default, so a ledger
            # constructed a few lines above still holds None here and `+= 1`
            # raises TypeError — deterministically, on the first retry for every
            # new customer. `or 0` also covers rows already NULL in the table.
            ledger.total_retries_24h = (ledger.total_retries_24h or 0) + 1
            ledger.last_retry_at = now

        if action.action == "nudge_customer":
            last_nudge = _aware(ledger.last_nudge_at)
            anchor = _aware(ledger.nudges_window_started_at)
            if anchor is not None:
                if now - anchor > window:
                    ledger.total_nudges_24h = 0
                    ledger.nudges_window_started_at = now
            elif last_nudge is None or now - last_nudge > window:
                ledger.total_nudges_24h = 0
                ledger.nudges_window_started_at = now
            else:
                ledger.nudges_window_started_at = last_nudge
            ledger.total_nudges_24h = (ledger.total_nudges_24h or 0) + 1
            ledger.last_nudge_at = now


# ── Module-level helpers ─────────────────────────────────────────────────

_orchestrator: PaymentRecoveryOrchestrator | None = None


def get_orchestrator() -> PaymentRecoveryOrchestrator:
    """Cached singleton orchestrator."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = PaymentRecoveryOrchestrator()
    return _orchestrator


async def process_payment_failure(event: WebhookEvent, session: AsyncSession) -> None:
    """Convenience function called by the webhook router."""
    orchestrator = get_orchestrator()
    await orchestrator.process_payment_failure(event, session)


async def process_risk_event(event: RiskEvent, session: AsyncSession) -> None:
    """Convenience function called by the risk-event ingestion router."""
    orchestrator = get_orchestrator()
    await orchestrator.process_risk_event(event, session)
