"""
Hinglish voice recovery agent — the voice surface of the payment recovery
engine.

Architecture ported from the Mic RAG model (four gates, extractive floor,
grounding verify, offline-first retriever) and re-grounded on this product's
own truth: policy.yaml bounds, the FailureClass taxonomy, case facts, and a
Hinglish FAQ. See pipeline.py for the turn contract and knowledge.py for the
corpus. No telephony dependency: providers POST transcripts to
/voice/turn and read the reply out with their own TTS.
"""
