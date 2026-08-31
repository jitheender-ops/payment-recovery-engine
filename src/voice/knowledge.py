"""
Knowledge base for the Hinglish voice recovery agent.

Ported from the Mic RAG model's retrieval shape (hashed bag-of-words
index, one chunk = one retrievable answer) and re-grounded on this
product's own truth. The corpus is small on purpose: the voice agent
answers questions about THIS customer's case, THIS product's bounds and
THIS regulator's rules — never general knowledge. Every chunk carries the
exact string the answer is allowed to use, so the grounding gate can
verify an answer against the chunk it claims to cite.

What is in the corpus and why:
  * policy facts    — mirrored from policy.yaml / src/chasers/policy.py
                      at import time, so bounds can never drift from what
                      the engine actually enforces
  * taxonomy        — the FailureClass set a diagnosis can name
  * FAQ             — the questions a customer actually asks on a
                      recovery call, hand-authored in code-mixed
                      Hindi/English (Hinglish), the way the question
                      arrives on the phone

Offline-first: the retriever is pure stdlib (hash + bag-of-words), the
same fallback the Mic RAG repo runs without its venv. An e5/sentence-
transformers upgrade swaps embed() and nothing else — the stage
signature is the ported contract.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

from src.chasers.policy import RISK_POLICIES
from src.classifier.taxonomy import FailureClass
from src.config import get_settings
from src.formatting import money

_WORD = re.compile(r"[\w\u0900-\u097F]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Roman + Devanagari words, lowercased, English + Hindi stop words kept
    Hinglish-specific: Hindi function words arrive in Roman script
    ("ka", "hai", "kaise"), so both scripts must survive tokenization."""
    return [w.lower() for w in _WORD.findall(text)]


# ── Corpus ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str          # the retrievable passage — also the grounding target
    tags: tuple[str, ...] = ()

    @property
    def index_text(self) -> str:
        """
        What retrieval scores against: the passage AND its tags.

        The tags are authored as retrieval anchors — the Hinglish and
        Devanagari phrasings a customer actually says ("kitna paisa",
        "कितना पैसा") — and before this property they were dead weight:
        embed() only ever saw `text`, so a Roman query for the amount
        matched `faq:who` ("...pending payment ke bare mein...") over
        `faq:how_much`, whose anchors lived only in tags. Indexing both is
        the fix; the grounding gate still verifies answers against `text`
        alone, so tags widen recall without widening what may be said.
        """
        return self.text + " " + " ".join(self.tags)


def _policy_chunks() -> list[Chunk]:
    """Bounds read from the live policy objects — never re-stated by hand."""
    chunks: list[Chunk] = []
    settings = get_settings()
    for risk_type, policy in RISK_POLICIES.items():
        label = risk_type.replace("_", " ")
        chunks.append(
            Chunk(
                id=f"policy:{risk_type}",
                text=(
                    f"{label}: maximum {policy.max_attempts} attempts, "
                    f"consent window {policy.consent_window_hours} hours, "
                    f"first chase after {policy.first_action_hours} hours, "
                    f"re-chase every {policy.re_chase_hours} hours. "
                    f"Recommended rail: {policy.recommended_rail or 'best available'}."
                ),
                tags=(risk_type,),
            )
        )
    chunks.append(
        Chunk(
            id="policy:payment_failure",
            text=(
                f"failed payment: maximum {settings.max_retries_per_payment} attempts, "
                f"consent window {settings.consent_window_hours} hours, "
                f"amount ceiling {money(settings.amount_ceiling_paise)}, "
                "switches to the rail most likely to clear."
            ),
            tags=("payment_failure",),
        )
    )
    chunks.append(
        Chunk(
            id="policy:quiet_hours",
            text=(
                "quiet hours: no contact between 23:00 and 07:00 IST. "
                "A chase that would land inside the blackout is deferred to "
                "the morning, not spent."
            ),
            tags=("blackout", "night", "time"),
        )
    )
    chunks.append(
        Chunk(
            id="policy:opt_out",
            text=(
                "opt-out: saying stop, band karo, mat bhejo, unsubscribe or "
                "do not contact closes every open case for the customer "
                "immediately, across all risk types, and stops all future "
                "outreach. Opting back in reopens recovery."
            ),
            tags=("opt_out", "stop", "consent"),
        )
    )
    return chunks


def _taxonomy_chunks() -> list[Chunk]:
    classes = ", ".join(fc.value for fc in FailureClass)
    return [
        Chunk(
            id="taxonomy:failure_classes",
            text=(
                "failure classes the engine can diagnose: " + classes + ". "
                "Anything the deterministic table cannot match is classified "
                "unknown, never guessed."
            ),
            tags=("diagnosis", "error", "decline"),
        )
    ]


