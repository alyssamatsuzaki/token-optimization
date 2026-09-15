"""What a task is, to everything that is not the demo (UPGRADE_V4.md M15).

Until M15 the engine's idea of a task was ``workloads.demo.generator.DemoItem``, imported
directly by the runner, the recorder, the annotator and the report. That is a stronger coupling
than "ships one workload" suggests: it is not a missing command but a type from one workload's
generator sitting in the signature of every neutral module (DECISIONS.md D41.2).

This module is that type's neutral replacement. ``Item`` is a protocol rather than a base class
so ``DemoItem`` satisfies it without changing, and so a workload whose tasks come from somewhere
else — a JSONL export, a CSV, a set of OpenTelemetry spans — satisfies it without inheriting
anything.

The fields split into three groups, and the split is the interesting part:

* **Grading**, which every workload has: ``id``, ``question``, ``gold``, ``answer_type``,
  ``aliases``. ``workloads/grading.py`` has always taken these as plain values, so the grader
  was never coupled to the demo; only its callers were.
* **Description**, which every workload needs something for: ``question_type`` groups tasks for
  the breakdown by type and, from M18, for the workload fingerprint. The name is the demo's;
  what it means is "which kind of task this is".
* **Provenance**, which only a generated workload has: ``template_id`` and ``sections`` say what
  produced a task and what it was drawn from. Real traffic has neither, so both default to
  empty rather than being invented — a dataset that fabricates a template id is a dataset whose
  concentration measure is fiction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from tokop.workloads.grading import AnswerType


@runtime_checkable
class Item(Protocol):
    """One task, as the engine sees it.

    Every member is read-only. Declared as properties rather than as plain attributes because a
    plain attribute in a protocol is a *settable* one, which no frozen implementation can
    satisfy — and every implementation here is frozen, because a task that can be edited after
    a result was measured against it is a task that can invalidate the result in place.
    """

    @property
    def id(self) -> str: ...

    @property
    def question(self) -> str: ...

    @property
    def gold(self) -> str: ...

    @property
    def answer_type(self) -> AnswerType: ...

    @property
    def question_type(self) -> str: ...

    @property
    def aliases(self) -> tuple[str, ...]: ...

    @property
    def template_id(self) -> str: ...

    @property
    def sections(self) -> tuple[str, ...]: ...

    def to_dict(self) -> dict[str, Any]: ...

    def router_view(self) -> dict[str, Any]:
        """What a router or scorer is allowed to see: the question and nothing else."""
        ...


@dataclass(frozen=True)
class IngestedItem:
    """A task that came from a file rather than from a generator.

    Everything a generator would know about how a task was made is absent here, and stays
    absent. ``template_id`` is empty because nothing templated it, and the provenance checks
    read that as "no template space to be concentrated in" rather than as a template of its own.
    """

    id: str
    question: str
    gold: str
    answer_type: AnswerType
    question_type: str = "unclassified"
    aliases: tuple[str, ...] = ()
    template_id: str = ""
    sections: tuple[str, ...] = ()
    #: Which split this task belongs to, as recorded in the file it was read from. Splits are
    #: committed rather than recomputed so that a re-read cannot silently move a task from the
    #: calibration side to the test side and invalidate every result measured before it.
    split: str = "test"
    #: Anything the source carried that Tokop has no field for. Kept so a trace can show it and
    #: an ingestion can be audited against its source, never read by a scorer.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "gold": self.gold,
            "answer_type": self.answer_type,
            "question_type": self.question_type,
            "aliases": list(self.aliases),
            "template_id": self.template_id,
            "sections": list(self.sections),
            "split": self.split,
            **({"extra": self.extra} if self.extra else {}),
        }

    def router_view(self) -> dict[str, Any]:
        return {"id": self.id, "question": self.question}
