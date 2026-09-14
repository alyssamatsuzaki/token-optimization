"""Verifiers: does this answer look right, when there is no answer key (UPGRADE_V3.md U1)?

``grading.py`` grades against gold. Almost no team arriving with a workload has gold. A
**verifier** is the gold-free half: it reads the question, the grounding document and the
answer, and returns a verdict. It is a model call, so it is wrong sometimes, and the whole
design assumes that rather than hoping otherwise.

Two verifiers, in the same shape:

* a **cheap judge** scores every task, producing ``G_t``;
* a **strong grader** scores a sampled subset, producing ``H_t``. It is a frontier model or a
  human review queue, and both write to the same table.

``core/stats.py``'s ``paired_active_eval_estimate`` then estimates the strong grader's mean from
the two. The inverse-probability weight makes the correction term's expectation equal
``E[H - G]`` whatever ``G`` does, so a biased judge costs interval width and never correctness.
That is the property that makes any of this defensible, and it is why this module is allowed to
be honest about how unreliable a cheap judge is.

**Isolation.** A verifier sees no gold, for the same reason a scorer sees none
(``optimize/scorers.py``): a judge that could reach the answer key would make the estimate
circular. ``tests/test_judge_isolation.py`` fails if this module can reach one. It may import
the *contract* parsers from ``grading.py`` — reading JSON out of a fenced block is not knowledge
of the answer — and nothing else.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: The registered verifier kinds. ``judge-v1`` is the LLM judge every workload gets.
JUDGE_V1 = "judge-v1"

#: What a judge that could not be parsed is recorded as: wrong, and maximally unsure. Wrong
#: because an unreadable verdict has not verified anything (the same rule ``grading.py`` applies
#: to an unreadable answer); maximally unsure because the allocation policy should spend the
#: annotation budget on exactly these, and a confident-looking failure would hide them.
UNPARSEABLE_CONFIDENCE = 0.5


class VerificationError(ValueError):
    """A verifier could not be built or applied."""


@dataclass(frozen=True)
class AnswerView:
    """Everything a verifier is allowed to see about one answer.

    Deliberately not a ``DemoItem`` and deliberately not a ``TaskView``: there is no ``gold``
    field to reach for, and the extra fields a judge does need — which arm produced the answer,
    which tier answered — are bookkeeping the judge prompt never renders.
    """

    task_id: str
    question: str
    answer: str
    #: The grounding document the answer is supposed to follow. Never gold.
    context: str = ""
    #: ``baseline`` or ``candidate``. Used to pair the two arms, never shown to the model.
    arm: str = ""
    tier: str = ""

    def __post_init__(self) -> None:
        for forbidden in ("gold", "correct", "answer_type", "question_type"):
            if forbidden in self.__dict__:  # pragma: no cover - defensive
                raise VerificationError(f"an AnswerView must never carry {forbidden!r}")


def binary_entropy(p: float) -> float:
    """Entropy of a Bernoulli(p), normalized to [0, 1]. 0 at certainty, 1 at p = 0.5."""
    if not 0.0 <= p <= 1.0:
        raise VerificationError(f"a probability must be in [0, 1], got {p}")
    if p in (0.0, 1.0):
        return 0.0
    return float(-p * math.log2(p) - (1 - p) * math.log2(1 - p))


@dataclass(frozen=True)
class VerifierVerdict:
    """One verifier's opinion about one answer."""

    correct: int
    #: The judge's own stated probability that the answer is right. Used by the allocation
    #: policy, never by the estimator: a judge's confidence is a feature, not evidence.
    confidence: float
    reason: str
    parsed: bool = True

    @property
    def uncertainty(self) -> float:
        """How unsure the judge said it was, in [0, 1]. The policy's second feature."""
        return binary_entropy(self.confidence)

    def as_dict(self) -> dict[str, Any]:
        return {
            "correct": self.correct,
            "confidence": self.confidence,
            "reason": self.reason,
            "parsed": self.parsed,
        }


_TRUE = {"true", "yes", "correct", "1", "pass"}
_FALSE = {"false", "no", "incorrect", "0", "fail"}
_VERDICT_LINE = re.compile(r"verdict\s*[:\-]\s*(\w+)", re.IGNORECASE)


