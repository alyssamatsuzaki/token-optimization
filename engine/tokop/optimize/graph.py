"""A pipeline as a graph of steps (UPGRADE_V4.md M16).

Until M16 a pipeline was one model call: a prompt, a model, a cap and a contract. That shape can
express two optimizations — run a cheaper model, write a cheaper prompt — and no others. The
workloads where the money actually goes are shaped differently: an agent that makes fourteen
calls and retries twice is a larger problem than a prompt that is eight thousand tokens long,
and the useful proposal there is usually *delete a step*, which a single-call spec cannot even
represent.

This module is the representation. It is deliberately the smallest thing that can hold a graph:

* a **step** is one unit of work with an id, a kind, and the steps whose output it consumes;
* a **graph** is the steps plus the order they run in, which is a topological sort of that
  dependency relation;
* a pipeline with no declared steps compiles to a graph of exactly one `generate` step, which
  renders the request the single-call path has always rendered.

That last point is the whole safety argument for the milestone. UPGRADE_V4.md section 3.4 says
single-call workloads keep working unchanged through M16 and M17, and "unchanged" here has a
precise meaning: the compiled one-step graph must produce a request with the **same cassette
key**. If the key matches, every committed cassette still replays, and every number computed
from them is identical by construction rather than by inspection.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tokop.workloads.spec import StepSpec, WorkloadError

#: Kinds of work a step can be. `generate` is a model call; the rest exist so a graph can say
#: what a step *is* before Tokop can price it, because a finding about a tool called twice with
#: identical arguments needs to know which calls were tool calls.
STEP_KINDS: tuple[str, ...] = ("generate", "tool", "retrieve", "verify", "retry", "loop")

#: The id the single-call path compiles to. Named rather than spelled out, because it appears in
#: the ledger, in findings and in the trace drawer, and three spellings would become two names.
SINGLE_STEP = "generate"


class GraphError(WorkloadError):
    """A graph that cannot be executed, with the reason a workload author can act on."""


@dataclass(frozen=True)
class Step:
    """One step, resolved: its spec plus the place it sits in the graph."""

    id: str
    kind: str
    spec: StepSpec | None
    inputs: tuple[str, ...]

    @property
    def is_generation(self) -> bool:
        return self.kind in ("generate", "retry", "verify")

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "inputs": list(self.inputs)}


@dataclass(frozen=True)
class Graph:
    """The steps of one pipeline, in the order they run."""

    pipeline_id: str
    steps: tuple[Step, ...]

    @property
    def is_single_call(self) -> bool:
        """Whether this is the shape every workload had before M16.

        Checked rather than assumed wherever the old path is taken, so that a graph workload
        cannot quietly fall through a branch written for one call.
        """
        return len(self.steps) == 1 and self.steps[0].id == SINGLE_STEP

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise GraphError(f"{self.pipeline_id} has no step {step_id!r}")

    def dependents(self, step_id: str) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step_id in step.inputs)

    def as_dict(self) -> dict[str, Any]:
        return {"pipeline": self.pipeline_id, "steps": [step.as_dict() for step in self.steps]}


def _topological(pipeline_id: str, specs: Sequence[StepSpec]) -> tuple[Step, ...]:
    """Run order: every step after the steps it consumes.

    Kahn's algorithm, with ties broken by declaration order so the same YAML always produces the
    same order — a graph whose run order depended on dict iteration would produce a different
    cassette key on a different day.
    """
    by_id = {spec.id: spec for spec in specs}
    if len(by_id) != len(specs):
        counts = Counter(spec.id for spec in specs)
        repeated = sorted(step_id for step_id, count in counts.items() if count > 1)
        raise GraphError(f"{pipeline_id} declares step {repeated} more than once")
    for spec in specs:
        unknown = [name for name in spec.inputs if name not in by_id]
        if unknown:
            raise GraphError(
                f"{pipeline_id} step {spec.id!r} consumes {unknown}, which it does not declare. "
                f"Known steps: {', '.join(by_id)}."
            )
        if spec.kind not in STEP_KINDS:
            raise GraphError(
                f"{pipeline_id} step {spec.id!r} is kind {spec.kind!r}; "
                f"choose from {', '.join(STEP_KINDS)}"
            )

    remaining = {spec.id: set(spec.inputs) for spec in specs}
    order: list[Step] = []
    while remaining:
        ready = [spec.id for spec in specs if spec.id in remaining and not remaining[spec.id]]
        if not ready:
            raise GraphError(
                f"{pipeline_id} has a cycle among {sorted(remaining)}. A step cannot consume "
                "its own output, directly or through others."
            )
        for step_id in ready:
            spec = by_id[step_id]
            order.append(Step(id=spec.id, kind=spec.kind, spec=spec, inputs=tuple(spec.inputs)))
            del remaining[step_id]
        for waiting in remaining.values():
            waiting.difference_update(ready)
    return tuple(order)


def compile_graph(pipeline: Any) -> Graph:
    """The graph a pipeline runs as.

    A pipeline that declares no steps compiles to one `generate` step carrying the pipeline
    itself, which is the single-call path expressed as a graph of one. Nothing about the request
    it renders changes, which is what `tests/test_graph.py` pins by cassette key.
    """
    if not pipeline.steps:
        return Graph(
            pipeline_id=pipeline.id,
            steps=(Step(id=SINGLE_STEP, kind="generate", spec=None, inputs=()),),
        )
    return Graph(pipeline_id=pipeline.id, steps=_topological(pipeline.id, pipeline.steps))
