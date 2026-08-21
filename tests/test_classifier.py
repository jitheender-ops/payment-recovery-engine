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
