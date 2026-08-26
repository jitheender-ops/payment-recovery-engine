"""
Payment Recovery Orchestrator — ties all five layers together.

Flow: classify → check retryability → build context → agent decision →
guardrail validation → execute/nudge → log everything.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src import recovery_link
from src.agent.actions import FailureContext, RetryAction
from src.agent.policy_agent import PolicyAgent
from src.cases import attach_attempt, close_case, log_event, open_case, stop_reason
from src.classifier.mapper import ClassifierMapper
from src.config import get_settings
from src.executor.rail_selector import resolve_target_rail
from src.executor.retry_executor import RetryExecutor
from src.guardrail.gate import GuardrailGate
from src.guardrail.rules import IST, clamp_retry_at_out_of_blackout
from src.messaging.nudge_generator import NudgeGenerator
from src.models import PaymentFailure, RecoveryCase, RetryAttempt, RetryLedger, WebhookEvent

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

    def _get_agent(self) -> PolicyAgent | None:
        """Lazy-init agent (needs API keys at runtime). None if init failed."""
        if self._agent is None:
            try:
                self._agent = PolicyAgent()
            except Exception:
                logger.warning("Failed to init PolicyAgent — will use XGBoost fallback")
        return self._agent

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
            customer_id=failure_record.customer_email or failure_record.customer_contact,
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
                "deferred" if stop.startswith("next action not due") else "stopped",
                reason=stop,
                attempts_used=case.attempts_used,
                max_attempts=case.max_attempts,
            )
            await session.commit()
            return

        # ── Step 5: Build context ─────────────────────────────────────────
        context = await self._build_failure_context(failure_record, session)

        # ── Step 6: Agent decision ────────────────────────────────────────
        agent = self._get_agent()
        # agent_type must record who ACTUALLY decided, not who was configured to.
        # PolicyAgent.decide() catches its own LLM errors and returns a heuristic
        # action, so "an agent object exists" says nothing about whether the LLM
        # answered. Comparing its fallback counter across the call is what
        # distinguishes a real LLM decision from a silent degradation — without
        # it the audit trail claims an LLM made calls it never saw.
        from src.agent.xgboost_baseline import XGBoostBaseline

        action = None
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
                logger.exception("Agent raised, using XGBoost")

        if action is None:
            action = XGBoostBaseline().predict(context)
            agent_type = "xgboost"

        # ── Step 6b: Resolve the target rail ──────────────────────────────
        # Before the guardrail, so Layer 4 validates the action that actually
        # executes rather than the one the agent first proposed. Every decision
        # path funnels through here — LLM, LLM-fallback and XGBoost — so this is
        # the one place it has to hold.
        if action.action == "switch_rail":
            resolved = resolve_target_rail(
                context.method, action.rail, context.failure_class
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

        # ── Step 8: Guardrail validation ──────────────────────────────────
        guardrail_result = self._guardrail.validate(
            action, context, idem_key, attempt_count
        )

        # ── Step 9: Create RetryAttempt record ────────────────────────────
        attempt = RetryAttempt(
            payment_failure_id=failure_record.id,
            payment_id=payment_id,
            idempotency_key=idem_key,
            attempt_number=attempt_count + 1,
            action_type=action.action,
            target_rail=action.rail,
            # Persist UTC, always. The agent hands back an IST-aware time and
            # Postgres would normalise it, but SQLite's DATETIME drops the zone
            # and stores the wall clock it was given — so an IST value read back
            # naive is 5h30m adrift, which is enough to move a retry into the
            # blackout window it was just clamped out of.
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

    async def _update_ledger_and_commit(
        self, customer_id: str | None, action: RetryAction, session: AsyncSession
    ) -> None:
        """Step 12 — bump the per-customer rate-limit tally, then commit."""
        if customer_id:
            await self._update_retry_ledger(customer_id, action, session)
        await session.commit()

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
            attempt.executed_at = datetime.now(UTC)
            attempt.result = "success" if exec_result.get("success") else "failed"
            attempt.result_details = exec_result
            # The attribution join key. Razorpay returns the Payment Link id
            # here; when the customer pays it, the capture webhook is the only
            # place this id and the new payment id appear together. Not
            # persisting it is what made recovered revenue unattributable.
            link_id = exec_result.get("payment_link_id")
            if link_id:
                attempt.external_ref = str(link_id)
            if nudge_message:
                attempt.nudge_sent = exec_result.get("nudge_sent", False)
            # The channel that actually reached them. A nudge notifies by SMS
            # and email both; only the first is recorded, which is enough for
            # per-channel limits and not enough to reconstruct a two-channel
            # send — split this into its own contacts table if that becomes
            # the question.
            channels = exec_result.get("channels") or []
            attempt.channel = channels[0] if channels else "payment_link"

            if attempt.result == "success" and action.action == "nudge_customer":
                log_event(
                    session,
                    case,
                    "escalated",
                    actor=actor,
                    level=case.escalation_level,
                    channel=attempt.channel,
                    next_action_at=(
                        case.next_action_at.isoformat() if case.next_action_at else None
                    ),
                )
            elif attempt.result == "success":
                log_event(
                    session,
                    case,
                    "contacted",
                    actor=actor,
                    action=action.action,
                    channel=attempt.channel,
                    external_ref=attempt.external_ref,
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
        customer_id = failure.customer_email or failure.customer_contact

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

        with_for_update(): the contact limits are checked from a snapshot
        taken in build_failure_context and bumped only after execution, so
        two concurrent webhooks for one customer could both read 4/5 and both
        send — the same TOCTOU shape as the idempotency race, which the UNIQUE
        constraint closes. There is no constraint equivalent for a tally, so
        the row lock is what closes it: the read takes the lock and the
        transaction holds it until its commit, serialising per-customer
        decisions end to end. Postgres honours it; SQLite ignores it (and is
        single-writer anyway). The lock is held across the agent call — a
        second webhook for the SAME customer waits, which is the correct
        behaviour; a different customer never touches this row.
        """
        if not customer_id:
            return None
        result = await session.execute(
            select(RetryLedger)
            .where(RetryLedger.customer_id == customer_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

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
        ledger = await self._get_ledger(customer_id, session)

        now = datetime.now(UTC)
        window = timedelta(hours=get_settings().rate_limit_window_hours)

        if ledger is None:
            ledger = RetryLedger(customer_id=customer_id)
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