def _faq_chunks() -> list[Chunk]:
    """Hand-authored Hinglish: the call is code-mixed, so is the corpus."""
    return [
        Chunk(
            id="faq:safe",
            text=(
                "Yes, this call and the payment page are safe. Payment is "
                "handled by Razorpay — hum card details ya UPI PIN kabhi "
                "nahi maangte. The link is aapke order ka official recovery "
                "link, signed aur expiring."
            ),
            tags=("safe", "scam", "trust", "fraud"),
        ),
        Chunk(
            id="faq:why_failed",
            text=(
                "Aapka payment bank ya gateway pe decline ho gaya — common "
                "reasons: insufficient funds, card expired, UPI limit "
                "reached, ya network issue. This is not a penalty, the "
                "amount has not left your account."
            ),
            tags=("why", "failed", "decline", "reason"),
        ),
        Chunk(
            id="faq:charged_not_failed",
            text=(
                "Agar aapke pass charge ka message aaya hai, please check "
                "your bank statement first. If the money left and the order "
                "still failed, the bank will reverse it automatically — "
                "double charge kabhi nahi hota. Call your bank if the "
                "reversal does not arrive in 5-7 working days."
            ),
            tags=("charged", "double", "reversal", "refund"),
        ),
        Chunk(
            id="faq:how_pay",
            text=(
                "Aap payment link se complete kar sakte ho — UPI, card, "
                "ya net banking, jo aapko easy lage. The link is signed, "
                "expires in one day, and Razorpay processes it. Main aapko "
                "link SMS kar deta hoon."
            ),
            tags=("how", "pay", "link", "complete"),
        ),
        Chunk(
            id="faq:how_much",
            text=(
                "Aapka pending amount main confirm karke bata sakta hoon — "
                "kitna paisa baaki hai, vo recovery link pe dikhta hai. "
                "The exact amount is on the page and in the SMS link. "
                "आपका बाकी पैसा लिंक पर दिखेगा, वही असली amount है।"
            ),
            tags=("amount", "how much", "kitna", "paisa", "pending", "balance",
                  "baaki", "पैसा", "बाकी", "रकम", "कितना"),
        ),
        Chunk(
            id="faq:when_recovered",
            text=(
                "Jaise hi aap payment complete karte ho, the recovery is "
                "instant — Razorpay confirms it to the merchant "
                "immediately, and your order or subscription resumes "
                "automatically."
            ),
            tags=("when", "recover", "resume", "order", "subscription"),
        ),
        Chunk(
            id="faq:mandate_notice",
            text=(
                "Autopay ke liye RBI rule hai: mandate debit se kam se kam "
                "24 ghante pehle notice jaata hai. Aapko debit se pehle "
                "SMS milta hai — agar nahi mila to vo debit block ho "
                "jata hai, aapki taraf se koi action nahi chahiye."
            ),
            tags=("mandate", "autopay", "notice", "rbi", "24"),
        ),
        Chunk(
            id="faq:who",
            text=(
                "Main merchant ka recovery assistant hoon — aapke pending "
                "payment ke bare mein batane ke liye call kiya hai. Ye koi "
                "scam call nahi hai, payment Razorpay se hota hai."
            ),
            tags=("who", "calling", "scam", "spam", "kaun", "कौन", "घोटाला"),
        ),
    ]


def build_corpus() -> list[Chunk]:
    return _policy_chunks() + _taxonomy_chunks() + _faq_chunks()


# ── Hashed bag-of-words index (offline-first, stdlib only) ────────────────
# Ported from Mic RAG's hash backend: deterministic, dependency-free, and
# honest about being a fallback. ponytail: O(n) scan over ~40 chunks is
# faster than an index for a corpus this size — swap embed() for a real
# encoder when the corpus grows past a few hundred chunks.


def _hash_dim(token: str) -> int:
    return int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big")


def embed(text: str) -> dict[int, float]:
    """Sparse hashed bag-of-words with sublinear tf, l2-normalised."""
    counts: dict[int, float] = {}
    for tok in tokenize(text):
        counts[_hash_dim(tok)] = counts.get(_hash_dim(tok), 0.0) + 1.0
    if not counts:
        return counts
    norm = math.sqrt(sum(v * v for v in counts.values()))
    return {k: (1.0 + math.log(v)) / norm for k, v in counts.items()}


def cosine(a: dict[int, float], b: dict[int, float]) -> float:
    if not a or not b:
        return 0.0
    small, big = (a, b) if len(a) < len(b) else (b, a)
    return sum(v * big.get(k, 0.0) for k, v in small.items())


_DEVA = re.compile(r"[\u0900-\u097F]")

# Code-switch relaxation, ported from the Mic RAG model's measured
# CS_RELAXATION for cross-script queries: "kitna" and "कितना" share
# meaning but not characters, so a Roman-tuned floor refuses real Hindi.
_CS_RELAXATION = 0.10


def retrieve(
    query: str, corpus: list[Chunk], k: int = 3, floor: float | None = None
) -> list[Chunk]:
    """
    Top-k chunks above the score floor. The floor is the difference between
    "I don't know" and a wrong answer: a query with no real match must
    abstain (empty list), not return a vaguely-related chunk the grounding
    gate would then have to reject anyway.

    Devanagari queries get the floor relaxed by the same amount the Mic
    RAG model measured for code-switched retrieval (CS_RELAXATION):
    cross-script matches lose lexical mass to transliteration ("kitna"
    vs "कितना" share meaning, not characters), and a floor tuned on
    Roman queries would refuse every real Hindi question.
    """
    if floor is None:
        floor = 0.18 if not _DEVA.search(query) else 0.18 - _CS_RELAXATION
    qvec = embed(query)
    scored = sorted(
        ((cosine(qvec, embed(c.index_text)), c) for c in corpus),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [c for score, c in scored[:k] if score >= floor]