def _as_bool(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    text = str(value).strip().casefold()
    if text in _TRUE:
        return 1
    if text in _FALSE:
        return 0
    return None


def _as_confidence(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= number <= 1.0:
        return None
    return number


def parse_verdict(output: str) -> VerifierVerdict:
    """Read a judge's reply.

    The contract is one JSON object: ``{"correct": true, "confidence": 0.0-1.0, "why": "..."}``.
    A ``Verdict: yes`` line is accepted as a fallback for a judge that would not emit JSON,
    because refusing it would throw away a verdict that was actually given.

    A reply that carries neither is **not** an error and **not** a skip: it is recorded as wrong
    and maximally unsure, so it shows up in the annotation budget rather than vanishing.
    """
    from tokop.workloads.grading import extract_json_answer

    payload = extract_json_answer(output)
    if payload is not None and "correct" in payload:
        decided = _as_bool(payload["correct"])
        if decided is not None:
            confidence = _as_confidence(payload.get("confidence"))
            if confidence is None:
                # A judge that gave a verdict but no usable confidence has told us the verdict
                # and nothing about how sure it is. Recording 1.0 would invent certainty, and
                # the allocation policy would then never sample the item.
                confidence = UNPARSEABLE_CONFIDENCE
            return VerifierVerdict(
                correct=decided,
                confidence=confidence,
                reason=str(payload.get("why", "")).strip()[:400],
                parsed=True,
            )

    match = _VERDICT_LINE.search(output or "")
    if match:
        decided = _as_bool(match.group(1))
        if decided is not None:
            return VerifierVerdict(
                correct=decided,
                confidence=UNPARSEABLE_CONFIDENCE,
                reason="a Verdict: line, with no confidence stated",
                parsed=True,
            )

    return VerifierVerdict(
        correct=0,
        confidence=UNPARSEABLE_CONFIDENCE,
        reason="the judge's reply could not be read as a verdict; recorded as unverified",
        parsed=False,
    )


ParseFn = Callable[[str], VerifierVerdict]


@dataclass(frozen=True)
class VerifierKind:
    """One registered way of turning a model reply into a verdict."""

    name: str
    description: str
    parse: ParseFn

    def method_note(self, model_id: str) -> str:
        """Names the method and the model behind it (non-negotiable 2)."""
        return f"{self.description}, {model_id}"


VERIFIER_REGISTRY: dict[str, VerifierKind] = {}


def register_verifier(kind: VerifierKind) -> None:
    if kind.name in VERIFIER_REGISTRY:
        raise VerificationError(f"verifier {kind.name!r} is already registered")
    VERIFIER_REGISTRY[kind.name] = kind


def verifier_kind(name: str) -> VerifierKind:
    try:
        return VERIFIER_REGISTRY[name]
    except KeyError:
        raise VerificationError(
            f"unknown verifier {name!r}; the registry offers {', '.join(sorted(VERIFIER_REGISTRY))}"
        ) from None


register_verifier(
    VerifierKind(
        name=JUDGE_V1,
        description=(
            "LLM judge: one call per answer, reading the question, the grounding document and "
            "the answer, returning a verdict and its own confidence as JSON"
        ),
        parse=parse_verdict,
    )
)


def verify(output: str, kind: str = JUDGE_V1) -> VerifierVerdict:
    """Turn one judge reply into a verdict. The gold-free counterpart of ``grade()``."""
    return verifier_kind(kind).parse(output)


# --------------------------------------------------------------------------- review queue


@dataclass(frozen=True)
class QueueEntry:
    """One answer waiting for a human strong grader."""

    task_id: str
    arm: str
    question: str
    answer: str
    #: The cheap judge's verdict, shown so the reviewer can disagree with something concrete.
    judge_correct: int
    judge_confidence: float
    sampling_rate: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "arm": self.arm,
            "question": self.question,
            "answer": self.answer,
            "judge_correct": self.judge_correct,
            "judge_confidence": self.judge_confidence,
            "sampling_rate": self.sampling_rate,
            "human_correct": None,
        }


def render_queue(entries: list[QueueEntry]) -> str:
    """The review queue a human strong grader works through, as JSON.

    ``human_correct`` is null on every row. Tokop does not fill it in, and the judged estimate
    refuses to run until something else does: a queue that answered itself would be the cheap
    judge wearing a hat.
    """
    return json.dumps(
        {
            "schema": "tokop.annotation-queue.v1",
            "instructions": (
                "Set human_correct to 1 or 0 on every row, then pass this file back with "
                "`tokop annotate --queue-results <file>`. Rows left null stay unannotated and "
                "the estimator treats them as never sampled."
            ),
            "entries": [entry.as_dict() for entry in entries],
        },
        indent=2,
        sort_keys=True,
    )


def read_queue_results(text: str) -> dict[tuple[str, str], VerifierVerdict]:
    """Read a completed review queue back into verdicts, keyed by (task id, arm).

    A row whose ``human_correct`` is null is skipped, not guessed. A row that carries anything
    other than 0, 1 or null is refused: a reviewer who typed something else meant something,
    and deciding what on their behalf is how an annotation set stops being ground truth.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"the review queue is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise VerificationError("a review queue must be an object with an `entries` list")

    out: dict[tuple[str, str], VerifierVerdict] = {}
    for row in payload["entries"]:
        if not isinstance(row, dict):
            raise VerificationError(f"a queue entry must be an object, got {type(row).__name__}")
        value = row.get("human_correct")
        if value is None:
            continue
        decided = _as_bool(value)
        if decided is None:
            raise VerificationError(
                f"task {row.get('task_id')!r} has human_correct={value!r}, which is neither 1, "
                "0 nor null. Fix the row rather than letting Tokop guess what was meant."
            )
        out[(str(row.get("task_id")), str(row.get("arm")))] = VerifierVerdict(
            correct=decided,
            confidence=1.0,
            reason="human review",
            parsed=True,
        )
    return out
