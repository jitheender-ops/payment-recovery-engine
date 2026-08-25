"""
Property-based tests for the guardrail — the layer that must never be wrong.

Example-based tests check the cases someone thought of. These check the RULES
themselves across generated classes of input: every hour of the day, consent
windows straddling their boundary, amounts around the ceiling, keys that are
empty-but-not-None. If a threshold edit or refactor breaks an edge anywhere in
the input space, one of these should find it before a payment does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from src.config import get_settings
from src.guardrail.rules import GuardrailRules, is_in_blackout
from src.messaging.templates import render_fallback

RULES = GuardrailRules()
S = get_settings()


# ── Time-of-day blackout ─────────────────────────────────────────────────


@given(hour=st.integers(min_value=0, max_value=23))
def test_blackout_rule_agrees_with_shared_helper_across_all_hours(hour: int) -> None:
    """The rule and the clamp's helper can never disagree about an hour."""
    passed, _ = RULES.check_time_of_day_blackout(hour)
    assert passed == (not is_in_blackout(hour, S))


@given(hour=st.integers(min_value=0, max_value=23))
def test_blackout_covers_exactly_the_configured_window(hour: int) -> None:
    start, end = S.retry_blackout_start_hour, S.retry_blackout_end_hour
    expected = hour >= start or hour < end if start > end else start <= hour < end
    assert is_in_blackout(hour, S) is expected


# ── Consent window ───────────────────────────────────────────────────────


@settings(max_examples=60)
@given(
    elapsed_hours=st.integers(min_value=0, max_value=24 * 30),
    minute_jitter=st.integers(min_value=0, max_value=59),
)
def test_consent_window_is_exact_at_the_boundary(
    elapsed_hours: int, minute_jitter: int
) -> None:
    failed_at = datetime(2026, 1, 1, tzinfo=UTC)
    now = failed_at + timedelta(hours=elapsed_hours, minutes=minute_jitter)
    passed, reason = RULES.check_consent_window(failed_at, now)

    deadline = failed_at + timedelta(hours=S.consent_window_hours)
    assert passed == (now <= deadline)
    if not passed:
        assert "Consent window expired" in (reason or "")


def test_consent_window_tolerates_naive_datetimes() -> None:
    """DB rows can arrive naive (SQLite harness); naive must mean UTC, not crash."""
    failed_at = datetime(2026, 1, 1)  # naive
    now = datetime(2026, 1, 2)  # 24h later, naive
    passed, _ = RULES.check_consent_window(failed_at, now)
    assert passed is (24 <= S.consent_window_hours)


# ── Amount ceiling ───────────────────────────────────────────────────────


@given(amount=st.integers(min_value=0, max_value=S.amount_ceiling_inr))
def test_every_amount_up_to_the_ceiling_passes(amount: int) -> None:
    passed, _ = RULES.check_amount_ceiling(amount)
    assert passed is True


@given(
    over_by=st.integers(min_value=1, max_value=10_000_000_000 - S.amount_ceiling_inr)
)
def test_every_amount_above_the_ceiling_fails(over_by: int) -> None:
    passed, reason = RULES.check_amount_ceiling(S.amount_ceiling_inr + over_by)
    assert passed is False
    assert "ceiling" in (reason or "").lower()


# ── Per-payment / per-customer budgets ───────────────────────────────────


# The cap consumes a slot: with max=3, attempts 0..2 pass and attempt 3
# is vetoed — 'max retries per payment' counts ATTEMPTS MADE, not slots
# remaining. The boundary case has its own test below.
@given(attempts=st.integers(min_value=0, max_value=S.max_retries_per_payment - 1))
def test_budget_slots_below_the_cap_pass(attempts: int) -> None:
    passed, _ = RULES.check_max_retries_per_payment("pay_prop", attempts)
    assert passed is True


@given(
    extra=st.integers(min_value=0, max_value=50),
)
def test_budget_slots_from_the_cap_up_fail(extra: int) -> None:
    passed, _ = RULES.check_max_retries_per_payment(
        "pay_prop", S.max_retries_per_payment + extra
    )
    assert passed is False


@given(nudges=st.integers(min_value=0, max_value=S.max_nudges_per_customer_24h - 1))
def test_nudge_slots_below_the_cap_pass(nudges: int) -> None:
    passed, _ = RULES.check_customer_nudge_rate_limit(nudges)
    assert passed is True


# ── Idempotency keys ─────────────────────────────────────────────────────


@given(
    junk=st.one_of(
        st.just(""),
        st.just("   "),
        st.text(alphabet=" \t\n", min_size=1, max_size=10),
        st.none(),
    )
)
def test_blank_idempotency_keys_always_fail(junk: str | None) -> None:
    """None, empty, whitespace-only — every flavour of missing must veto."""
    passed, reason = RULES.check_idempotency_key(junk)
    assert passed is False
    assert "idempotent" in (reason or "").lower()


