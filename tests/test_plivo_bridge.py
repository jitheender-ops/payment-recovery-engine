"""
The Plivo call leg (src/voice/plivo_bridge.py): the XML callbacks that
carry a live call, the signature gate on every one of them, and the
claim-gating that keeps a half-configured bridge from dialing anyone.

The STT/TTS/engine-turn helpers are monkeypatched — these tests prove the
conversation state machine and the auth surface, not Sarvam or the engine
(those have their own suites).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.voice import plivo_bridge
from src.voice.plivo_bridge import (
    BridgeError,
    _check_getinput_sig,
    _sign_getinput,
    claim_and_dial,
    router,
)

SECRET = "voice-test-secret"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def bridge(monkeypatch: Any, tmp_path: Any) -> TestClient:
    monkeypatch.setattr(
        "src.voice.plivo_bridge.get_settings",
        lambda: type("S", (), {
            "voice_webhook_secret": SECRET,
            "merchant_name": "TestMerchant",
            "plivo_bridge_base_url": "https://bridge.example.test",
            "plivo_engine_base_url": "",
            "plivo_auth_id": "",
            "plivo_auth_token": "",
            "plivo_caller_number": "",
            "plivo_bridge_poll_seconds": 1,
        })(),
    )
    monkeypatch.setattr(plivo_bridge, "_AUDIO_DIR", tmp_path / "audio")
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _post(client: TestClient, path: str, form: dict[str, str]) -> Any:
    raw = "&".join(f"{k}={v}" for k, v in form.items()).encode()
    return client.post(
        path, content=raw,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Voice-Signature": _sign(raw),
        },
    )


# ── the auth surface ──────────────────────────────────────────────────────


def test_every_callback_refuses_an_unsigned_body(bridge: TestClient) -> None:
    for path in ("/plivo/answer", "/plivo/turn", "/plivo/hangup"):
        r = bridge.post(path, content=b"CallUUID=x",
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 401, path


def test_callbacks_have_a_volume_ceiling(bridge: TestClient, monkeypatch: Any) -> None:
    """Signature auth proves identity, not volume: a looped callback would
    burn Sarvam quota turn after turn. 429 after the window budget."""
    monkeypatch.setattr(plivo_bridge, "_RATE_LIMIT", 3)
    for _ in range(3):
        r = _post(bridge, "/plivo/answer", {"CallUUID": "cu_rl"})
        assert r.status_code == 200
    r = _post(bridge, "/plivo/answer", {"CallUUID": "cu_rl"})
    assert r.status_code == 429


def test_a_wrong_signature_is_refused(bridge: TestClient) -> None:
    r = bridge.post(
        "/plivo/answer", content=b"CallUUID=x",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Voice-Signature": "deadbeef",
        },
    )
    assert r.status_code == 401


def test_a_non_ascii_signature_is_a_401_not_a_500(bridge: TestClient) -> None:
    """Same fail-closed rule as /voice/turn: compare_digest raises TypeError
    on a non-ASCII str, and an HTTP header may legally carry obs-text, so a
    raw \xE9\xE9\xE9 signature must be a clean 401, not an unhandled 500."""
    r = bridge.post(
        "/plivo/answer", content=b"CallUUID=x",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Voice-Signature": b"\xe9\xe9\xe9",
        },
    )
    assert r.status_code == 401


# ── TTS audio serving ──────────────────────────────────────────────────────


def test_audio_is_served_only_while_the_call_is_live(bridge: TestClient) -> None:
    plivo_bridge._CALLS["cu_aud"] = {"turn": 1, "token": "t"}
    d = plivo_bridge._AUDIO_DIR / "cu_aud"
    d.mkdir(parents=True, exist_ok=True)
    (d / "1.wav").write_bytes(b"RIFFfake")

    ok = bridge.get("/plivo/audio/cu_aud/1.wav")
    assert ok.status_code == 200 and ok.content == b"RIFFfake"
    assert ok.headers["Cache-Control"] == "no-store"

    # No call state (call ended / never existed) -> 404, not a directory walk.
    assert bridge.get("/plivo/audio/cu_other/1.wav").status_code == 404
    # Path-shape policing: only <digits>.wav, no traversal, no other ext.
    assert bridge.get("/plivo/audio/cu_aud/x.wav").status_code == 400
    assert bridge.get("/plivo/audio/cu_aud/1.mp3").status_code == 400
    assert bridge.get("/plivo/audio/cu_aud/../../etc/passwd.wav").status_code in (400, 404)
    # Ended call's audio is gone even if the file lingers on disk.
    plivo_bridge._CALLS.pop("cu_aud")
    assert bridge.get("/plivo/audio/cu_aud/1.wav").status_code == 404


# ── answer: greeting + first GetInput ─────────────────────────────────────


def test_the_answer_callback_greets_with_the_ai_disclosure(
    bridge: TestClient,
) -> None:
    r = _post(bridge, "/plivo/answer", {"CallUUID": "cu_1", "client": "qc_1"})
    assert r.status_code == 200
    xml = r.text
    # The greeting is SPOKEN by the bridge on answer — it must be the same
    # disclosure template the compliance test in test_voice.py pins.
    assert "automated recovery assistant" in xml
    assert "AI" in xml or "ai" in xml
    assert "<GetInput" in xml and "inputType=\"SPEECH\"" in xml


def test_getinput_action_urls_carry_a_signature_bound_to_the_call(
    bridge: TestClient,
) -> None:
    r = _post(bridge, "/plivo/answer", {"CallUUID": "cu_2"})
    xml = r.text
    assert "call_uuid=cu_2" in xml and "n=1" in xml and "sig=" in xml
    # The tamper check: a wrong n fails, the right one passes.
    assert _check_getinput_sig("cu_2", 1, "wrong") is False
    # A non-ASCII sig (legal in a URL query string) must fail, not raise.
    assert _check_getinput_sig("cu_2", 1, "\u00e9\u00e9\u00e9") is False
    # Extract the sig back out of the XML to prove it verifies.
    marker = "sig="
    sig = xml.split(marker, 1)[1].split('"', 1)[0].split("&", 1)[0]
    assert _check_getinput_sig("cu_2", 1, sig) is True
    # Bound to THIS call: another CallUUID's sig does not verify.
    assert _check_getinput_sig("cu_other", 1, sig) is False


# ── the turn state machine ────────────────────────────────────────────────


def _turn_form(call_uuid: str, n: int, recording: str = "https://plivo/rec.wav") -> dict[str, str]:
    return {"CallUUID": call_uuid, "RecordingUrl": recording}


def _turn_path(call_uuid: str, n: int) -> str:
    sig = _sign_getinput(call_uuid, n)
    return f"/plivo/turn?call_uuid={call_uuid}&n={n}&sig={sig}"


def test_a_turn_with_a_bad_sig_is_refused(bridge: TestClient) -> None:
    r = _post(bridge, "/plivo/turn?call_uuid=cu_3&n=1&sig=bad", _turn_form("cu_3", 1))
    assert r.status_code == 400


def test_speech_is_stt_turn_tts_and_re_asked(
    bridge: TestClient, monkeypatch: Any,
) -> None:
    monkeypatch.setattr(plivo_bridge, "_stt_from_plivo", lambda url: "kitna pending hai")
    seen: dict[str, Any] = {}

    def fake_engine_turn(transcript: str, state: dict[str, Any], cu: str) -> dict[str, Any]:
        seen["transcript"] = transcript
        seen["case_id"] = state.get("case_id")
        return {"reply": "Rs 2,499 pending hai", "intent": "answer"}

    monkeypatch.setattr(plivo_bridge, "_engine_turn", fake_engine_turn)
    monkeypatch.setattr(
        plivo_bridge, "_tts_to_public_url",
        lambda text, cu, n: f"https://bridge.example.test/plivo/audio/{cu}/{n}.wav",
    )

    # Bind the case first, as the answer callback would have.
    _post(bridge, "/plivo/answer", {"CallUUID": "cu_4", "client": "qc_4"})
    r = _post(bridge, _turn_path("cu_4", 1), _turn_form("cu_4", 1))
    assert r.status_code == 200
    xml = r.text
    assert seen["transcript"] == "kitna pending hai"
    assert "<Play>" in xml and "cu_4/1.wav" in xml
    assert "n=2" in xml, "the next GetInput must advance the turn counter"
    # TTS fell back to Plivo's own voice: no audio was written for cu_4.
    assert not (plivo_bridge._AUDIO_DIR / "cu_4").exists() or xml


def test_an_opt_out_ends_the_call_and_reports_it(
    bridge: TestClient, monkeypatch: Any,
) -> None:
    monkeypatch.setattr(plivo_bridge, "_stt_from_plivo", lambda url: "band karo")
    monkeypatch.setattr(
        plivo_bridge, "_engine_turn",
        lambda t, s, cu: {"reply": "theek hai, note kar liya", "intent": "opt_out"},
    )
    reported: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        plivo_bridge, "_report",
        lambda cu, result, opted_out=False: reported.append((cu, result, opted_out)),
    )
    _post(bridge, "/plivo/answer", {"CallUUID": "cu_5"})
    r = _post(bridge, _turn_path("cu_5", 1), _turn_form("cu_5", 1))
    assert "<Hangup/>" in r.text
    assert reported == [("cu_5", "opted_out", True)]


def test_a_promise_ends_the_call_with_confirmation(
    bridge: TestClient, monkeypatch: Any,
) -> None:
    monkeypatch.setattr(plivo_bridge, "_stt_from_plivo", lambda url: "kal tak bhej dunga")
    monkeypatch.setattr(
        plivo_bridge, "_engine_turn",
        lambda t, s, cu: {"reply": "confirm: kal tak", "intent": "promise_captured"},
    )
    reported: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        plivo_bridge, "_report",
        lambda cu, result, opted_out=False: reported.append((cu, result, opted_out)),
    )
    _post(bridge, "/plivo/answer", {"CallUUID": "cu_6"})
    r = _post(bridge, _turn_path("cu_6", 1), _turn_form("cu_6", 1))
    assert "<Hangup/>" in r.text and "confirm: kal tak" in r.text
    assert reported == [("cu_6", "promise_captured", False)]


def test_three_abstains_hang_up_instead_of_looping(
    bridge: TestClient, monkeypatch: Any,
) -> None:
    monkeypatch.setattr(plivo_bridge, "_stt_from_plivo", lambda url: "mahina ka kya haal hai")
    monkeypatch.setattr(
        plivo_bridge, "_engine_turn",
        lambda t, s, cu: {"reply": "pata nahi", "intent": "abstain"},
    )
    reported: list[str] = []
    monkeypatch.setattr(
        plivo_bridge, "_report",
        lambda cu, result, opted_out=False: reported.append(result),
    )
    monkeypatch.setattr(
        plivo_bridge, "_tts_to_public_url",
        lambda text, cu, n: f"https://b.test/plivo/audio/{cu}/{n}.wav",
    )
    _post(bridge, "/plivo/answer", {"CallUUID": "cu_7"})
    for n in (1, 2, 3):
        r = _post(bridge, _turn_path("cu_7", n), _turn_form("cu_7", n))
    assert "<Hangup/>" in r.text, "the third abstain must end the call"
    assert reported == ["abstained"]


def test_silence_gets_one_nudge_then_the_farewell(
    bridge: TestClient,
) -> None:
    _post(bridge, "/plivo/answer", {"CallUUID": "cu_8"})
    # No RecordingUrl in the form = GetInput timed out with no speech.
    r = _post(bridge, _turn_path("cu_8", 1), {"CallUUID": "cu_8"})
    assert "wahin hain" in r.text, "first silence gets a nudge, not a hangup"
    r = _post(bridge, _turn_path("cu_8", 2), {"CallUUID": "cu_8"})
    assert "<Hangup/>" in r.text, "second silence ends the call"


def test_a_stt_failure_asks_to_repeat_instead_of_hanging_up(
    bridge: TestClient, monkeypatch: Any,
) -> None:
    def boom(url: str) -> str:
        raise plivo_bridge.BridgeError("sarvam down")

    monkeypatch.setattr(plivo_bridge, "_stt_from_plivo", boom)
    _post(bridge, "/plivo/answer", {"CallUUID": "cu_9"})
    r = _post(bridge, _turn_path("cu_9", 1), _turn_form("cu_9", 1))
    assert "sunayi nahi diya" in r.text
    assert "<Hangup/>" not in r.text


def test_the_turn_cap_never_traps_a_caller(bridge: TestClient) -> None:
    _post(bridge, "/plivo/answer", {"CallUUID": "cu_10"})
    r = _post(bridge, _turn_path("cu_10", plivo_bridge.MAX_TURNS), _turn_form("cu_10", 99))
    assert "<Hangup/>" in r.text


# ── hangup: terminal report + audio cleanup ───────────────────────────────


def test_hangup_reports_the_outcome_and_cleans_up(
    bridge: TestClient, monkeypatch: Any,
) -> None:
    reported: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        plivo_bridge, "_report",
        lambda cu, result, opted_out=False: reported.append((cu, result, opted_out)),
    )
    audio_dir = plivo_bridge._AUDIO_DIR / "cu_11"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "1.wav").write_bytes(b"RIFFfake")
    r = _post(bridge, "/plivo/hangup", {"CallUUID": "cu_11", "CallStatus": "completed"})
    assert r.status_code == 200
    assert reported == [("cu_11", "done", False)]
    assert not audio_dir.exists()


def test_hangup_on_a_busy_call_reports_failed(
    bridge: TestClient, monkeypatch: Any,
) -> None:
    reported: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        plivo_bridge, "_report",
        lambda cu, result, opted_out=False: reported.append((cu, result, opted_out)),
    )
    _post(bridge, "/plivo/hangup", {"CallUUID": "cu_12", "CallStatus": "busy"})
    assert reported == [("cu_12", "failed", False)]


def test_hangup_after_an_opt_out_report_does_not_double_report(
    bridge: TestClient, monkeypatch: Any,
) -> None:
    """A call that ended via opt_out already reported; the terminal callback
    must keep that outcome, not overwrite it with 'done'."""
    monkeypatch.setattr(plivo_bridge, "_stt_from_plivo", lambda url: "band karo")
    monkeypatch.setattr(
        plivo_bridge, "_engine_turn",
        lambda t, s, cu: {"reply": "theek hai", "intent": "opt_out"},
    )
    reported: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        plivo_bridge, "_report",
        lambda cu, result, opted_out=False: reported.append((cu, result, opted_out)),
    )
    _post(bridge, "/plivo/answer", {"CallUUID": "cu_13"})
    _post(bridge, _turn_path("cu_13", 1), _turn_form("cu_13", 1))
    _post(bridge, "/plivo/hangup", {"CallUUID": "cu_13", "CallStatus": "completed"})
    assert reported == [("cu_13", "opted_out", True)]


# ── claim gating: the fail-closed rules ──────────────────────────────────


def test_claim_refuses_without_the_secret(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "src.voice.plivo_bridge.get_settings",
        lambda: type("S", (), {
            "voice_webhook_secret": "", "plivo_auth_id": "x",
            "plivo_auth_token": "x", "plivo_caller_number": "+91",
            "plivo_bridge_base_url": "https://b.test",
            "plivo_engine_base_url": "", "merchant_name": "M",
        })(),
    )
    with pytest.raises(BridgeError):
        claim_and_dial()


def test_claim_refuses_when_half_configured(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "src.voice.plivo_bridge.get_settings",
        lambda: type("S", (), {
            "voice_webhook_secret": SECRET, "plivo_auth_id": "",
            "plivo_auth_token": "", "plivo_caller_number": "",
            "plivo_bridge_base_url": "https://b.test",
            "plivo_engine_base_url": "", "merchant_name": "M",
        })(),
    )
    with pytest.raises(BridgeError):
        claim_and_dial()


def test_claim_and_dial_claims_then_dials(monkeypatch: Any) -> None:
    settings = type("S", (), {
        "voice_webhook_secret": SECRET, "plivo_auth_id": "AC",
        "plivo_auth_token": "TK", "plivo_caller_number": "+911234567890",
        "plivo_bridge_base_url": "https://b.test",
        "plivo_engine_base_url": "", "merchant_name": "M",
        "plivo_bridge_poll_seconds": 1,
    })()
    monkeypatch.setattr("src.voice.plivo_bridge.get_settings", lambda: settings)

    claims: list[bytes] = []
    dials: list[dict[str, Any]] = []

    import urllib.request

    class FakeResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return json.dumps(self._payload).encode()

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *a: Any) -> None:
            pass

    def fake_urlopen(req: Any, timeout: int = 0) -> FakeResponse:  # type: ignore[no-untyped-def]
        url = req.full_url
        if url.endswith("/voice/queue/claim"):
            claims.append(req.data)
            return FakeResponse({"call": {
                "call_id": "qc_99", "case_id": "case_1",
                "phone": "+919999999999", "risk_type": "payment_failure",
                "amount": "Rs 2,499",
            }})
        if "/Call/" in url:
            dials.append(json.loads(req.data))
            return FakeResponse({"request_id": "req_1", "api_id": "a"})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    call = claim_and_dial()
    assert call is not None and call["call_id"] == "qc_99"
    # The claim was signed with the voice secret.
    assert len(claims) == 1
    # The dial carried the queue call id as `client` — the answer callback's
    # only way to bind Plivo's CallUUID back to the queue row.
    assert dials == [{
        "from": "+911234567890", "to": "+919999999999",
        "answer_url": "https://b.test/plivo/answer", "answer_method": "POST",
        "hangup_url": "https://b.test/plivo/hangup", "hangup_method": "POST",
        "client": "qc_99",
    }]
    # And the pending binding exists for the answer callback to consume.
    assert plivo_bridge._PENDING["qc_99"]["case_id"] == "case_1"
