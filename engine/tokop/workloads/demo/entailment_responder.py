"""What the simulated entailment model says (UPGRADE_V3.md U6).

Invented behaviour, confined here like every other invented thing. Entailment clustering is the
difference between ``semantic-entropy-v1`` and the exact-match approximation D27 shipped, so the
simulator has to be able to do the one thing exact match cannot: merge two answers that are
worded differently and mean the same.

The oracle it uses is deliberately **more permissive** than the workload's answer-equivalence
relation and in exactly one direction — units, currency symbols and a hedging preamble do not
change what an answer means. That is the gap the method exists to close, so it is the gap the
simulator models.

**What this cannot show on the demo's own data.** The demo's answers are numbers, enums and
yes/no, and the simulated responder emits them bare. Exact match and entailment therefore agree
on almost every pair the fixtures actually contain, and semantic entropy buys nothing here while
costing calls. That is a property of this workload, not of the method, and the paraphrase
merging the method is *for* is tested against explicit pairs in ``tests/test_entailment.py``
rather than claimed from a split that cannot exercise it.
"""

from __future__ import annotations

import hashlib
import re

from tokop.adapters.base import LLMRequest
from tokop.optimize.entailment import entailment_reply
from tokop.workloads.runner import TierProfile

#: P(the entailment model gets a pair right), by the model role judging it. Entailment is an
#: easier question than answering, and the tiers are close together for the same reason the
#: judge's are: checking is easier than generating.
ENTAILMENT_ACCURACY: dict[str, float] = {"frontier": 0.995, "mid": 0.985, "cheap": 0.970}

#: Suffixes that do not change what a numeric answer means.
_UNITS = re.compile(r"\s*(days?|business days?|dollars?|usd|%|percent)\s*$", re.IGNORECASE)
_CURRENCY = re.compile(r"^\s*[$£€]\s*")
#: A hedge in front of a yes/no answer does not change the answer.
_HEDGE = re.compile(r"^\s*(yes|no)\s*[,.—-]\s*.*$", re.IGNORECASE | re.DOTALL)

ANSWER_A = "<<<A"
ANSWER_A_CLOSE = "A>>>"
ANSWER_B = "<<<B"
ANSWER_B_CLOSE = "B>>>"

_BLOCK_A = re.compile(re.escape(ANSWER_A) + r"\s*(.*?)\s*" + re.escape(ANSWER_A_CLOSE), re.DOTALL)
_BLOCK_B = re.compile(re.escape(ANSWER_B) + r"\s*(.*?)\s*" + re.escape(ANSWER_B_CLOSE), re.DOTALL)


class EntailmentResponderError(ValueError):
    """The simulated entailment model was asked something it cannot answer honestly."""


def _canonical(answer: str) -> str:
    """What an answer means, as far as this simulator is concerned.

    Strips the things that do not change meaning — a currency symbol, a unit, a hedge before a
    yes or no — and normalizes the rest with the workload's own normalizer. Everything it does
    *not* strip stays a difference, so the oracle is permissive in one direction only.
    """
    from tokop.workloads.grading import normalize, parse_number

    text = answer.strip()
    hedge = _HEDGE.match(text)
    if hedge:
        text = hedge.group(1)
    text = _CURRENCY.sub("", text)
    text = _UNITS.sub("", text)
    number = parse_number(text)
    if number is not None and _looks_bare(text):
        return f"n:{number.normalize()}"
    return normalize(text)


def _looks_bare(text: str) -> bool:
    return bool(re.fullmatch(r"[-+]?[\d,]*\.?\d+\s*%?", text.strip()))


def means_the_same(left: str, right: str) -> bool:
    """The simulator's oracle: do these two answers mean the same thing?"""
    return bool(_canonical(left)) and _canonical(left) == _canonical(right)


def extract_pair(text: str) -> tuple[str, str] | None:
    a, b = _BLOCK_A.search(text), _BLOCK_B.search(text)
    return (a.group(1), b.group(1)) if a and b else None


def _draw(model_id: str, left: str, right: str) -> float:
    digest = hashlib.sha256(f"{model_id}|{left}|{right}".encode()).hexdigest()
    return int(digest[:12], 16) / 0xFFFFFFFFFFFF


class DemoEntailmentResponder:
    """Answers entailment prompts for the simulated provider."""

    def __init__(self, tiers: dict[str, TierProfile]) -> None:
        self.role_by_model = {profile.model_id: profile.role for profile in tiers.values()}

    def __call__(self, request: LLMRequest) -> str:
        text = request.messages[-1].text if request.messages else ""
        pair = extract_pair(text)
        if pair is None:
            # A prompt with no pair in it is not an entailment question. Answering it would be
            # inventing a judgement; the parser records an unreadable reply and the clustering
            # keeps the samples apart rather than merging on a guess.
            return "this prompt does not contain two answers to compare"
        role = self.role_by_model.get(request.model)
        if role is None:
            raise EntailmentResponderError(
                f"no simulated entailment profile for {request.model!r}; known: "
                f"{', '.join(sorted(self.role_by_model))}"
            )
        left, right = pair
        truth = means_the_same(left, right)
        correct = _draw(request.model, left, right) < ENTAILMENT_ACCURACY[role]
        says = truth if correct else not truth
        return entailment_reply(
            says, "same value once units and hedging are removed" if says else "different values"
        )
