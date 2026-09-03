"""
The merchant receivables API (POST /ar/cases/paid, /ar/cases/dispute,
/ar/tasks/done) — the B2B closure surface, at the HTTP level.

The service functions beneath it (record_external_payment,
resolve_dispute, complete_task) have their own tests in
test_receivables.py; what only the ROUTE can prove is the discipline
around them: the HMAC gate, the 400-vs-401 discrimination, the schema
rejections, and the closed-vocabulary outcomes mapped to status codes.
Same pattern as test_webhook_router.py — the router alone, over the
test database, everything else monkeypatched away.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.cases import open_case
from src.database import get_session
from src.merchant.receivables_api import router as ar_router
from src.models import RecoveryCase
from src.receivables.models import AccountTask
from src.receivables.tasks import raise_call_task

SECRET = "ar-test-secret"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> Any:
    monkeypatch.setattr(
        "src.merchant.receivables_api.get_settings",
        lambda: type("S", (), {"risk_webhook_secret": SECRET})(),
    )
    app = FastAPI()
    app.include_router(ar_router, prefix="/ar")

    async def override() -> Any:
        async with db_sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = override
    return TestClient(app)


def _post(
    client: TestClient, path: str, payload: dict[str, Any], *, sig: str | None = "valid"
) -> Any:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sig == "valid":
        headers["X-Risk-Signature"] = _sign(body)
    elif sig is not None:
        headers["X-Risk-Signature"] = sig
    return client.post(path, content=body, headers=headers)


async def _open_invoice(
    db_sessionmaker: async_sessionmaker[AsyncSession], ref: str
) -> Any:
    async with db_sessionmaker() as session:
        case = await open_case(
            session,
            risk_type="invoice_overdue",
            subject_ref=ref,
            amount_at_risk=100_000,
            customer_id="ar@acme.in",
        )
        await session.commit()
        return case


# ── The auth gate ──────────────────────────────────────────────────────────


async def test_every_endpoint_refuses_a_missing_signature(client: Any) -> None:
    # No case needed — the gate runs before anything touches the database.
    for path, payload in (
        ("/ar/cases/paid", {"case_id": str(uuid.uuid4()), "amount_paise": 100,
                            "paid_ref": "UTR1"}),
        ("/ar/cases/dispute", {"dispute_id": str(uuid.uuid4()),
                               "outcome": "upheld"}),
        ("/ar/tasks/done", {"task_id": str(uuid.uuid4())}),
    ):
        r = _post(client, path, payload, sig=None)
        assert r.status_code == 401, path


async def test_a_wrong_signature_is_refused(client: Any) -> None:
    r = _post(client, "/ar/tasks/done", {"task_id": str(uuid.uuid4())},
              sig="deadbeef")
    assert r.status_code == 401


async def test_valid_signature_over_invalid_json_is_a_400_not_a_401(
    client: Any,
) -> None:
    """The discrimination the merchant's client needs: a body that FAILED
    HMAC is an auth problem (401); a body that PASSED HMAC but is not JSON
    is their bug (400). Conflating them sends a client hunting the wrong
    secret."""
    raw = b"{not json"
    r = client.post(
        "/ar/tasks/done",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Risk-Signature": _sign(raw),
        },
    )
    assert r.status_code == 400


# ── POST /ar/cases/paid ────────────────────────────────────────────────────


async def test_a_signed_external_payment_closes_the_case_without_claiming_it(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The honesty rule at the HTTP boundary: money arrived by NEFT — the
    case closes, the amount counts, and recovered_via_attempt_id stays
    NULL so the engine's headline never claims a rupee it did not earn."""
    case = await _open_invoice(db_sessionmaker, "INV-P1")
    r = _post(client, "/ar/cases/paid", {
        "case_id": str(case.id),
        "amount_paise": 100_000,
        "paid_ref": "UTR-77",
        "method": "neft",
        "note": "bank slip verified",
    })
    assert r.status_code == 200
    assert "recorded" in r.text or r.text == ""

    async with db_sessionmaker() as reader:
        fresh = await reader.get(RecoveryCase, case.id)
        assert fresh is not None
        assert fresh.state == "recovered"
        assert fresh.amount_recovered == 100_000
        assert fresh.recovered_via_attempt_id is None, (
            "external money must never be claimed as engine-attributed"
        )
        assert fresh.recovered_ref == "UTR-77"


