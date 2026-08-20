"""
Fire test webhooks to the local FastAPI server.
Usage: python scripts/simulate_webhooks.py --count 20 --host http://localhost:8000
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
import uuid

import httpx


SAMPLE_FAILURES = [
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "insufficient_funds", "error_source": "customer", "error_step": "payment_authorization", "method": "card", "bank": "HDFC"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "invalid_otp", "error_source": "customer", "error_step": "payment_authentication", "method": "card", "bank": "SBI"},
    {"error_code": "GATEWAY_ERROR", "error_reason": "issuer_down", "error_source": "gateway", "error_step": "payment_authorization", "method": "netbanking", "bank": "PNB"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "payment_cancelled", "error_source": "customer", "error_step": "payment_authentication", "method": "upi", "bank": "ICICI"},
    {"error_code": "GATEWAY_ERROR", "error_reason": "bank_technical_error", "error_source": "gateway", "error_step": "payment_authorization", "method": "card", "bank": "Axis"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "card_expired", "error_source": "customer", "error_step": "payment_initiation", "method": "card", "bank": "HDFC"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "payment_risk_check_failed", "error_source": "razorpay", "error_step": "payment_authorization", "method": "card", "bank": "ICICI"},
    {"error_code": "SERVER_ERROR", "error_reason": "timeout", "error_source": "razorpay", "error_step": "payment_capture", "method": "upi", "bank": "SBI"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "upi_collect_timeout", "error_source": "customer", "error_step": "payment_authorization", "method": "upi", "bank": "Kotak"},
    {"error_code": "BAD_REQUEST_ERROR", "error_reason": "card_limit_exceeded", "error_source": "customer", "error_step": "payment_authorization", "method": "card", "bank": "HDFC"},
]


def make_payload(failure: dict, idx: int) -> dict:
    payment_id = f"pay_test_{uuid.uuid4().hex[:12]}"
    return {
        "entity": "event",
        "account_id": "acc_test123",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": (idx + 1) * 10000,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": f"order_test_{idx:04d}",
                    "method": failure["method"],
                    "bank": failure.get("bank"),
                    "email": "test@example.com",
                    "contact": "+919876543210",
                    "error_code": failure["error_code"],
                    "error_description": f"Test: {failure['error_reason']}",
                    "error_source": failure["error_source"],
                    "error_step": failure["error_step"],
                    "error_reason": failure["error_reason"],
                    "created_at": int(time.time()),
                }
            }
        },
        "created_at": int(time.time()),
    }


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Simulate Razorpay webhooks")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--host", type=str, default="http://localhost:8000")
    parser.add_argument("--secret", type=str, default="test_webhook_secret_123")
    args = parser.parse_args()

    print(f"🔄 Sending {args.count} test webhooks to {args.host}/webhooks/razorpay\n")

    with httpx.Client(timeout=10) as client:
        for i in range(args.count):
            failure = SAMPLE_FAILURES[i % len(SAMPLE_FAILURES)]
            payload = make_payload(failure, i)
            body = json.dumps(payload).encode()
            sig = sign_payload(body, args.secret)

            try:
                resp = client.post(
                    f"{args.host}/webhooks/razorpay",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": sig,
                    },
                )
                status = "✅" if resp.status_code == 200 else f"❌ {resp.status_code}"
                print(f"  [{i+1:2d}/{args.count}] {failure['error_reason']:30s} → {status}")
            except Exception as e:
                print(f"  [{i+1:2d}/{args.count}] {failure['error_reason']:30s} → ❌ {e}")

            time.sleep(0.2)

    print(f"\n✅ Done — sent {args.count} webhooks")


if __name__ == "__main__":
    main()
