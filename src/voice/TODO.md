# Voice Agent — To-Do List

The Hinglish voice recovery agent (src/voice/) is built, tested, and live-wired,
but it is a brain without a mouth. This list takes it from "verified in tests"
to "making real calls a native speaker would trust."

## 1. Connect a real mouth (before anything else)

- [x] **Pick a telephony provider.** DONE: Plivo for the call leg + Sarvam
      (saaras:v3 STT, bulbul:v3 TTS) for Hinglish — implemented in
      src/voice/plivo_bridge.py, runbook in docs/VOICE_CALL_SETUP.md.
- [ ] **Create the Plivo account**, buy/verify an Indian caller number
      (DoT/TCPB KYC), note AUTH ID/TOKEN → PLIVO_AUTH_ID / PLIVO_AUTH_TOKEN.
- [x] **Write the provider adapter** — src/voice/plivo_bridge.py maps
      Plivo's form fields (CallUUID, RecordingUrl, CallStatus) onto the
      bridge state; /voice/turn keeps accepting both spellings.
- [x] **Signing** — every /plivo/* callback is HMAC-checked over the raw
      body with VOICE_WEBHOOK_SECRET (same secret, same construction, as
      /voice/turn); GetInput action URLs carry a per-call+turn signature.
      NOTE: Plivo cannot set X-Voice-Signature natively — sign at the
      proxy in front of the callbacks (docs/VOICE_CALL_SETUP.md §3).
- [ ] **One live call round**: `python scripts/run_plivo_bridge.py --dry-run`
      then `--once`, call your own phone, ask the 8 FAQ questions, note
      where the flow breaks. This is the first real evidence.

## 2. Compliance before real customers

- [ ] **DoT/TCPB registration** for the calling number and route
      (merchant-side duty; the engine cannot do this).
- [ ] **AI disclosure at call start** — the greeting must state it is an
      automated assistant. Update `dialogue.py:GREETING` to include it
      (currently it says "recovery assistant" but not explicitly "automated").
- [ ] **TRAI DLT template registration** for any SMS the voice flow triggers
      (e.g., the payment-link SMS sent during a call).
- [ ] **Record the consent trail**: every voice opt-out must be auditable
      (the case_events hash chain already covers the DB write; confirm the
      provider's call recording/log retention policy matches DPDP needs).

## 3. Language quality — a native speaker, not a model

- [ ] **Native review of the Hinglish FAQ** (`knowledge.py:_faq_chunks`) —
      the same caveat `src/customer/i18n.py` carries: "machine-grade
      phrasing undermines the trust this page exists to build."
- [ ] **Native review of response templates** (`dialogue.py`): opt-out
      confirmation, injection refusal, abstention, greeting, hangup.
- [ ] **Devanagari mirror expansion**: currently only the amount FAQ and
      opt-out/injection lexicons carry Devanagari. Mirror the remaining 6
      FAQ chunks so Devanagari-first callers get equal retrieval quality.
- [ ] **Transliteration handling**: real STT output will be inconsistent
      ("kitna"/"कितना"/"ktna"). Decide whether to add a transliteration
      normalizer to `tokenize()` or expand the code-switch relaxation.

## 4. Fluency — turn on the LLM path (measured, not assumed)

- [ ] **A/B the extractive vs LLM reply on the same 8 FAQ questions**:
      extractive is accurate but joins two passages verbatim (long for TTS);
      `VOICE_LLM_ENABLED=true` rephrases with the same grounding gate.
- [ ] **Set per-call token/latency budget**: the LLM path currently uses
      `max_tokens=300` and the global `LLM_TIMEOUT_SECONDS=30` — voice needs
      a much tighter turn budget (e.g., 2-3s). Consider a voice-specific
      timeout setting.
- [ ] **Measure grounding-failure rate on the LLM path**: the numeric gate
      must stay at 0 failures on money amounts. If a rephrase trades Hindi
      words for numbers, the gate abstains — track abstention rate.
- [ ] **Only then flip `VOICE_LLM_ENABLED=true` in production.**

## 5. Retrieval quality — the measured upgrade path

- [ ] **Swap `embed()` for `multilingual-e5-small`** (sentence-transformers,
      384d) once the corpus grows past a few hundred chunks or hashed
      retrieval misses real questions. This is the exact upgrade the Mic RAG
      repo designed for: same signatures, `EMBEDDER` env switch.
- [ ] **Build a real calibration set**: 50+ real call transcripts
      (from step 1's live round), hand-label which chunk should answer each,
      measure recall@3. The current floor (0.18 Roman / 0.08 Devanagari)
      is a placeholder borrowed from Mic RAG's measurement, not ours.
- [ ] **Add the case-events corpus**: recovery history (past attempts,
      nudges, outcomes) is retrievable context the agent currently ignores.

## 6. Production hardening (after live rounds)

- [ ] **Rate limit `/voice/turn`** per session/IP — currently only
      signature-gated. A leaked provider key would allow unbounded calls.
- [ ] **Call-context logging**: log turn intent + cited chunk id (never
      transcripts with PII) to the audit chain for compliance review.
- [ ] **Hangup/handoff policy**: define what happens after 2 abstentions
      (transfer to human? send SMS link? hang up politely?). Currently the
      caller can loop on abstain.
- [ ] **Load test the webhook**: voice turns are latency-critical (the
      caller is standing there). Measure p95 turn time; the extractive path
      is ms-fast, the LLM path is the risk.
- [ ] **Remove the process-level corpus cache footgun**: `reset_corpus_cache()`
      exists for tests; document that settings changes need a process
      restart in production (or rebuild the corpus on a settings version
      bump).

## Done is when

A real customer, called through a real provider, hears a native-reviewed
Hinglish reply, states an opt-out that verifiably closes their cases, and
every number spoken is grounded in their real case facts — and the p95 turn
latency and abstention rate are written down.