@given(key=st.text(min_size=1, max_size=64).filter(lambda k: k.strip()))
def test_any_real_key_passes(key: str) -> None:
    passed, _ = RULES.check_idempotency_key(key)
    assert passed is True


# ── Nudge templates ──────────────────────────────────────────────────────


@settings(max_examples=40)
@given(
    name=st.one_of(st.none(), st.text(min_size=1, max_size=80)),
    amount=st.integers(min_value=0, max_value=100_000_000),
)
def test_rendered_nudges_are_deterministic_and_self_contained(
    name: str | None, amount: int
) -> None:
    """
    The renderer's actual contract (the 160-char SMS cap lives one layer up,
    in NudgeGenerator.generate's truncation): same inputs → identical string,
    the customer's name only ever appears as itself, and an unknown failure
    class falls back to the generic wording rather than raising.
    """
    amount_display = f"{amount / 100:,.2f}"
    msg_a = render_fallback("insufficient_funds", amount_display, customer_name=name)
    msg_b = render_fallback("insufficient_funds", amount_display, customer_name=name)
    assert msg_a == msg_b
    if name:
        assert name.strip() in msg_a or "&" in msg_a  # raw or entity-escaped
    fallback = render_fallback("totally_unknown_class", "10.00")
    assert "didn't go through" in fallback


def test_customer_name_cannot_inject_markup() -> None:
    """The autoescape fix: webhook-controlled text arrives inert."""
    nasty = "<script>alert(1)</script>"
    msg = render_fallback("network_error", "100.00", customer_name=nasty)
    assert "<script>" not in msg


def test_the_cap_slot_itself_is_vetoed() -> None:
    """The exact edge the two ranges above exclude, stated on its own."""
    passed, _ = RULES.check_max_retries_per_payment("pay_prop", S.max_retries_per_payment)
    assert passed is False
    passed, _ = RULES.check_customer_nudge_rate_limit(S.max_nudges_per_customer_24h)
    assert passed is False


# ── Ground truth, not tautology ──────────────────────────────────────────
# The two blackout tests above prove the rule agrees with the helper — which
# they could BOTH be wrong about identically. These pin the actual contract.


def test_blackout_boundaries_against_stated_contract() -> None:
    """Window 23→7 IST: 23:xx–06:xx blocked; 07:00–22:59 allowed."""
    blocked = {23, 0, 1, 2, 3, 4, 5, 6}
    for hour in range(24):
        assert is_in_blackout(hour, S) is (hour in blocked), hour


def test_blackout_edge_hours_are_exact() -> None:
    assert is_in_blackout(22, S) is False, "22:59 must be allowed"
    assert is_in_blackout(23, S) is True, "23:00 sharp is inside"
    assert is_in_blackout(6, S) is True, "06:59's hour is still inside"
    assert is_in_blackout(7, S) is False, "07:00 sharp is outside"


# ── Both window shapes ───────────────────────────────────────────────────
# Production runs an overnight window (23→7), which exercises one branch of
# is_in_blackout and leaves the same-day branch equivalent-mutant territory.
# The contract holds for ANY window shape, so prove it against a daytime
# window explicitly.


def _day_settings():
    from src.config import Settings

    return Settings(retry_blackout_start_hour=2, retry_blackout_end_hour=5)


def test_same_day_window_blocks_exactly_its_hours() -> None:
    day = _day_settings()
    blocked = {2, 3, 4}
    for hour in range(24):
        assert is_in_blackout(hour, day) is (hour in blocked), hour


def test_both_shapes_agree_at_their_shared_semantics() -> None:
    night = S  # 23 -> 7 from env/config
    day = _day_settings()  # 2 -> 5
    # Window start hour: inside. Hour before end: inside. End hour: outside.
    assert is_in_blackout(night.retry_blackout_start_hour, night) is True
    assert is_in_blackout(night.retry_blackout_end_hour, night) is False
    assert is_in_blackout(day.retry_blackout_start_hour, day) is True
    assert is_in_blackout(day.retry_blackout_end_hour, day) is False


def test_consent_deadline_itself_still_passes() -> None:
    """
    The exact boundary point: a contact AT the deadline is allowed ('>' not
    '>='), and the deadline arithmetic moves forward, never back. Hypothesis
    could sample around here forever and miss the single instant.
    """
    failed_at = datetime(2026, 1, 1, tzinfo=UTC)
    deadline = failed_at + timedelta(hours=S.consent_window_hours)
    passed, _ = RULES.check_consent_window(failed_at, deadline)
    assert passed is True
    one_second_later = deadline + timedelta(seconds=1)
    passed, _ = RULES.check_consent_window(failed_at, one_second_later)
    assert passed is False


def test_unknown_failure_class_is_deliberately_not_blocklisted() -> None:
    """
    Documented intent: an unrecognisable class string passes THIS rule so the
    taxonomy's own UNKNOWN handling decides downstream. Pinning it so a
    future 'harden everything' edit is a conscious choice, not an accident.
    """
    passed, _ = RULES.check_hard_decline_blocklist("brand_new_class_nobody_knows")
    assert passed is True
