"""
The voice turn pipeline — ported from the Mic RAG model's 4-gate shape,
re-grounded on the recovery domain.

  gate 1  input guards      opt-out honoured first; injection refused
  gate 2  retrieval floor   nothing above the score floor -> "no idea" abstain
  gate 3  sanitation        instructions inside retrieved text are stripped
  gate 4  grounding verify  the answer must be supported by its cited passage

Two answer paths, same gates:

* extractive (default, zero dependencies) — the answer is assembled only
  from retrieved chunk text + case-fact passages, so gate 4 verifies a
  string the system itself produced. Ceiling: fluency. Upgrade path: an
  LLM at temperature 0 constrained by the same passages, and gate 4
  stays the boss — an unverifiable answer is a bug, never a feature.

* LLM (opt-in via VOICE_LLM_ENABLED) — the existing dual-provider client
  pattern from src/classifier/llm_tail.py: JSON-constrained output, one
  attempt, any error falls back to the extractive path.

Never-rules that apply unchanged from policy.yaml: no invented amounts,
no invented deadlines, no promises the engine has not already made, and
an opt-out ends the conversation — never a counter-offer.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.voice.dialogue import (
    INJECTION_RESPONSE,
    OPT_OUT_RESPONSE,
    ask_promise,
    extract_amount_paise,
    is_injection,
    is_opt_out,
    is_promise,
    promise_clarify,
    promise_confirm,
    render,
    resolve_date_offset,
)
from src.voice.facts import CaseFacts
from src.voice.knowledge import Chunk, build_corpus, retrieve, tokenize

logger = logging.getLogger(__name__)

_MERCHANT_FALLBACK = "the merchant"
_CORPUS = None  # process-level cache; the corpus is settings-derived


def _corpus() -> list[Chunk]:
    global _CORPUS
    if _CORPUS is None:
        _CORPUS = build_corpus()
    return _CORPUS


def reset_corpus_cache() -> None:
    """Tests only: settings change, so the cached corpus is stale."""
    global _CORPUS
    _CORPUS = None


# ── Gate 3: sanitation (ported from Mic RAG's sanitize()) ──────────────────
_SANITIZE_PATTERNS = (
    "ignore previous", "ignore all previous", "system:", "assistant:",
    "you are now", "developer mode", "instructions:", "prompt:",
)


def sanitize(passage: str) -> str:
    """Strip instruction-shaped lines out of retrieved text (gate 3)."""
    kept = []
    for line in passage.splitlines():
        low = line.lower()
        if not any(p in low for p in _SANITIZE_PATTERNS):
            kept.append(line)
    return " ".join(kept).strip()


# ── Gate 4: grounding verify ──────────────────────────────────────────────


def content_overlap(answer: str, passage: str) -> float:
    """
    Fraction of the answer's content words found in the cited passage.

    Ported from the Mic RAG model's covers(): stop-word-free overlap, the
    shallow-but-honest verifier. Numbers are content words here — an
    amount or deadline in the answer that is absent from the passage is
    exactly the hallucination this gate exists to catch.
    """
    stop = {
        "the", "a", "an", "is", "are", "was", "were", "hai", "hain", "ho",
        "hota", "hoti", "ka", "ki", "ke", "ko", "se", "mein", "me", "aur",
        "ya", "to", "jo", "kya", "nahi", "nahin", "not", "and", "or", "of",
        "in", "on", "for", "your", "you", "i", "we", "this", "that", "it",
    }
    answer_terms = {t for t in tokenize(answer) if t not in stop and len(t) > 1}
    if not answer_terms:
        return 0.0
    passage_terms = set(tokenize(passage))
    return len(answer_terms & passage_terms) / len(answer_terms)


SUPPORT_FLOOR = 0.70  # gate 4: 70% of content words must come from the passage


def grounded(answer: str, passage: str) -> bool:
    return content_overlap(answer, passage) >= SUPPORT_FLOOR


_NUM = re.compile(r"\d[\d,\.]*")


def numbers_grounded(answer: str, passages: list[str]) -> bool:
    """
    Gate 4 (numeric, the money-critical half): every number in the answer —
    amounts, deadlines, attempt counts, hours — must appear verbatim in at
    least one supporting passage. An LLM rephrasing may trade Hindi words
    freely, but it may never trade a number: "₹1,500" unsupported is the
    exact hallucination this system exists to prevent, whatever its fluency.
    """
    answer_nums = set(_NUM.findall(answer))
    if not answer_nums:
        return True
    passage_nums: set[str] = set()
    for p in passages:
        passage_nums.update(_NUM.findall(p))
    return answer_nums.issubset(passage_nums)


# ── The turn ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnResult:
    reply: str
    intent: str
    cited: str | None
    grounded_passages: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    # Set only on intent="promise_captured": the parsed commitment, for the
    # webhook to hand to cases.record_promise. Amount in PAISE; due_at is
    # UTC-aware. Everything here was resolved deterministically from the
    # transcript — the LLM never touched it.
    promise_amount_paise: int | None = None
    promise_due_at: datetime | None = None
    promise_is_partial: bool | None = None


def _opt_out(merchant: str) -> TurnResult:
    return TurnResult(
        reply=render(OPT_OUT_RESPONSE, merchant), intent="opt_out", cited=None,
        notes=("opt-out honoured: all open cases for this customer will be closed",),
    )


def _refuse_injection(merchant: str) -> TurnResult:
    return TurnResult(
        reply=INJECTION_RESPONSE, intent="injection_refused", cited=None,
    )


def _abstain(merchant: str) -> TurnResult:
    return TurnResult(
        reply=(
            "Main is sawaal ka jawab sure nahi hoon — main sirf aapke "
            "payment recovery ke bare mein baat kar sakta hoon. Aapke "
            "order, amount ya payment link ke bare mein kuch poochna "
            "chahenge?"
        ),
        intent="abstain", cited=None,
    )


def _extractive_answer(
    hits: list[Chunk], facts: CaseFacts | None, merchant: str
) -> tuple[str, str]:
    """
    Assemble the reply from retrieved text only: the top chunk's passage
    (sanitized) plus, when the case facts were retrieved, the amount
    passage. Returns (answer, cited_chunk_id).
    """
    passages: list[str] = [sanitize(h.text) for h in hits]
    cited = hits[0].id
    if facts is not None:
        passages.append(sanitize(facts.as_passages(merchant)[0]))
        cited = f"{cited}+facts"
    return (" ".join(passages), cited)


def _promise_turn(
    transcript: str, facts: CaseFacts | None, merchant: str
) -> TurnResult:
    """
    The promise branch: parse deterministically or ask, never guess.

    Resolution order is the whole safety story: (1) a bound case, (2) a
    date the lexicon can ground, (3) an amount — the customer's own number
    if they named one, else the case's full outstanding amount. Missing any
    piece returns the clarification, and nothing is recorded. The spoken
    confirmation restates amount and date, so a misparse is caught by the
    only party who knows the truth: the caller.
    """
    if facts is None:
        return TurnResult(
            reply=promise_clarify(),
            intent="promise_clarify",
            cited=None,
            notes=("promise declined: no case bound to this call",),
        )

    from src.config import get_settings

    horizon = timedelta(days=get_settings().promise_max_horizon_days)
    offset = resolve_date_offset(transcript)
    if offset is None or offset > horizon.days:
        return TurnResult(
            reply=promise_clarify(),
            intent="promise_clarify",
            cited=None,
            notes=("promise declined: date unresolvable or beyond horizon",),
        )
    due_at = datetime.now(UTC) + timedelta(days=offset)

    # The case's outstanding, not the at-risk label: a partial recovery has
    # already shrunk what is genuinely owed, and promising the stale total
    # re-promises money that already arrived. (This comment was right and the
    # line under it read amount_at_risk anyway — so a customer who had paid
    # half was asked, out loud, to promise the whole thing again, and the
    # promise ledger recorded the inflated figure.)
    outstanding_label = facts.amount_outstanding
    if facts.state == "recovered":
        return TurnResult(
            reply=(
                "Aapka payment already aa chuka hai — koi pending amount nahi "
                "hai. Thank you!"
            ),
            intent="answer",
            cited=None,
            grounded_passages=tuple(),
            notes=("promise declined: case already recovered",),
        )

    named = extract_amount_paise(transcript)
    amount: int | None
    if named is not None and named > 0:
        amount = named
        is_partial = True  # the customer's own number, not the full dues
    else:
        # Full outstanding: the digits come out of the case facts so the
        # numeric grounding holds by construction.
        digits = re.sub(r"[^\d]", "", outstanding_label)
        amount = int(digits) * 100 if digits else None
        is_partial = False
        if amount is None or amount <= 0:
            return TurnResult(
                reply=promise_clarify(),
                intent="promise_clarify",
                cited=None,
                notes=("promise declined: no amount could be grounded",),
            )
    assert amount is not None  # both branches above guarantee it

    amount_display = f"₹{amount // 100:,}"
    if offset == 0:
        date_display = "aaj"
    elif offset == 1:
        date_display = "kal"
    elif offset == 2:
        date_display = "parso"
    else:
        date_display = f"{offset} din me"
    reply = promise_confirm(amount_display, date_display)

    return TurnResult(
        reply=reply,
        intent="promise_captured",
        cited=None,
        # The reply's numbers are grounded because the passages ARE the
        # values the row will carry — the row is the ground truth, rendered.
        grounded_passages=(
            f"amount {amount_display} due {due_at.isoformat()} promise confirmed",
        ),
        promise_amount_paise=amount,
        promise_due_at=due_at,
        promise_is_partial=is_partial,
        notes=(f"promise parsed: {amount} paise due {due_at.isoformat()}",),
    )


def _ask_promise_fallback(merchant: str) -> TurnResult:
    return TurnResult(reply=ask_promise(merchant), intent="ask_ptp", cited=None)


async def run_turn(
    transcript: str,
    *,
    facts: CaseFacts | None,
    merchant_name: str | None = None,
    llm_enabled: bool = False,
) -> TurnResult:
    """
    One voice turn: transcript in, verified reply out.

    Gate order is the contract: opt-out and injection are checked on the
    raw transcript before any retrieval (nothing downstream may re-route
    an opt-out into a pitch), then the retrieval floor, then assembly,
    then grounding. A reply that fails grounding is an abstain, never a
    best guess.
    """
    merchant = merchant_name or _MERCHANT_FALLBACK

    # Gate 1 — input guards, in the order that can never be argued with:
    # an opt-out wins even if the same sentence also contains an injection.
    if is_opt_out(transcript):
        return _opt_out(merchant)
    if is_injection(transcript):
        return _refuse_injection(merchant)

    # Gate 1b — a promise to pay is the one customer response that makes the
    # workflow quieter, so it is captured before any retrieval: the promise
    # branch never needs the FAQ corpus, and a promise riding inside a
    # question ("1500 kal tak bhej dunga, but kya safe hai?") must land as a
    # commitment, not as an answer about safety. Deterministic end to end —
    # date and amount are lexicon/regex-resolved, never LLM-parsed, and the
    # spoken confirmation re-states both numbers so the customer hears the
    # double-loop. A promise with no bound case has nowhere to be recorded;
    # it declines into a clarification, not a silent discard.
    if is_promise(transcript):
        return _promise_turn(transcript, facts, merchant)

    # Gate 2 — retrieval floor. Case-fact passages join the pool when the
    # caller bound the turn to a case (identity already established by
    # the telephony provider's lookup). A case-bound turn never abstains
    # purely for lack of FAQ hits — the facts themselves answer "how
    # much / what status".
    corpus = _corpus()
    hits = retrieve(transcript, corpus, k=3)

    if not hits and facts is None:
        return _abstain(merchant)

    # Assembly — LLM path optional, extractive path is the floor.
    if llm_enabled:
        try:
            answer, cited = await _llm_answer(transcript, hits, facts, merchant)
        except Exception:
            logger.warning("voice LLM path failed; extractive fallback", exc_info=True)
            answer, cited = _extractive_answer(hits, facts, merchant)
    else:
        answer, cited = _extractive_answer(hits, facts, merchant)

    # Gate 4 — grounding, two halves: content-word overlap against the UNION
    # of the support passages (the extractive answer is built from that set,
    # so a fragment-wise check could never pass a multi-passage answer), and
    # the numeric half that applies to every path — a number absent from the
    # passages is a hallucination regardless of fluency.
    support_passages = [h.text for h in hits]
    if facts is not None:
        support_passages.extend(facts.as_passages(merchant))
    support_union = " ".join(support_passages)
    best = content_overlap(answer, support_union)
    if best < SUPPORT_FLOOR or not numbers_grounded(answer, support_passages):
        logger.info(
            "voice turn failed grounding (overlap %.2f) — abstaining", best
        )
        return _abstain(merchant)

    return TurnResult(
        reply=answer,
        intent="answer",
        cited=cited,
        grounded_passages=tuple(support_passages),
    )


async def _llm_answer(
    transcript: str,
    hits: list[Chunk],
    facts: CaseFacts | None,
    merchant: str,
) -> tuple[str, str]:
    """
    LLM upgrade path: JSON-constrained, passages-only, one attempt.

    Same never-freeform discipline as the policy agent: the model picks a
    reply built from the supplied passages; anything unparseable raises
    and the caller falls back to the extractive answer.
    """
    from src.config import get_settings, reveal

    settings = get_settings()
    passages = [sanitize(h.text) for h in hits]
    if facts is not None:
        passages.extend(facts.as_passages(merchant))
    # Any: the two SDK clients have different shapes and the branch keys off
    # a str setting mypy cannot narrow (the same pattern the other LLM
    # call sites in this codebase use).
    client: Any
    system = (
        "You are a payment recovery voice agent speaking Hinglish (mixed "
        "Hindi and English). Reply ONLY with JSON: "
        '{"reply": "<one short spoken answer, built ONLY from the passages, '
        'same Hinglish mix>"} . Never state amounts, deadlines or facts '
        "not present in the passages. Keep it under 60 spoken words."
    )
    user = "Passages:\n" + "\n".join(f"- {p}" for p in passages) + f"\n\nCaller: {transcript}"

    if settings.llm_provider == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic(
            api_key=reveal(settings.anthropic_api_key),
            timeout=settings.llm_timeout_seconds,
        )
        response = await client.messages.create(
            model=settings.llm_model,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        raw = response.content[0].text if hasattr(response.content[0], "text") else ""
    else:
        import openai

        client = openai.AsyncOpenAI(
            api_key=reveal(settings.openai_api_key),
            base_url=settings.llm_base_url or None,
            timeout=settings.llm_timeout_seconds,
        )
        completion = await client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=300,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = completion.choices[0].message.content or ""

    start, end = raw.find("{"), raw.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    reply = json.loads(raw[start:end]).get("reply", "")
    if not isinstance(reply, str) or not reply.strip():
        raise ValueError("empty reply")
    return reply.strip(), hits[0].id + ("+facts" if facts is not None else "")
