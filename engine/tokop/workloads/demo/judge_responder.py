"""What the simulated judge says, and how often it is wrong (UPGRADE_V3.md U1).

Invented behaviour, confined here for the same reason ``responder.py`` is: everything
downstream — the verdict parser, the allocation policy, the active estimator, the report — runs
the real code over whatever this produces, so the *mechanism* is exercised even though the
*verdicts* are simulated. Every surface that shows a number derived from it labels it simulated
(DECISIONS.md D1).

The model it encodes, and why each part is there:

* **A judge is more willing to accept a wrong answer than to reject a right one.** That is the
  characteristic LLM-judge failure and the one a buyer has been burned by, so the simulator
  gives sensitivity and specificity separately rather than a single accuracy, and specificity is
  the worse of the two at every tier.
* **Checking is easier than answering, but not uniformly.** The gap between the tiers is
  narrower here than in ``responder.py``'s answer table, which is the asymmetry the whole
  design rests on — a model too weak to answer can still be strong enough to check. It is not
  *zero*: a computation still has to be redone to be checked, so the type penalty is largest
  there.
* **The judge states a calibrated confidence, computed from what it could know.** It reports
  the Bayes posterior that the answer is right given the verdict it just gave, its own error
  rates and a prior — never given the truth, which would put a gold label inside the field the
  allocation policy reads. See ``posterior`` for what that costs in per-item signal, which is
  a limitation of these fixtures and is stated rather than hidden.

**What this file does not establish.** None of these numbers is a measurement, and no claim
about real judges is derived from them. The property U1 rests on — that the active estimate
stays unbiased however bad the judge is — is proved against an *injected* adversarial judge in
``tests/test_judged_proof.py``, not against this table, precisely so the guarantee does not
depend on a simulator that could have been tuned to flatter it.

Draws are deterministic in (judge model, task, arm, answer), so a re-record reproduces the same
verdicts, and judging the same answer twice gives the same verdict — which a content-addressed
cassette would enforce anyway.
"""

from __future__ import annotations

import hashlib
import json
import re

from tokop.adapters.base import LLMRequest
from tokop.workloads.item import Item
from tokop.workloads.runner import TierProfile

#: P(the judge accepts an answer that is in fact right), by the judge's model role.
JUDGE_SENSITIVITY: dict[str, float] = {"frontier": 0.985, "mid": 0.960, "cheap": 0.930}

#: P(the judge rejects an answer that is in fact wrong), by role. Worse than sensitivity at
#: every tier, on purpose: leniency is the failure mode that makes LLM-as-judge untrustworthy.
JUDGE_SPECIFICITY: dict[str, float] = {"frontier": 0.970, "mid": 0.900, "cheap": 0.780}

#: Subtracted from both rates. Checking a lookup is reading; checking a computation is redoing
#: it, so the tail of the question mix is where a cheap judge stops being reliable.
JUDGE_TYPE_PENALTY: dict[str, float] = {
    "lookup": 0.0,
    "two_hop": 0.02,
    "computation": 0.05,
    "exception": 0.06,
}

#: What a checkable answer buys the judge (UPGRADE_V3.md U4). An answer that shows the step
#: from the quoted rule to the number can be *checked*; one that shows only the number has to be
#: re-derived, and a cheap model re-deriving is a cheap model answering. The bonus lands mostly
#: on specificity, because catching a wrong answer is where a cheap judge fails: a derivation
#: that does not produce the stated answer is visible without knowing the right one.
#:
#: Invented, like every other number in this file. What is *not* invented is where it shows up:
#: a higher-agreement judge shrinks the correction term in the active estimator, which shrinks
#: the annotation budget for a given interval width, and the report prices that against the
#: accuracy the contract cost. The mechanism is real; the size is a simulator parameter.
CHECKABLE_SENSITIVITY_BONUS = 0.01
CHECKABLE_SPECIFICITY_BONUS = 0.12

#: How far the stated confidence wanders from the posterior. A judge that reported its own
#: reliability to three decimal places would make the uncertainty feature suspiciously clean.
CONFIDENCE_JITTER = 0.05

#: The judge's prior that an answer it is handed is right. It is the one thing about the item a
#: judge could legitimately know without checking, and it is what turns its error rates into a
#: stated confidence. 0.9 is roughly what the frontier tier delivers on this workload.
ASSUMED_BASE_RATE = 0.9

#: The delimiters the demo's judge prompt wraps the answer under review in, so the simulator can
#: find it without guessing. A real judge reads the same markers.
ANSWER_OPEN = "<<<ANSWER"
ANSWER_CLOSE = "ANSWER>>>"

_ANSWER_BLOCK = re.compile(
    re.escape(ANSWER_OPEN) + r"\s*(.*?)\s*" + re.escape(ANSWER_CLOSE), re.DOTALL
)


class JudgeResponderError(ValueError):
    """The simulated judge was asked something it cannot answer honestly."""


def extract_reviewed_answer(text: str) -> str | None:
    """Pull the answer under review out of a rendered judge prompt."""
    match = _ANSWER_BLOCK.search(text)
    return match.group(1) if match else None


def _draw(model_id: str, task_id: str, answer_hash: str, salt: str) -> float:
    digest = hashlib.sha256(f"{model_id}|{task_id}|{answer_hash}|{salt}".encode()).hexdigest()
    return int(digest[:12], 16) / 0xFFFFFFFFFFFF


def _answer_hash(answer: str) -> str:
    return hashlib.sha256(answer.encode()).hexdigest()[:16]


