# Voice Call Setup — Plivo + Sarvam

How to take the Hinglish voice agent (src/voice/) from "verified in tests"
to "making real calls". The engine never dials a phone: `voice_call_queue`
rows are claimed by a bridge process (src/voice/plivo_bridge.py) that drives
the call through Plivo, with Sarvam saaras:v3 for STT and bulbul:v3 for TTS.

## Architecture

```
scheduler/orchestrator          Plivo                 Sarvam
   voice_call_queue row  ──►   bridge claims row
   (state=queued)       │       │
                        │       └─ dial (REST, basic auth)
                        │
                        ▼
                 Plivo calls customer
                        │
        ┌───────────────┴────────────────┐
        ▼                                ▼
  POST /plivo/answer            POST /plivo/turn (per turn)
  Speak AI-disclosure           download recording → Sarvam STT
  + GetInput (SPEECH)           → POST /voice/turn (signed, grounded)
                                 → Sarvam TTS → <Play> → GetInput
                        │
                        ▼
                 POST /plivo/hangup → report outcome to /voice/queue/report
```

Every number the agent can speak passes the pipeline's numeric grounding
gate (src/voice/pipeline.py) — the bridge only ferries bytes.

## 1. Accounts

1. **Plivo** (console.plivo.com): sign up, buy an Indian mobile number,
   note AUTH ID and AUTH TOKEN. Outbound India calling also needs your
   number KYC-verified for Indian regulations.
2. **Sarvam AI** (dashboard.sarvam.ai): API key (already used by the
   browser demo; the live call reuses it).

## 2. Environment (.env)

```
VOICE_WEBHOOK_SECRET=<generate: openssl rand -hex 32>
VOICE_CHASER_ENABLED=true          # queues calls after a successful nudge
PLIVO_AUTH_ID=...
PLIVO_AUTH_TOKEN=...
PLIVO_CALLER_NUMBER=+91XXXXXXXXXX  # the bought number, E.164
PLIVO_BRIDGE_BASE_URL=https://<public-host-of-this-app>
# Optional, only if the bridge runs beside a separate engine process:
# PLIVO_ENGINE_BASE_URL=https://<engine-host>
```

All of them fail closed: an unset VOICE_WEBHOOK_SECRET closes every
callback AND the bridge refuses to run half-configured (tested).

## 3. Plivo application wiring

Plivo needs no per-application XML app for API-originated calls — the
`answer_url`/`hangup_url` passed at dial time carry the callbacks. But the
**signing**: Plivo cannot natively send `X-Voice-Signature`, so put an
HMAC signing layer in front (nginx/OpenResty or a tiny proxy) that signs
Plivo's callback bodies with VOICE_WEBHOOK_SECRET before forwarding to
`/plivo/*`. Alternative if you cannot sign at the proxy: restrict the
`/plivo/*` surface by source IP to Plivo's published callback ranges AND
keep the GetInput action signatures (they are HMAC-bound per call+turn
inside the URLs the bridge itself generates).

## 4. Run it

```bash
python scripts/run_plivo_bridge.py --dry-run   # proves the env is complete
python scripts/run_plivo_bridge.py --once      # dials at most one call
python scripts/run_plivo_bridge.py             # the polling worker
```

The worker claims one queued call at a time (`POST /voice/queue/claim`,
signed), dials, then paces 60s between calls — a recovery blitz reads as
spam on the customer's phone.

To make calls actually queue: a chase must fire a successful
`nudge_customer` with a customer phone on file, and
`VOICE_CHASER_ENABLED=true`. In demo mode, `scripts/seed_bulk.py`
provides fake cases with contacts.

## 5. First live round (the TODO's checklist item)

Call your own phone. Verify, in order:

- [ ] The greeting states it is an automated AI assistant (first sentence).
- [ ] A spoken "band karo" ends the call AND the cases close (check
      `case_events` for the opt-out audit row).
- [ ] "kitna pending hai" answers with the case's real amount and the
      reply's numbers are the row's numbers.
- [ ] Silence gets one nudge, then a polite hangup.
- [ ] A promise ("kal tak bhej dunga") is confirmed aloud with amount and
      date, recorded in `promises_to_pay`, and the call ends.
- [ ] p95 turn latency feels under ~3s end to end (STT + engine + TTS).
      The extractive path is ms-fast; if it lags, keep
      VOICE_LLM_ENABLED=false on calls.

## 6. Compliance before real customers (voice/TODO.md section 2)

- DoT/TCPB registration for the calling number (merchant-side duty).
- AI disclosure at call start — already the greeting's first sentence,
  pinned by tests/test_voice.py.
- TRAI DLT template registration for any SMS the flow triggers.
- Call-recording retention: the bridge downloads recordings for STT and
  deletes them immediately after transcription; confirm your Plivo account
  does not retain recordings longer than DPDP allows.

## 7. Ops notes

- Bridge state is in-process per CallUUID; a restart mid-call means Plivo
  fails the next XML fetch and closes the call — the queue row is freed by
  the hangup report (tested for the no-answer path too).
- TTS audio lives at `/tmp/voice_bridge_audio/<CallUUID>/<n>.wav` and is
  served at `/plivo/audio/...` only while the call is live; hangup deletes
  the directory (tested).
- MAX_TURNS (12) and the 3-abstain hangup are the anti-trap rails (tested).
- Launching uvicorn by hand (not via `./run.sh`)? Export
  `PYTHONUNBUFFERED=1` first: without it the scheduler's ticks and every
  INFO line sit in the pipe buffer, and a healthy engine looks frozen —
  the heartbeat in the DB keeps ticking while the log says nothing.
