"""
The bulk demo seeder.

One property matters more than the rest and it is what these pin: **the
decisions are real**. The seeder writes rows in bulk, but every case is
routed through the actual ClassifierMapper, XGBoostBaseline and
GuardrailGate first. A batch whose classifications or guardrail verdicts
were invented would make the headline recovery figure meaningless, which is
the one number the whole demo exists to produce.

The second property is that refusals are represented. A dataset of nothing
but successes cannot show that the engine knows when to stop, and stopping
is a judged criterion.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from scripts.seed_bulk import DECLINES, IST_HOUR_WEIGHTS, seed
from src.classifier.mapper import ClassifierMapper
from src.models import CaseEvent, PaymentFailure, RecoveryCase, RetryAttempt


@pytest.fixture
def _demo_env(monkeypatch: Any, tmp_path: Any) -> Any:
    from src.config import get_settings

    db = tmp_path / "bulk.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_every_decline_reason_is_one_the_classifier_knows() -> None:
    """
    The seeder draws from Razorpay's own vocabulary. If one of these ever
    stopped mapping, the demo would quietly fill with `unknown` — which is
    non-retryable, so the batch would show the engine abandoning everything
    and nobody would know why.
    """
    from src.classifier.taxonomy import FailureClass

    mapper = ClassifierMapper()
    for reason, code, source, step, _weight in DECLINES:
        fc, _ = mapper.classify(code, None, source, step, reason)
        assert fc is not FailureClass.UNKNOWN, reason


def test_the_hour_curve_covers_the_whole_clock() -> None:
    """24 weights, none negative — the blackout slice has to be a draw from a
    real distribution, not a hole in one."""
    assert len(IST_HOUR_WEIGHTS) == 24
    assert all(w > 0 for w in IST_HOUR_WEIGHTS)
    # Overnight must be the trough, or the blackout rejects an absurd share.
    overnight = sum(IST_HOUR_WEIGHTS[23:] + IST_HOUR_WEIGHTS[:7])
    assert overnight / sum(IST_HOUR_WEIGHTS) < 0.10


async def test_a_seeded_batch_carries_real_verdicts_and_refusals(
    _demo_env: Any,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker as _sm
    from sqlalchemy.ext.asyncio import create_async_engine

    import src.receivables.models  # noqa: F401  — register every table
    from src.config import get_settings
    from src.database import Base

    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    tally = await seed(count=120, recovered_rate=0.4, seed_value=11)
    assert tally["cases"] == 120

    engine = create_async_engine(get_settings().database_url)
    sm = _sm(engine, expire_on_commit=False)
    mapper = ClassifierMapper()
    try:
        async with sm() as session:
            # 1. Every stored classification is the mapper's own answer.
            failures = (await session.execute(select(PaymentFailure))).scalars().all()
            assert failures
            for f in failures:
                fc, retryable = mapper.classify(
                    f.error_code, f.error_description, f.error_source,
                    f.error_step, f.error_reason,
                )
                assert f.failure_class == fc.value, f.error_reason
                assert f.is_retryable == retryable, f.error_reason

            # 2. Refusals are present and cost no attempt — the whole point
            # of a guardrail block is that nothing was spent.
            blocked = (await session.execute(
                select(CaseEvent).where(CaseEvent.event_type == "guardrail_blocked")
            )).scalars().all()
            assert blocked, "a batch with no refusals cannot show stopping rules"
            for ev in blocked:
                assert ev.detail and ev.detail["rejection_reasons"], "silent refusal"
                spent = await session.scalar(
                    select(func.count()).select_from(RetryAttempt)
                    .where(RetryAttempt.recovery_case_id == ev.recovery_case_id)
                )
                assert spent == 0, "a blocked case must not have spent an attempt"

            # 3. Recovered money is attributed to the attempt that earned it.
            recovered = (await session.execute(
                select(RecoveryCase).where(RecoveryCase.state == "recovered")
            )).scalars().all()
            assert recovered
            for case in recovered:
                assert case.amount_recovered > 0
                assert case.recovered_via_attempt_id is not None, (
                    "unattributed recovery — the engine would be taking credit "
                    "for money it did not earn"
                )

            # 4. The agent's reasoning and confidence are persisted, because
            # the case-detail screen renders them.
            attempts = (await session.execute(select(RetryAttempt))).scalars().all()
            assert attempts
            assert any(a.agent_reasoning for a in attempts)
            assert all(a.guardrail_passed for a in attempts)
    finally:
        await engine.dispose()
