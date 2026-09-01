"""Tests for the deterministic classifier."""

from src.classifier.mapper import ClassifierMapper
from src.classifier.taxonomy import FailureClass

mapper = ClassifierMapper()


def test_insufficient_funds_mapping() -> None:
    fc, retryable = mapper.classify("BAD_REQUEST_ERROR", error_reason="insufficient_funds")
    assert fc == FailureClass.INSUFFICIENT_FUNDS
    assert retryable is True


def test_issuer_decline_mapping() -> None:
    fc, retryable = mapper.classify("GATEWAY_ERROR", error_reason="issuer_down")
    assert fc == FailureClass.BANK_DOWNTIME  # issuer_down maps to bank_downtime


def test_3ds_dropoff_mapping() -> None:
    fc, _ = mapper.classify(
        "BAD_REQUEST_ERROR", error_reason="invalid_otp",
        error_step="payment_authentication"
    )
    assert fc == FailureClass.THREEDS_DROPOFF


def test_bank_downtime_mapping() -> None:
    fc, retryable = mapper.classify("GATEWAY_ERROR", error_reason="bank_technical_error")
    assert fc == FailureClass.BANK_DOWNTIME
    assert retryable is True


def test_fraud_block_mapping() -> None:
    fc, retryable = mapper.classify("BAD_REQUEST_ERROR", error_reason="payment_risk_check_failed")
    assert fc == FailureClass.FRAUD_BLOCK
    assert retryable is False


def test_hard_decline_non_retryable() -> None:
    fc, retryable = mapper.classify("BAD_REQUEST_ERROR", error_reason="card_stolen")
    assert fc == FailureClass.HARD_DECLINE
    assert retryable is False


def test_unknown_code_returns_unknown() -> None:
    fc, retryable = mapper.classify("TOTALLY_NEW_ERROR", error_reason="never_seen_before")
    assert fc == FailureClass.UNKNOWN
    assert retryable is False


def test_retryable_property() -> None:
    assert FailureClass.NETWORK_ERROR.is_retryable is True
    assert FailureClass.BANK_DOWNTIME.is_retryable is True
    assert FailureClass.HARD_DECLINE.is_retryable is False
    assert FailureClass.FRAUD_BLOCK.is_retryable is False
    assert FailureClass.CUSTOMER_CANCELLED.is_retryable is False


def test_customer_cancelled_mapping() -> None:
    fc, _ = mapper.classify("BAD_REQUEST_ERROR", error_reason="payment_cancelled")
    assert fc == FailureClass.CUSTOMER_CANCELLED


def test_expired_card_mapping() -> None:
    fc, _ = mapper.classify("BAD_REQUEST_ERROR", error_reason="card_expired")
    assert fc == FailureClass.EXPIRED_INSTRUMENT


# ── Razorpay decline-taxonomy audit, 2026-09-01 ──────────────────────────
#
# Pins the mappings added after auditing error_codes.yaml against Razorpay's
# own published reason list. Ten of their eighteen documented reasons matched
# no rule by name; two of those reached no rule at all and were abandoned
# without an attempt. Each test below names the consequence of the gap it
# closes, because "add a YAML row" is not self-evidently worth a test and
# these very much are.
#
# The full table and the reasoning live in docs/decline-taxonomy.md.

import yaml  # noqa: E402

from src.classifier.mapper import _YAML_PATH  # noqa: E402


def test_a_plain_bank_decline_is_chased_rather_than_abandoned() -> None:
    """
    THE expensive gap. card_declined and payment_declined are the most
    generic declines the card rail produces, and both arrive with
    error_source "bank" — which no catch-all covers, since those key on
    "customer" and "business". Both landed on UNKNOWN, which is
    non-retryable, so the case was closed without one attempt.
    """
    for reason in ("card_declined", "payment_declined"):
        fc, retryable = mapper.classify(
            "BAD_REQUEST_ERROR", error_source="bank",
            error_step="payment_authorization", error_reason=reason,
        )
        assert fc is FailureClass.ISSUER_DECLINE, reason
        assert retryable is True, reason


