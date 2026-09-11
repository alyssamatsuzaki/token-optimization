"""What the simulated provider answers, and how often it is right.

This is the one place in the build where behaviour is invented rather than measured, and it is
confined here on purpose. Everything downstream — usage, cost, grading, scorer features, the
cascade, the proof — runs the real code over whatever this produces, so the *mechanism* is
genuinely exercised even though the *answers* are simulated. Every surface that displays a
number derived from it labels it simulated (DECISIONS.md D1).

The model it encodes:

* **Accuracy falls with tier and with question difficulty.** A lookup is nearly free for any
  model; a multi-step computation separates them. This is the shape a cascade exists to
  exploit, and inventing it any other way would be inventing the result.
* **The output contract affects parseability, not knowledge.** B0's "end with a line" occasionally
  produces an unparseable answer; B2's JSON contract rarely does. That is the real effect an
  output contract has, and it is why the contract is worth its input tokens.
* **Verbosity follows the contract too.** An uncapped "explain your reasoning in full detail"
  prompt produces paragraphs; a JSON contract produces one line. That gap is the output-side
  saving the product measures.
* **A wrong answer looks like a wrong answer,** not like noise: a perturbed number, a flipped
  yes/no, a neighbouring enum, sometimes a quote from the wrong section.

Draws are deterministic in (model, task, pipeline), so a re-record reproduces the same matrix.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from decimal import Decimal

from tokop.adapters.base import LLMRequest
from tokop.workloads.demo.generator import DemoItem

#: P(correct) by tier role and question type. The frontier clears the 90% sanity floor and the
#: cheap tier sits well outside the 3-point band, so the set can actually show escalation
#: (SPEC.md section 6, the difficulty check).
ACCURACY: dict[str, dict[str, float]] = {
    "frontier": {"lookup": 0.99, "two_hop": 0.97, "computation": 0.94, "exception": 0.93},
    "mid": {"lookup": 0.98, "two_hop": 0.94, "computation": 0.87, "exception": 0.85},
    "cheap": {"lookup": 0.95, "two_hop": 0.86, "computation": 0.70, "exception": 0.66},
}

#: P(the answer cannot be parsed at all), by output contract. An uncapped free-text contract
#: sometimes buries or mangles the answer line; a JSON contract almost never does.
PARSE_FAILURE = {"final_answer_line": 0.02, "json_answer": 0.005}

#: P(the cited section is wrong | the answer is wrong). Gives the scorer real signal to learn.
WRONG_SECTION_GIVEN_WRONG = 0.55


@dataclass(frozen=True)
class TierProfile:
    """One simulated tier: a model ID and the role whose accuracy it uses."""

    model_id: str
    role: str


def _draw(model_id: str, task_id: str, pipeline_id: str, salt: str) -> float:
    """A deterministic uniform draw for one (model, task, pipeline, purpose)."""
    digest = hashlib.sha256(f"{model_id}|{task_id}|{pipeline_id}|{salt}".encode()).hexdigest()
    return int(digest[:12], 16) / 0xFFFFFFFFFFFF


def _wrong_answer(item: DemoItem, rng: random.Random) -> str:
    """A plausible wrong answer of the right shape."""
    if item.answer_type in ("number", "money"):
        try:
            value = Decimal(item.gold)
        except (ValueError, ArithmeticError):
            return "0"
        # The mistakes a model actually makes here: skipping the tier bonus, applying the wrong
        # percentage, or slipping a weight band.
        choice = rng.choice(["drop_bonus", "scale", "band_slip", "round"])
        if choice == "drop_bonus":
            candidate = value - Decimal(rng.choice([14, 30, 7]))
        elif choice == "scale":
            candidate = value * Decimal(rng.choice(["0.9", "1.1", "0.85", "1.25"]))
        elif choice == "band_slip":
            candidate = value + Decimal(rng.choice(["4.00", "-4.00", "8.00"]))
        else:
            candidate = value + Decimal(rng.choice([1, -1, 5, -5]))
        if candidate < 0:
            candidate = abs(candidate)
        if candidate == value:
            candidate = value + Decimal(1)
        return f"{candidate:.2f}" if item.answer_type == "money" else f"{candidate:g}"
    if item.answer_type == "yes_no":
        return "no" if item.gold == "yes" else "yes"
    if item.answer_type == "enum":
        alternatives = [t for t in ("tier_1", "tier_2", "tier_3") if t != item.gold]
        return rng.choice(alternatives) if alternatives else "tier_1"
    return rng.choice(["original payment method", "refund to card", "exchange only"])


def _evidence(handbook: str, section_id: str, rng: random.Random) -> str:
    """A verbatim sentence from a section, which is what the scorer checks."""
    from tokop.workloads.demo.handbook import section_text

    body = section_text(handbook, section_id)
    if not body:
        return "the handbook does not appear to cover this"
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if 40 <= len(s.strip()) <= 220]
    if not sentences:
        return body[:160].strip()
    return rng.choice(sentences).replace("\n", " ")


def _essay(item: DemoItem, answer: str, evidence: str, rng: random.Random) -> str:
    """The long free-text response an uncapped, "explain in full detail" prompt produces."""
    opener = rng.choice(
        [
            "Thanks for reaching out, and I'm happy to help you get to the bottom of this.",
            "Great question, and I can see why this one is confusing.",
            "Let me walk you through how this works so the number makes sense.",
            "I appreciate you checking rather than guessing on this one.",
        ]
    )
    middle = rng.choice(
        [
            "Looking at the handbook material provided, the relevant rule is in the section that "
            "governs this situation, and it is worth reading carefully because there are a few "
            "interacting pieces here.",
            "There are really two things going on in your question, and it helps to separate "
            "them before putting them back together, because the handbook treats them in "
            "different places.",
            "The policy here has changed since earlier versions circulated, so let me quote the "
            "current wording rather than working from memory.",
        ]
    )
    caveat = rng.choice(
        [
            "If anything about your situation differs from what I've assumed, the answer could "
            "change, so do let me know and I'll re-check it for you.",
            "I've double-checked the arithmetic, but if your order details differ from what I "
            "have in front of me the figure may move.",
            "Should you disagree with how this has been applied, I can pass it to a specialist "
            "who can take another look.",
        ]
    )
    return (
        f'{opener}\n\n{middle}\n\nThe handbook says: "{evidence}"\n\n'
        f"Applying that to your question: the relevant category and tier determine the outcome, "
        f"and working through it step by step gives the result below. {caveat}\n\n"
        f"I hope that's clear, and thank you again for your patience.\n\n"
        f"Final answer: {answer}"
    )


class DemoResponder:
    """Answers demo questions for the simulated provider."""

    def __init__(
        self,
        items: list[DemoItem],
        handbook: str,
        tiers: dict[str, TierProfile],
        pipeline_id: str = "B2",
        output_contract: str = "json_answer",
    ) -> None:
        self.by_question = {item.question: item for item in items}
        self.handbook = handbook
        self.tiers = tiers
        self.role_by_model = {p.model_id: p.role for p in tiers.values()}
        self.pipeline_id = pipeline_id
        self.output_contract = output_contract

    def item_for(self, request: LLMRequest) -> DemoItem | None:
        """Find the task a request is asking about, by matching the question text."""
        text = request.messages[-1].text if request.messages else ""
        for question, item in self.by_question.items():
            if question in text:
                return item
        return None

    def is_correct(self, model_id: str, item: DemoItem) -> bool:
        """Whether this tier gets this task right. Deterministic in (model, task)."""
        role = self.role_by_model.get(model_id)
        if role is None:
            raise KeyError(
                f"no simulated accuracy profile for {model_id!r}; known: "
                f"{', '.join(sorted(self.role_by_model))}"
            )
        return _draw(model_id, item.id, "accuracy", "c") < ACCURACY[role][item.question_type]

    def __call__(self, request: LLMRequest) -> str:
        item = self.item_for(request)
        if item is None:
            # A request Tokop did not generate. Answering it would invent a task, so refuse.
            return json.dumps({"answer": "not covered", "evidence": "", "section": ""})

        rng = random.Random(f"{request.model}|{item.id}|{self.pipeline_id}")
        correct = self.is_correct(request.model, item)
        answer = item.gold if correct else _wrong_answer(item, rng)

        section = item.sections[0]
        if not correct and _draw(request.model, item.id, self.pipeline_id, "sec") < (
            WRONG_SECTION_GIVEN_WRONG
        ):
            from tokop.workloads.demo.handbook import SECTION_IDS

            alternatives = [s for s in SECTION_IDS if s not in item.sections]
            section = rng.choice(alternatives)
        evidence = _evidence(self.handbook, section, rng)

        # A wrong-section citation sometimes comes with a quote that is not in that section at
        # all, which is exactly what the evidence feature is there to catch.
        if (
            section not in item.sections
            and _draw(request.model, item.id, self.pipeline_id, "q") < 0.5
        ):
            evidence = "the policy states that this situation is handled as described above"

        contract = self.output_contract
        if _draw(request.model, item.id, self.pipeline_id, "parse") < PARSE_FAILURE.get(
            contract, 0.01
        ):
            # The answer is there but not in a form the parser can read: an essay that trails
            # off, or JSON with prose wrapped around it and the key missing.
            if contract == "json_answer":
                return f"Based on the handbook, the answer is {answer}. Section: {section}."
            return _essay(item, "", evidence, rng).replace("Final answer: ", "In summary, ")

        if contract == "json_answer":
            return json.dumps({"answer": answer, "evidence": evidence, "section": section})
        return _essay(item, answer, evidence, rng)