def posterior(sensitivity: float, specificity: float, says_correct: bool) -> float:
    """P(the answer is right | this verdict), for a judge that knows its own error rates.

    ``ASSUMED_BASE_RATE`` is the judge's prior that an answer is right — the one thing here it
    genuinely could know, from having seen the workload. The posterior depends on the verdict,
    the judge's rates and that prior, and on nothing about this particular item, which is the
    point: a confidence derived from whether the answer *is* right would be a gold label in
    disguise.

    The consequence is worth stating rather than discovering later: the uncertainty this
    produces varies with the judge's role, the question type and the verdict, and with nothing
    finer. It carries no per-item signal. So on these fixtures the allocation policy's
    per-item information comes from disagreement between the two arms, and its uncertainty
    feature separates question types. A real judge's confidence would vary task by task, and
    the policy would have more to work with, not less.
    """
    base = ASSUMED_BASE_RATE
    if says_correct:
        numerator = sensitivity * base
        denominator = numerator + (1 - specificity) * (1 - base)
    else:
        numerator = (1 - sensitivity) * base
        denominator = numerator + specificity * (1 - base)
    return numerator / denominator if denominator else base


def rates_for(role: str, question_type: str, *, checkable: bool = False) -> tuple[float, float]:
    """(sensitivity, specificity) for one judge role on one question type.

    ``checkable`` is a property of the *answer under review*, not of the judge: the same judge
    reading an answer that shows its derivation catches more wrong answers than one reading a
    bare number.
    """
    try:
        sensitivity = JUDGE_SENSITIVITY[role]
        specificity = JUDGE_SPECIFICITY[role]
    except KeyError:
        raise JudgeResponderError(
            f"no simulated judge profile for role {role!r}; known: "
            f"{', '.join(sorted(JUDGE_SENSITIVITY))}"
        ) from None
    penalty = JUDGE_TYPE_PENALTY.get(question_type, 0.0)
    if checkable:
        sensitivity += CHECKABLE_SENSITIVITY_BONUS
        specificity += CHECKABLE_SPECIFICITY_BONUS
    return (
        min(0.999, max(0.5, sensitivity - penalty)),
        min(0.999, max(0.5, specificity - penalty)),
    )


def is_checkable(answer: str) -> bool:
    """Whether the answer under review shows the step from its rule to its number.

    Read off the answer itself rather than passed in as a flag, because that is what makes the
    lever honest: the judge is better because the answer carries something checkable, not
    because a configuration said it should be.
    """
    from tokop.workloads.grading import extract_json_answer

    payload = extract_json_answer(answer)
    return bool(payload and str(payload.get("derivation", "")).strip())


class DemoJudgeResponder:
    """Answers judge prompts for the simulated provider."""

    def __init__(
        self,
        items: list[Item],
        handbook: str,
        tiers: dict[str, TierProfile],
    ) -> None:
        self.by_question = {item.question: item for item in items}
        self.handbook = handbook
        self.role_by_model = {profile.model_id: profile.role for profile in tiers.values()}

    def item_for(self, request: LLMRequest) -> Item | None:
        text = request.messages[-1].text if request.messages else ""
        for question, item in self.by_question.items():
            if question in text:
                return item
        return None

    def __call__(self, request: LLMRequest) -> str:
        item = self.item_for(request)
        text = request.messages[-1].text if request.messages else ""
        answer = extract_reviewed_answer(text)
        if item is None or answer is None:
            # A judge prompt Tokop did not generate, or one with no answer in it. Returning a
            # verdict would be inventing a verification; the parser records this as unverified.
            return json.dumps(
                {
                    "correct": False,
                    "confidence": 0.5,
                    "why": "this prompt does not contain a reviewable answer",
                }
            )

        role = self.role_by_model.get(request.model)
        if role is None:
            raise JudgeResponderError(
                f"no simulated judge profile for {request.model!r}; known: "
                f"{', '.join(sorted(self.role_by_model))}"
            )

        # The simulator is allowed to know the truth; that is what makes it a simulator. The
        # engine's own verification path never can — `workloads/verification.py` has no way to
        # reach a gold answer and `tests/test_judge_isolation.py` fails if it grows one.
        from tokop.workloads.grading import grade

        truth = grade(answer, item.gold, item.answer_type, item.aliases).correct
        sensitivity, specificity = rates_for(
            role, item.question_type, checkable=is_checkable(answer)
        )
        hit_rate = sensitivity if truth else specificity
        digest = _answer_hash(answer)
        agrees = _draw(request.model, item.id, digest, "verdict") < hit_rate
        says_correct = truth if agrees else not truth

        # The stated confidence must be computable from what the judge *knows*: the verdict it
        # just gave, and its own reliability on this kind of question. Deriving it from `truth`
        # would leak the answer key into the confidence — an item the judge was wrong about
        # would carry visibly higher uncertainty — and the allocation policy reads exactly that
        # field. The policy would then be sampling a disguised gold label and its measured
        # advantage over uniform would be an artefact of the simulator, which is the mistake
        # D27 caught in the sampling scorer. So this is a Bayes posterior instead.
        jitter = (_draw(request.model, item.id, digest, "confidence") - 0.5) * 2 * CONFIDENCE_JITTER
        confidence = min(
            0.98, max(0.02, posterior(sensitivity, specificity, says_correct) + jitter)
        )

        return json.dumps(
            {
                "correct": bool(says_correct),
                "confidence": round(confidence, 3),
                "why": (
                    f"checked against {item.sections[0]}"
                    if says_correct
                    else f"does not follow from {item.sections[0]}"
                ),
            }
        )
