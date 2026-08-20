"""Tests for HMAC-SHA256 signature verification."""

import hashlib
import hmac
import json

from src.ingestion.signature import verify_webhook_signature


def test_valid_signature_passes(webhook_secret, signed_payload):
    raw_body, signature = signed_payload
    assert verify_webhook_signature(raw_body, signature, webhook_secret) is True


def test_invalid_signature_rejected(webhook_secret, signed_payload):
    raw_body, _ = signed_payload
    assert verify_webhook_signature(raw_body, "invalid_sig", webhook_secret) is False


def test_empty_signature_rejected(webhook_secret, signed_payload):
    raw_body, _ = signed_payload
    assert verify_webhook_signature(raw_body, "", webhook_secret) is False


def test_modified_body_rejected(webhook_secret, signed_payload):
    _, signature = signed_payload
    modified = b'{"tampered": true}'
    assert verify_webhook_signature(modified, signature, webhook_secret) is False


def test_different_secret_rejected(signed_payload):
    raw_body, signature = signed_payload
    assert verify_webhook_signature(raw_body, signature, "wrong_secret") is False


def test_unicode_body_handling():
    body = '{"name": "राजपे टेस्ट"}'.encode("utf-8")
    secret = "test_secret"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, sig, secret) is True
