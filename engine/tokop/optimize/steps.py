"""What each step of a graph cost (UPGRADE_V4.md M16).

A single-call pipeline has one cost and one question: is it worth it. A graph has one cost per
step and a different question at each of them — is *this* step worth it — and the proposal worth
making about an agent is almost always to delete one. That proposal cannot be made from a total.

So this module is the breakdown, computed from the same call rows everything else is computed
from. Nothing here estimates: a step's cost is the sum of the calls it made, its share is that
sum over the run's, and a step that made no calls because the step before it was satisfied is
counted as skipped rather than as free.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from tokop.adapters.cassette import CassetteStore, ReplayAdapter
from tokop.core.pricing import PriceSnapshot
from tokop.core.registry import Registry
from tokop.optimize.graph import Graph, compile_graph
from tokop.workloads.bundle import Bundle
from tokop.workloads.runner import Runner, RunResult
from tokop.workloads.spec import WorkloadSpec


@dataclass(frozen=True)
class StepCost:
    """One step's share of what a run cost."""

    step: str
    kind: str
    #: Provider calls this step made. Zero for a tool step, which makes none.
    calls: int
    #: Tasks on which this step did not run at all.
    skipped: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    #: Characters a tool step returned, summed. What a tool costs is what it puts in the next
    #: step's prompt, so this is the figure that connects a free call to a paid one.
    tool_chars: int
    cost_usd: Decimal

    @property
    def is_free(self) -> bool:
        return self.calls == 0


@dataclass(frozen=True)
class GraphRun:
    """A graph run, broken down by step."""

    pipeline_id: str
    split: str
    tasks: int
    steps: tuple[StepCost, ...]
    prewarm_calls: int
    prewarm_cost_usd: Decimal
    total_cost_usd: Decimal

    @property
    def calls(self) -> int:
        return sum(step.calls for step in self.steps)

    @property
    def calls_per_task(self) -> float:
        return self.calls / self.tasks if self.tasks else 0.0

    def share(self, step: StepCost) -> float:
        """This step's share of the run's cost, pre-warming included in the denominator."""
        if not self.total_cost_usd:
            return 0.0
        return float(step.cost_usd / self.total_cost_usd)


def step_costs(graph: Graph, result: RunResult, snapshot: PriceSnapshot) -> GraphRun:
    """Break one run down by the step that made each call."""
    by_step: dict[str, dict[str, int]] = {
        step.id: {"calls": 0, "skipped": 0, "tools": 0, "input": 0, "output": 0, "chars": 0}
        for step in graph.steps
    }
    cost: dict[str, Decimal] = {step.id: Decimal(0) for step in graph.steps}
    for task in result.tasks:
        for call in task.calls:
            # A call whose step is not in the graph cannot happen: the runner tags every call
            # with the step that rendered it. Reading it back through the graph rather than
            # trusting the string is what makes that true rather than assumed.
            row = by_step[graph.step(call.step).id]
            row["calls"] += 1
            row["input"] += call.response.usage.total_input
            row["output"] += call.response.usage.total_output
            cost[call.step] += snapshot.cost(call.response.model, call.response.usage).total
        for tool_call in task.tool_calls:
            row = by_step[tool_call.step]
            row["tools"] += 1
            row["chars"] += tool_call.result.chars
        for step_id, _reason in task.skipped:
            by_step[step_id]["skipped"] += 1

    prewarm_cost = sum(
        (snapshot.cost(response.model, response.usage).total for _, response in result.prewarm),
        Decimal(0),
    )
    return GraphRun(
        pipeline_id=result.pipeline_id,
        split=result.split,
        tasks=len(result.tasks),
        steps=tuple(
            StepCost(
                step=step.id,
                kind=step.kind,
                calls=by_step[step.id]["calls"],
                skipped=by_step[step.id]["skipped"],
                tool_calls=by_step[step.id]["tools"],
                input_tokens=by_step[step.id]["input"],
                output_tokens=by_step[step.id]["output"],
                tool_chars=by_step[step.id]["chars"],
                cost_usd=cost[step.id],
            )
            for step in graph.steps
        ),
        prewarm_calls=result.prewarm_calls,
        prewarm_cost_usd=prewarm_cost,
        total_cost_usd=result.total_cost,
    )


def replay_graph(
    workload: WorkloadSpec,
    registry: Registry,
    bundle: Bundle,
    snapshot: PriceSnapshot,
    store: CassetteStore,
    pipeline_id: str,
    *,
    split: str = "test",
    tier: str = "frontier",
) -> tuple[Graph, RunResult]:
    """Re-run one pipeline from its cassettes and return the graph and the run.

    Deliberately not ``report.replay_run``, which returns the flattened shape the proof needs
    and drops what a graph is interesting for: which step made a call, which tool ran, and which
    steps did not run at all. Both replay the same cassettes through the same runner, so neither
    is a second source of numbers.
    """
    items = list(bundle.calibration if split == "calibration" else bundle.test)
    runner = Runner(
        workload,
        registry,
        snapshot,
        ReplayAdapter(store, name="simulated"),
        provider="simulated",
        grounding=bundle.grounding,
        origin="simulated",
    )
    result = asyncio.run(
        runner.run_pipeline(pipeline_id, items, model_id=registry.roles[tier], split=split)
    )
    return compile_graph(workload.pipeline(pipeline_id)), result
