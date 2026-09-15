"""A simulated provider for a workload that has no simulator of its own (UPGRADE_V4.md M15).

Every fixture in this repository is simulated, and the demo's simulator is elaborate: it models
which handbook section a wrong answer cites, how often a citation quotes text that is not in the
section it names, and what an unparseable answer looks like. All of that exists to exercise
specific features, and all of it is the demo's.

This is the plain version, for a second workload whose purpose is to show the engine runs on
something it did not generate. It is deliberately *not* a better simulator — UPGRADE_V4.md
section 5 rules that out, and a more convincing simulator would only produce more convincing
numbers about nothing. It does exactly two things:

* decides, deterministically, whether a tier gets a task right, at a rate the **workload
  declares in its own YAML** rather than one buried in this file;
* when it is wrong, answers with a *different task's gold of the same answer type*, so a wrong
  answer is confusable with a right one without inventing a vocabulary of mistakes.

The rates being declared matters more than it looks. They are invented numbers, and a reader
auditing a result should find them in the workload they belong to, next to the provenance block
that says the whole set is program-generated — not by reading the engine.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field

from tokop.adapters.base import LLMRequest
from tokop.workloads.item import Item
from tokop.workloads.runner import TierProfile
from tokop.workloads.spec import OutputContract, SimulationSpec


def _draw(model_id: str, task_id: str, pipeline_id: str, salt: str) -> float:
    """A deterministic uniform draw for one (model, task, pipeline, purpose)."""
    digest = hashlib.sha256(f"{model_id}|{task_id}|{pipeline_id}|{salt}".encode()).hexdigest()
    return int(digest[:12], 16) / 0xFFFFFFFFFFFF


@dataclass
class GroundedResponder:
    """Answers a workload's questions from its own tasks, at declared per-tier rates."""

    items: list[Item]
    grounding: str
    tiers: dict[str, TierProfile]
    simulation: SimulationSpec
    pipeline_id: str = "B2"
    output_contract: OutputContract = "json_answer"
    _by_question: dict[str, Item] = field(init=False)
    _golds: dict[str, list[str]] = field(init=False)

    def __post_init__(self) -> None:
        self._by_question = {item.question: item for item in self.items}
        golds: dict[str, list[str]] = {}
        for item in self.items:
            golds.setdefault(item.answer_type, []).append(item.gold)
        self._golds = {key: sorted(set(values)) for key, values in golds.items()}
        self.role_by_model = {profile.model_id: profile.role for profile in self.tiers.values()}

    def item_for(self, request: LLMRequest) -> Item | None:
        text = request.messages[-1].text if request.messages else ""
        for question, item in self._by_question.items():
            if question in text:
                return item
        return None

    def is_correct(self, model_id: str, item: Item, sample_index: int = 0) -> bool:
        role = self.role_by_model.get(model_id)
        if role is None:
            raise KeyError(
                f"no simulated accuracy for {model_id!r}; known: "
                f"{', '.join(sorted(self.role_by_model))}"
            )
        ceiling = self.simulation.accuracy_for(role, item.question_type)
        salt = "c" if sample_index == 0 else f"c#{sample_index}"
        return _draw(model_id, item.id, "accuracy", salt) < ceiling

    def wrong_answer(self, item: Item, rng: random.Random) -> str:
        """Another task's answer, of the same shape.

        A wrong answer drawn from the set's own answers is confusable with the right one by
        construction, which is what makes a scorer's job non-trivial, and it invents nothing.
        """
        candidates = [gold for gold in self._golds.get(item.answer_type, []) if gold != item.gold]
        if not candidates:
            return "unknown"
        return rng.choice(candidates)

    def __call__(self, request: LLMRequest) -> str:
        item = self.item_for(request)
        if item is None:
            # A request this workload did not generate. Answering it would invent a task.
            return json.dumps({"answer": "not covered", "evidence": ""})

        sample = request.sample_index
        seed = f"{request.model}|{item.id}|{self.pipeline_id}"
        rng = random.Random(seed if sample == 0 else f"{seed}|s{sample}")
        correct = self.is_correct(request.model, item, sample)
        answer = item.gold if correct else self.wrong_answer(item, rng)

        salt = "" if sample == 0 else f"#{sample}"
        if _draw(request.model, item.id, self.pipeline_id, f"parse{salt}") < (
            self.simulation.parse_failure_rate
        ):
            # The answer is present but not in a form the parser can read, which is a real
            # failure mode and the reason `output_parses` is a scorer feature.
            return f"Reading the reference, the answer works out to {answer}."

        evidence = self._evidence(item, rng)
        if self.output_contract == "json_answer":
            return json.dumps({"answer": answer, "evidence": evidence})
        return f"{evidence}\n\nFinal answer: {answer}"

    def _evidence(self, item: Item, rng: random.Random) -> str:
        """A sentence from the grounding document, standing in for a citation."""
        sentences = [line.strip() for line in self.grounding.splitlines() if len(line.strip()) > 40]
        if not sentences:
            return "the reference covers this case"
        matching = [s for s in sentences if item.gold in s]
        return rng.choice(matching or sentences)
