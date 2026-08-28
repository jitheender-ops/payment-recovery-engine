"""
Hardening round 2 — the fixes that came out of the architecture review.

Each test pins one of the review findings so it cannot quietly regress:

1.  Old payment links stay payable      → cancel_links_for_closed_cases
2.  switch_rail is decorative           → upi_link=True on UPI-target links
3.  Webhook secret doubles as PII key   → dedicated pii_mask_secret
4.  Two sources of truth for budget     → attempt count skips abandon rows
6.  "per 24h" was 24h-from-last-contact → anchored windows
7.  Reconcile consumed events on error  → re-arm with a retry cap
8.  Deploys ate deliberate retries      → stale scheduler claims re-park
10. Public recovery page unthrottled    → per-IP rate limits
15. Scheduler can die silently          → heartbeat row stamped every tick
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import src.customer.routes as customer_routes
import src.scheduler as scheduler
from src.agent.prompts import mask_customer_id
from src.config import get_settings
from src.models import (
    RecoveryCase,
    RetryAttempt,
    SchedulerHeartbeat,
    WebhookEvent,
)
from src.orchestrator import PaymentRecoveryOrchestrator, get_orchestrator

# ── helpers ──────────────────────────────────────────────────────────────────


def _settings_patcher(monkeypatch: Any, **overrides: Any) -> None:
    """Patch get_settings() used inside the modules under test."""
    real = get_settings()
    patched = type(real)(**{
        **{k: getattr(real, k) for k in type(real).model_fields},
        **overrides,
    })
    for module in (scheduler,):
        monkeypatch.setattr(module, "get_settings", lambda p=patched: p)


async def _open_case(
    sm: async_sessionmaker[AsyncSession],
    subject: str,
    *,
    state: str = "open",
) -> RecoveryCase:
    from src.cases import close_case, open_case

    async with sm() as session:
        case = await open_case(
            session,
            risk_type="payment_failure",
            subject_ref=subject,
            amount_at_risk=50_000,
            customer_id="c@example.com",
        )
        if state != "open":
            close_case(case, state, "test")  # type: ignore[arg-type]
            session.add(case)
        await session.commit()
        return case


async def _insert_attempt(
    sm: async_sessionmaker[AsyncSession],
    *,
    key: str,
    payment_id: str,
    case: RecoveryCase,
    result: str,
    age_seconds: int = 10_000,
    external_ref: str | None = None,
    result_details: dict[str, Any] | None = None,
) -> None:
    async with sm() as session:
        attempt = RetryAttempt(
            payment_id=payment_id,
            idempotency_key=key,
            attempt_number=1,
            action_type="retry_now",
            guardrail_passed=True,
            result=result,
            external_ref=external_ref,
            result_details=result_details,
            created_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        )
        attempt.recovery_case_id = case.id
        session.add(attempt)
        await session.commit()


# ── Fix 1b: links of closed cases get cancelled ─────────────────────────────


class _FakeExecutor:
    def __init__(self, fail_first: int = 0) -> None:
        self.calls: list[str] = []
        self._fail_first = fail_first

    async def cancel_payment_link(self, link_id: str) -> bool:
        if self._fail_first > 0:
            self._fail_first -= 1
            return False
        self.calls.append(link_id)
        return True


async def test_links_of_recovered_cases_are_cancelled(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """
    A retry mints a NEW link per attempt while the old ones stay live on
    Razorpay's side. When the case closes, its links must die with it — a
    customer paying an old link after a newer one settled is a double payment
    the case would credit twice.
    """
    case = await _open_case(db_sessionmaker, "pay_linkkill", state="recovered")
    await _insert_attempt(
        db_sessionmaker,
        key="retry_linkkill_0",
        payment_id="pay_linkkill",
        case=case,
        result="superseded",
        external_ref="plink_OLD",
    )

    fake = _FakeExecutor()
    orch = get_orchestrator()
    monkeypatch.setattr(orch, "_executor", fake)

    async with db_sessionmaker() as session:
        cancelled = await scheduler.cancel_links_for_closed_cases(session, orch)
        assert cancelled == 1

    assert fake.calls == ["plink_OLD"]
    async with db_sessionmaker() as reader:
        row = (
            await reader.execute(
                select(RetryAttempt).where(
                    RetryAttempt.idempotency_key == "retry_linkkill_0"
                )
            )
        ).scalar_one()
    assert row.result_details["link_cancelled_at"] is not None


async def test_open_cases_keep_their_links(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """Only terminal cases get the sweep's attention; live cases may still be paid."""
    case = await _open_case(db_sessionmaker, "pay_stillopen", state="open")
    await _insert_attempt(
        db_sessionmaker,
        key="retry_stillopen_0",
        payment_id="pay_stillopen",
        case=case,
        result="success",
        external_ref="plink_LIVE",
    )

    fake = _FakeExecutor()
    orch = get_orchestrator()
    monkeypatch.setattr(orch, "_executor", fake)

    async with db_sessionmaker() as session:
        assert await scheduler.cancel_links_for_closed_cases(session, orch) == 0
    assert fake.calls == []


