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

from tokop.workloads.spec import WORKLOAD_VARIABLES, PipelineSpec, StepSpec, WorkloadError
from tokop.workloads.tools import get_tool

#: Kinds of work a step can be. `generate` is a model call; the rest exist so a graph can say
#: what a step *is* before Tokop can price it, because a finding about a tool called twice with
#: identical arguments needs to know which calls were tool calls.
#:
#: There is no `loop`. M16a listed one and nothing could run it; a kind that compiles and then
#: cannot execute is the state this milestone exists to leave (DECISIONS.md D47). A loop's cost
#: is its trip count, which is a fact about traces rather than about a spec, so a bounded repeat
#: is written as the steps it actually takes and an unbounded one is not expressible.
STEP_KINDS: tuple[str, ...] = ("generate", "tool", "retrieve", "verify", "retry")

#: Kinds that make a model call. The rest run in-process and spend nothing.
GENERATION_KINDS: tuple[str, ...] = ("generate", "verify", "retry")

#: Kinds that call a tool rather than a model.
TOOL_KINDS: tuple[str, ...] = ("tool", "retrieve")

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
        return self.kind in GENERATION_KINDS

    @property
    def is_tool(self) -> bool:
        return self.kind in TOOL_KINDS

    def model_role(self, pipeline: PipelineSpec) -> str:
        """Which tier runs this step. A step that names none runs on the pipeline's own.

        This is the lever M16 adds to the two a single call had: a graph can put its cheap work
        on a cheap model without the whole pipeline moving.
        """
        if self.spec is not None and self.spec.model_role:
            return self.spec.model_role
        return pipeline.model_role

    def max_tokens(self, pipeline: PipelineSpec) -> int:
        if self.spec is not None and self.spec.max_tokens is not None:
            return self.spec.max_tokens
        return pipeline.max_tokens

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "inputs": list(self.inputs)}
        if self.spec is not None and self.spec.tool:
            out["tool"] = self.spec.tool
        return out


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

    @property
    def terminal(self) -> Step:
        """The step whose output is the pipeline's answer.

        The one step nothing else consumes. A graph with two of them has not said which answer
        is the answer, and a grader picking either would be grading a coin flip — which is why
        :func:`compile_graph` refuses it rather than leaving the choice to run order.
        """
        return self.steps[-1] if len(self.steps) == 1 else _terminal(self.pipeline_id, self.steps)

    @property
    def generation_steps(self) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step.is_generation)

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


def _terminal(pipeline_id: str, steps: Sequence[Step]) -> Step:
    """The unique step nothing consumes."""
    leaves = [step for step in steps if not any(step.id in other.inputs for other in steps)]
    if len(leaves) == 1:
        return leaves[0]
    if not leaves:
        # Unreachable through _topological, which refuses a cycle first. Kept because a graph
        # with no leaf is a graph with no answer, and a silent IndexError here would surface
        # three layers away as a missing output.
        raise GraphError(f"{pipeline_id} has no final step; every step feeds another one")
    raise GraphError(
        f"{pipeline_id} ends in {len(leaves)} steps ({', '.join(sorted(s.id for s in leaves))}) "
        "and so does not say which one answers the task. Give the others a consumer, or delete "
        "the ones whose output nothing uses — a step whose output nothing reads is a step that "
        "costs money and changes no outcome."
    )


def _validate_executable(pipeline_id: str, steps: Sequence[Step]) -> None:
    """Refuse a graph that would compile and then not run, naming what is wrong with it.

    Every check here is one that would otherwise fail mid-run, after some of a split had been
    paid for. A recording that stops half way through because step four names a tool nobody
    implemented has spent real money to discover a typo.
    """
    for step in steps:
        if step.id in WORKLOAD_VARIABLES:
            raise GraphError(
                f"{pipeline_id} names a step {step.id!r}, which is also what every prompt calls "
                f"the workload's own {step.id}. A step's output is rendered as {{{{{step.id}}}}}, "
                "so this one would shadow it. Rename the step."
            )
        if step.is_tool:
            if step.spec is None or not step.spec.tool:
                raise GraphError(
                    f"{pipeline_id} step {step.id!r} is a {step.kind} step and names no tool. "
                    "A tool step is the tool it calls; there is nothing else for it to do."
                )
            # Raises with the implemented tools listed. Done at compile time on purpose: a
            # misspelled tool is found before the run rather than during it.
            get_tool(step.spec.tool)
        if step.kind == "retry":
            consumed = [other for other in steps if other.id in step.inputs]
            if not any(other.kind == "verify" for other in consumed):
                raise GraphError(
                    f"{pipeline_id} step {step.id!r} is a retry and consumes no verify step. A "
                    "retry that always runs is a second attempt, and the trace could not say "
                    "what triggered it — which is exactly the question 'did the verifier ever "
                    "change an outcome?' needs answered. Give it the verify step it reacts to."
                )
            if all(other.kind == "verify" for other in consumed):
                raise GraphError(
                    f"{pipeline_id} step {step.id!r} retries nothing: every step it consumes is "
                    "a verify step, so when the verifier is satisfied there is no answer for it "
                    "to fall back on. Give it the step whose answer it revises."
                )
        if step.is_generation and step.spec is not None and not step.spec.user:
            raise GraphError(
                f"{pipeline_id} step {step.id!r} makes a model call and has no user blocks, so "
                "there is nothing to send."
            )
    _terminal(pipeline_id, steps)


def compile_graph(pipeline: PipelineSpec) -> Graph:
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
    steps = _topological(pipeline.id, pipeline.steps)
    _validate_executable(pipeline.id, steps)
    return Graph(pipeline_id=pipeline.id, steps=steps)