async def test_a_reposted_bank_ref_is_an_ack_never_a_second_rupee(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Idempotency per (case, ref) through the route: the merchant's client
    retries the POST; the money must count exactly once."""
    case = await _open_invoice(db_sessionmaker, "INV-P2")
    payload = {"case_id": str(case.id), "amount_paise": 100_000,
               "paid_ref": "UTR-88"}
    assert _post(client, "/ar/cases/paid", payload).status_code == 200
    r = _post(client, "/ar/cases/paid", payload)
    assert r.status_code == 200, "a re-POST is an ack, not an error"

    async with db_sessionmaker() as reader:
        fresh = await reader.get(RecoveryCase, case.id)
        assert fresh is not None
        assert fresh.amount_recovered == 100_000, "the ref double-counted"


async def test_external_money_on_a_closed_case_is_refused_not_guessed(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A ref arriving after the case closed is an overpayment-shaped
    anomaly for a human — the route must say so, not resolve it silently."""
    case = await _open_invoice(db_sessionmaker, "INV-P3")
    first = {"case_id": str(case.id), "amount_paise": 100_000,
             "paid_ref": "UTR-9"}
    assert _post(client, "/ar/cases/paid", first).status_code == 200
    second = {"case_id": str(case.id), "amount_paise": 50_000,
              "paid_ref": "UTR-10"}
    r = _post(client, "/ar/cases/paid", second)
    assert r.status_code in (409, 422), (
        f"a second DIFFERENT ref on a closed case got {r.status_code}"
    )


async def test_a_payment_for_an_unknown_case_is_a_clean_refusal(
    client: Any,
) -> None:
    """The closed vocabulary through the route: no such case → 409 with
    'refused_no_case' in the body, never a 500 and never a silent 200."""
    r = _post(client, "/ar/cases/paid", {
        "case_id": str(uuid.uuid4()), "amount_paise": 100, "paid_ref": "UTR-x",
    })
    assert r.status_code == 409
    assert "refused_no_case" in r.text


async def test_schema_violations_are_400s_not_500s(client: Any) -> None:
    """The validators (UUID shape, amount > 0, method enum, note length)
    must fail as clean 400s — an unvalidated field reaching the service
    layer is how a money surface 500s."""
    base = {"amount_paise": 100, "paid_ref": "UTR-v"}
    assert _post(client, "/ar/cases/paid", {**base, "case_id": "not-a-uuid"}
                 ).status_code == 400
    assert _post(client, "/ar/cases/paid", {
        **base, "case_id": str(uuid.uuid4()), "amount_paise": 0,
    }).status_code == 400
    assert _post(client, "/ar/cases/paid", {
        **base, "case_id": str(uuid.uuid4()), "amount_paise": -5,
    }).status_code == 400
    assert _post(client, "/ar/cases/paid", {
        **base, "case_id": str(uuid.uuid4()), "method": "carrier_pigeon",
    }).status_code == 400
    assert _post(client, "/ar/cases/paid", {
        **base, "case_id": str(uuid.uuid4()), "paid_ref": "",
    }).status_code == 400


# ── POST /ar/cases/dispute ────────────────────────────────────────────────


async def test_a_signed_dispute_verdict_flows_through(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from src.receivables import open_dispute

    async with db_sessionmaker() as session:
        case = await open_case(
            session, risk_type="invoice_overdue", subject_ref="INV-D1",
            amount_at_risk=80_000, customer_id="d1@acme.in",
        )
        dispute = await open_dispute(session, case, reason="qty wrong")
        await session.commit()
        assert dispute is not None

    r = _post(client, "/ar/cases/dispute", {
        "dispute_id": str(dispute.id), "outcome": "upheld",
        "note": "credit note issued",
    })
    assert r.status_code == 200
    async with db_sessionmaker() as reader:
        fresh = await reader.get(RecoveryCase, case.id)
        assert fresh is not None
        assert fresh.state == "abandoned", "upheld verdict closes the case"


async def test_a_dispute_verdict_for_an_unknown_dispute_is_404(
    client: Any,
) -> None:
    r = _post(client, "/ar/cases/dispute", {
        "dispute_id": str(uuid.uuid4()), "outcome": "rejected",
    })
    assert r.status_code == 404


async def test_an_outcome_outside_the_vocabulary_is_a_400(
    client: Any,
) -> None:
    """'maybe' is not a verdict. The Literal exists so merchant automation
    can branch safely; anything else must bounce at the schema."""
    r = _post(client, "/ar/cases/dispute", {
        "dispute_id": str(uuid.uuid4()), "outcome": "maybe",
    })
    assert r.status_code == 400


# ── POST /ar/tasks/done ────────────────────────────────────────────────────


async def test_a_signed_task_done_marks_it_done_and_stays_done(
    client: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as session:
        task = await raise_call_task(
            session,
            account_id=uuid.uuid4(),
            account_ref="ACME-1",
            detail={"outstanding": "₹800", "stage": "call"},
        )
        await session.commit()

    payload = {"task_id": str(task.id)}
    assert _post(client, "/ar/tasks/done", payload).status_code == 200
    # Idempotent at the route: done stays done, second POST is not an error.
    assert _post(client, "/ar/tasks/done", payload).status_code == 200

    async with db_sessionmaker() as reader:
        fresh = await reader.get(AccountTask, task.id)
        assert fresh is not None
        assert fresh.status == "done"
        assert fresh.done_at is not None


async def test_task_done_for_an_unknown_task_is_404(client: Any) -> None:
    r = _post(client, "/ar/tasks/done", {"task_id": str(uuid.uuid4())})
    assert r.status_code == 404