async def test_failed_cancel_is_retried_next_sweep(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """A network failure during cancel stays unmarked so the next tick retries it."""
    case = await _open_case(db_sessionmaker, "pay_retrycancel", state="recovered")
    await _insert_attempt(
        db_sessionmaker,
        key="retry_retrycancel_0",
        payment_id="pay_retrycancel",
        case=case,
        result="superseded",
        external_ref="plink_FLAKY",
    )

    flaky = _FakeExecutor(fail_first=1)
    orch = get_orchestrator()
    monkeypatch.setattr(orch, "_executor", flaky)

    async with db_sessionmaker() as session:
        assert await scheduler.cancel_links_for_closed_cases(session, orch) == 0
    async with db_sessionmaker() as session:
        assert await scheduler.cancel_links_for_closed_cases(session, orch) == 1
    assert flaky.calls == ["plink_FLAKY"]


# ── Fix 8: stale scheduler claims re-park; write-aheads stay fail-closed ────


async def test_stale_scheduler_claim_is_reparked_not_failed(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    The fire sweep claimed the row, then the process died before the
    write-ahead — Razorpay was never called, so there is nothing to be
    'outcome unknown' about. The agent's most deliberate decision (a timed
    retry) should survive a deploy that happened to overlap it.
    """
    case = await _open_case(db_sessionmaker, "pay_repark")
    await _insert_attempt(
        db_sessionmaker,
        key="retry_repark_0",
        payment_id="pay_repark",
        case=case,
        result="pending",
        result_details={"claimed_by": "scheduler", "claimed_at": "2026-08-26T10:00:00+00:00"},
    )

    async with db_sessionmaker() as session:
        await scheduler.reconcile_stale_attempts(session)
        await session.commit()

    async with db_sessionmaker() as reader:
        row = (
            await reader.execute(
                select(RetryAttempt).where(RetryAttempt.idempotency_key == "retry_repark_0")
            )
        ).scalar_one()
    assert row.result == "scheduled"
    assert row.scheduled_at is not None
    assert "re-parked" in json.dumps(row.result_details)


async def test_write_ahead_phase_still_resolves_fail_closed(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A row past the write-ahead may have reached Razorpay — never re-park it."""
    case = await _open_case(db_sessionmaker, "pay_wa")
    await _insert_attempt(
        db_sessionmaker,
        key="retry_wa_0",
        payment_id="pay_wa",
        case=case,
        result="pending",
        result_details={"phase": "write_ahead"},
    )

    async with db_sessionmaker() as session:
        await scheduler.reconcile_stale_attempts(session)
        await session.commit()

    async with db_sessionmaker() as reader:
        row = (
            await reader.execute(
                select(RetryAttempt).where(RetryAttempt.idempotency_key == "retry_wa_0")
            )
        ).scalar_one()
    assert row.result == "failed"
    assert "stale-pending" in str(row.result_details)


# ── Fix 7: reconcile re-arms transient failures with a cap ──────────────────


async def test_failed_reconcile_is_rearmed_then_gives_up(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """
    A transient database blip must not permanently skip a real payment
    failure; a payload that raises on every tick must also not eat the batch
    forever. Three strikes, then rest with the error recorded.
    """
    received = datetime.now(UTC) - timedelta(seconds=10_000)
    async with db_sessionmaker() as session:
        event = WebhookEvent(
            razorpay_event_id="evt_flaky",
            event_type="payment.failed",
            payload={
                "payload": {"payment": {"entity": {"id": "pay_flaky"}}}
            },
            received_at=received,
            processed=False,
            processing_attempts=0,
        )
        session.add(event)
        await session.commit()
        event_id = event.id

    calls = {"n": 0}

    async def exploding(event: WebhookEvent, session: AsyncSession) -> None:
        calls["n"] += 1
        raise RuntimeError("transient blip")

    monkeypatch.setattr(scheduler, "process_payment_failure", exploding)

    for expected_attempts in (1, 2):
        async with db_sessionmaker() as session:
            await scheduler.reconcile_events(session)
            await session.commit()
        async with db_sessionmaker() as reader:
            row = await reader.get(WebhookEvent, event_id)
        assert row.processed is False, f"should be re-armed after {expected_attempts} failures"
        assert row.processing_attempts == expected_attempts

    async with db_sessionmaker() as session:
        await scheduler.reconcile_events(session)
        await session.commit()
    async with db_sessionmaker() as reader:
        row = await reader.get(WebhookEvent, event_id)
    assert row.processed is True
    assert row.processing_attempts == 3
    assert "retry cap" in (row.processing_error or "")


# ── Fix 4: attempt budget skips the deterministic abandon markers ───────────


async def test_attempt_count_excludes_abandon_markers(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    The hard-decline path writes an attempt_number=0 abandon row WITHOUT
    spending case budget — but the old count included it, so the guardrail's
    per-payment budget ran one slot tighter than the case's own. Both must
    answer the same question.
    """
    case = await _open_case(db_sessionmaker, "pay_abandoncount")
    await _insert_attempt(
        db_sessionmaker,
        key="abandon_pay_abandoncount",
        payment_id="pay_abandoncount",
        case=case,
        result="skipped",
    )
    # Rewrite it as an attempt_number=0 marker, as the orchestrator writes it.
    async with db_sessionmaker() as session:
        row = (
            await session.execute(
                select(RetryAttempt).where(
                    RetryAttempt.idempotency_key == "abandon_pay_abandoncount"
                )
            )
        ).scalar_one()
        row.attempt_number = 0
        await session.commit()

    async with db_sessionmaker() as session:
        orch = PaymentRecoveryOrchestrator.__new__(PaymentRecoveryOrchestrator)
        assert await orch._get_attempt_count("pay_abandoncount", session) == 0


# ── Fix 6: anchored rate-limit windows ──────────────────────────────────────


async def test_anchored_window_resets_even_when_last_contact_is_recent(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """
    The old rule keyed the reset off the LAST contact, so contacts spaced just
    inside the window kept the tally alive forever — '5 per 24h' was really
    '5 per 24h from the last contact'. With the anchor, a window opened 25h
    ago resets no matter how recent the last contact was.
    """
    from src.models import RetryLedger

    now = datetime.now(UTC)
    async with db_sessionmaker() as session:
        ledger = RetryLedger(
            customer_id="anchored@example.com",
            total_retries_24h=4,
            total_nudges_24h=0,
            last_retry_at=now - timedelta(hours=1),  # very recent
            retries_window_started_at=now - timedelta(hours=25),  # anchor expired
            consent_status="granted",
        )
        session.add(ledger)
        await session.commit()

    async with db_sessionmaker() as session:
        orch = PaymentRecoveryOrchestrator.__new__(PaymentRecoveryOrchestrator)
        row = await orch._get_ledger("anchored@example.com", session)
        assert row is not None
        retries, _ = orch._effective_counts(row, now)
        assert retries == 0, "expired anchor must reset the tally despite recent contact"


async def test_anchored_window_counts_within_the_window(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from src.models import RetryLedger

    now = datetime.now(UTC)
    async with db_sessionmaker() as session:
        session.add(
            RetryLedger(
                customer_id="inwindow@example.com",
                total_retries_24h=3,
                total_nudges_24h=0,
                last_retry_at=now - timedelta(hours=2),
                retries_window_started_at=now - timedelta(hours=2),
                consent_status="granted",
            )
        )
        await session.commit()

    async with db_sessionmaker() as session:
        orch = PaymentRecoveryOrchestrator.__new__(PaymentRecoveryOrchestrator)
        row = await orch._get_ledger("inwindow@example.com", session)
        assert row is not None
        retries, _ = orch._effective_counts(row, now)
        assert retries == 3


# ── Fix 2: switch_rail to UPI creates a UPI-only link ───────────────────────


async def test_upi_target_rail_creates_upi_only_link() -> None:
    """A 'switch to UPI' decision must not execute as a generic link the
    customer can pay by card — upi_link=True makes the rail real."""
    from src.executor.retry_executor import RetryExecutor
    from src.models import PaymentFailure

    captured: dict[str, Any] = {}

    class _Client:
        class payment_link:  # noqa: N801 — mirrors the SDK's shape
            @staticmethod
            def create(data: dict[str, Any]) -> dict[str, Any]:
                captured.update(data)
                return {"id": "plink_upi", "short_url": "https://rzp.io/i/upi"}

    executor = RetryExecutor.__new__(RetryExecutor)
    executor._client = _Client()

    from concurrent.futures import ThreadPoolExecutor

    executor._pool = ThreadPoolExecutor(max_workers=1)

    failure = PaymentFailure(
        payment_id="pay_upi",
        amount=10000,
        currency="INR",
        method="card",
        failure_class="3ds_dropoff",
    )
    result = await executor.execute_retry(
        payment_failure=failure,
        action_type="switch_rail",
        target_rail="upi",
        idempotency_key="retry_pay_upi_0",
    )

    assert result["success"] is True
    assert captured["upi_link"] is True
    assert captured["notes"]["target_rail"] == "upi"


async def test_non_upi_rail_stays_generic_link() -> None:
    from src.executor.retry_executor import RetryExecutor
    from src.models import PaymentFailure

    captured: dict[str, Any] = {}

    class _Client:
        class payment_link:  # noqa: N801
            @staticmethod
            def create(data: dict[str, Any]) -> dict[str, Any]:
                captured.update(data)
                return {"id": "plink_gen", "short_url": "https://rzp.io/i/gen"}

    executor = RetryExecutor.__new__(RetryExecutor)
    executor._client = _Client()
    from concurrent.futures import ThreadPoolExecutor

    executor._pool = ThreadPoolExecutor(max_workers=1)

    failure = PaymentFailure(
        payment_id="pay_gen",
        amount=10000,
        currency="INR",
        method="upi",
        failure_class="upi_collect_timeout",
    )
    await executor.execute_retry(
        payment_failure=failure,
        action_type="switch_rail",
        target_rail="card",
        idempotency_key="retry_pay_gen_0",
    )
    assert "upi_link" not in captured
    assert captured["notes"]["target_rail"] == "card"


# ── Fix 3: dedicated PII-mask secret ─────────────────────────────────────────


def test_pii_mask_uses_dedicated_secret(monkeypatch: Any) -> None:
    """
    The webhook secret is shared with the Razorpay dashboard and proves
    RAZORPAY'S identity — using it to key customer pseudonymisation meant one
    leak unmasked every customer. The dedicated secret takes precedence the
    moment it is set; empty falls back for pre-existing deployments.
    """
    real = get_settings()

    async def _check() -> None:
        fields = {k: getattr(real, k) for k in type(real).model_fields}

        fields.update(razorpay_webhook_secret="webhook-secret", pii_mask_secret="dedicated-secret")
        dedicated = type(real)(**fields)
        monkeypatch.setattr("src.agent.prompts.get_settings", lambda: dedicated)
        with_dedicated = mask_customer_id("customer@example.com")

        fields.update(pii_mask_secret="")
        fallback = type(real)(**fields)
        monkeypatch.setattr("src.agent.prompts.get_settings", lambda: fallback)
        with_fallback = mask_customer_id("customer@example.com")

        assert with_dedicated != with_fallback
        assert with_dedicated.startswith("cust_")

    import asyncio

    asyncio.run(_check())


# ── Fix 10: rate limits on the public recovery surface ──────────────────────


class _FakeRequest:
    def __init__(self, ip: str) -> None:
        self.headers = {"x-forwarded-for": ip}
        self.client = None


def test_rate_limit_blocks_after_budget(
    monkeypatch: Any,
) -> None:
    # XFF is honoured only behind a trusted proxy (the leftmost entry is
    # client-spoofable otherwise) — these bucketing tests run as that
    # deployment would.
    get_settings.cache_clear()
    monkeypatch.setenv("BEHIND_TRUSTED_PROXY", "true")
    customer_routes._RATE_LIMIT_BUCKETS.clear()
    request = _FakeRequest("1.2.3.4")
    for _ in range(customer_routes._PAY_LIMIT):
        customer_routes._check_rate_limit(request, kind="pay", limit=customer_routes._PAY_LIMIT)
    with pytest.raises(customer_routes.HTTPException) as exc:
        customer_routes._check_rate_limit(request, kind="pay", limit=customer_routes._PAY_LIMIT)
    assert exc.value.status_code == 429

    # A different IP is a different bucket.
    customer_routes._check_rate_limit(_FakeRequest("5.6.7.8"), kind="pay", limit=6)

    # The page bucket is independent of the pay bucket.
    customer_routes._check_rate_limit(request, kind="page", limit=customer_routes._PAGE_LIMIT)
    customer_routes._RATE_LIMIT_BUCKETS.clear()
    get_settings.cache_clear()


def test_rate_limit_window_releases(
    monkeypatch: Any,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("BEHIND_TRUSTED_PROXY", "true")
    customer_routes._RATE_LIMIT_BUCKETS.clear()
    request = _FakeRequest("9.9.9.9")
    bucket = customer_routes._RATE_LIMIT_BUCKETS.setdefault("pay:9.9.9.9", deque())
    bucket.append(0.0)  # ancient timestamp
    monkeypatch.setattr(customer_routes.time, "monotonic", lambda: 1e9)
    customer_routes._check_rate_limit(request, kind="pay", limit=1)
    customer_routes._RATE_LIMIT_BUCKETS.clear()
    get_settings.cache_clear()


# ── Fix 15: the scheduler stamps its heartbeat ───────────────────────────────


async def test_tick_stamps_the_heartbeat(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """
    A tick that swallows an exception is indistinguishable from a scheduler
    that died days ago — unless something outside the loop remembers when it
    last ran. The heartbeat row is that something, and the Operations view
    reads it.
    """
    async with db_sessionmaker() as session:
        await scheduler.tick(session)

    async with db_sessionmaker() as reader:
        row = await reader.get(SchedulerHeartbeat, 1)
    assert row is not None
    assert row.last_tick_at is not None
    assert "retries_fired" in (row.last_tick_counts or {})


async def test_expired_cases_also_get_their_links_cancelled(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """`expired` is terminal too — its links must die with the case."""
    case = await _open_case(db_sessionmaker, "pay_expired", state="expired")
    await _insert_attempt(
        db_sessionmaker,
        key="retry_expired_0",
        payment_id="pay_expired",
        case=case,
        result="superseded",
        external_ref="plink_EXPIRED",
    )

    fake = _FakeExecutor()
    orch = get_orchestrator()
    monkeypatch.setattr(orch, "_executor", fake)

    async with db_sessionmaker() as session:
        assert await scheduler.cancel_links_for_closed_cases(session, orch) == 1
    assert fake.calls == ["plink_EXPIRED"]