def test_an_expired_upi_collect_is_not_called_a_bank_refusal() -> None:
    """
    Razorpay's real string is payment_collect_request_expired; the rule that
    existed keyed on "upi_collect_timeout", which appears nowhere in their
    docs. The customer catch-all absorbed it into issuer_decline, so the page
    explained an expired UPI request as "your bank declined the payment" and
    then recommended UPI — to someone whose UPI request had just expired.
    """
    fc, retryable = mapper.classify(
        "BAD_REQUEST_ERROR", error_source="customer",
        error_step="payment_authorization",
        error_reason="payment_collect_request_expired",
    )
    assert fc is FailureClass.UPI_COLLECT_TIMEOUT
    assert retryable is True


def test_a_card_not_enrolled_in_3ds_is_told_the_truth_about_otp() -> None:
    """Absorbed into issuer_decline before, so the page blamed the bank for
    what is a 3DS-enrolment problem. Both are UPI-recommended, so the button
    was right and only the explanation was wrong."""
    fc, retryable = mapper.classify(
        "BAD_REQUEST_ERROR", error_source="customer",
        error_step="payment_authentication", error_reason="card_not_enrolled",
    )
    assert fc is FailureClass.THREEDS_DROPOFF
    assert retryable is True


def test_authentication_failed_classifies_without_the_step() -> None:
    """The precise step-scoped rule still wins; this is the fallback for a
    payload that names the reason and not the step."""
    with_step, _ = mapper.classify(
        "BAD_REQUEST_ERROR", error_step="payment_authentication",
        error_reason="authentication_failed",
    )
    bare, retryable = mapper.classify("", error_reason="authentication_failed")
    assert with_step is bare is FailureClass.THREEDS_DROPOFF
    assert retryable is True


def test_razorpays_business_failures_are_hard_declines() -> None:
    """Razorpay marks all four non-retryable, and retrying an identical bad
    request reproduces an identical refusal."""
    for reason in (
        "input_validation_failed",
        "international_transaction_not_allowed",
        "invalid_amount",
        "invalid_currency",
    ):
        fc, retryable = mapper.classify(
            "BAD_REQUEST_ERROR", error_source="business",
            error_step="payment_initiation", error_reason=reason,
        )
        assert fc is FailureClass.HARD_DECLINE, reason
        assert retryable is False, reason


def test_gateway_technical_errors_are_transient() -> None:
    for reason in ("gateway_technical_error", "payment_failed", "server_error"):
        fc, retryable = mapper.classify("", error_reason=reason)
        assert fc is FailureClass.NETWORK_ERROR, reason
        assert retryable is True, reason


def test_every_documented_razorpay_reason_is_matched_by_name() -> None:
    """
    The guard that would have caught this whole class of bug.

    Probed with an empty error_code and no source/step, so ONLY rules keyed
    on error_reason can fire. A realistic 5-tuple would prove nothing: the
    low-priority catch-alls swallow anything unmatched into issuer_decline or
    network_error, which is exactly how ten reasons hid in plain sight while
    the file looked complete.
    """
    data = yaml.safe_load(_YAML_PATH.read_text())
    acknowledged = data.get("razorpay_unmapped_deliberately", {})
    documented = data.get("razorpay_documented", {})
    assert documented, "the published reference list went missing"

    unmapped = [
        reason
        for reason in documented
        if mapper.classify("", error_reason=reason)[0] is FailureClass.UNKNOWN
        and reason not in acknowledged
    ]
    assert not unmapped, (
        f"{unmapped} classify as UNKNOWN, which is non-retryable — cases "
        "carrying them are abandoned without an attempt"
    )


def test_the_deliberate_disagreements_stay_deliberate() -> None:
    """
    Two mappings knowingly contradict Razorpay's retry verdict. Neither is an
    accident, and neither should change by accident either — this fails if
    someone flips one without going through docs/decline-taxonomy.md.
    """
    # The customer said stop. Respecting that outranks a recoverable rupee.
    fc, retryable = mapper.classify("", error_reason="payment_cancelled")
    assert fc is FailureClass.CUSTOMER_CANCELLED and retryable is False

    # Fraud-adjacent, so the conservative reading stands until it is a
    # product decision rather than a mapping one.
    fc, retryable = mapper.classify("", error_reason="payment_risk_check_failed")
    assert fc is FailureClass.FRAUD_BLOCK and retryable is False
