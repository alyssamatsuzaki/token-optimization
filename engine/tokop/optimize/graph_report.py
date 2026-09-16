"""Run a graph workload, find what its shape costs, and prove the deletion (UPGRADE_V4.md M16c).

``build_report`` is the demo's report: it replays a committed recording of a single-call
pipeline, fits a scorer, searches thresholds, simulates a cascade. None of that applies to a
graph. What applies to a graph is a different question — *which step is buying nothing* — and it
is answered by running the pipeline, reading the traces, and comparing the pipeline against
itself with a step removed.

Three decisions worth stating, because each could have been made worse quietly.

**There is no recording and there is not meant to be one.** ``tokop/recorder.py`` implements the
single-call spend plan from SPEC.md section 6 — the B2 matrix at three tiers, then the remaining
single-call pipelines — and it is left exactly as it is. A graph workload runs here against the
deterministic in-process provider: no socket, no key, no money, ``origin: simulated`` on every
row, and nothing it produces appears in the README or on a screen.

**Both arms see the same simulator.** The in-process responder seeds its answers on a pipeline
id, so two pipelines that differ only by a deleted step would otherwise be handed *different*
answers and the deletion's measured effect would be the simulator's noise. It is seeded on the
workload id here instead, which says the thing that is actually true: the same prompt to the
same model returns the same answer.

**A deletion that provably changes nothing is reported as that.** When the deleted step's verdict
reaches nothing, the two arms answer with byte-identical text and the accuracy difference is
zero by construction. The interval machinery still runs and still reports, because a proof that
skipped it would be a proof with a hole; what the report adds is the sentence saying the zero was
structural, so nobody reads a bootstrap where an argument belongs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokop.adapters.factory import simulated_profiles
from tokop.adapters.simulated import SimulatedAdapter
from tokop.core.pricing import PriceSnapshot
from tokop.core.registry import Registry, load_registry
from tokop.core.stats import DEFAULT_SEED
from tokop.optimize.graph import Graph, compile_graph
from tokop.optimize.graph_findings import GraphStats, StepStats, deletable_step, graph_findings
from tokop.optimize.lint import Finding
from tokop.optimize.proof import ArmResult, Proof, ProofCost, build_proof
from tokop.paths import repo_root
from tokop.workloads.bundle import Bundle, load_bundle
from tokop.workloads.grading import grade
from tokop.workloads.responder import GroundedResponder
from tokop.workloads.runner import Runner, RunResult, TierProfile
from tokop.workloads.spec import WorkloadError, WorkloadSpec, load_workload

#: The date the price snapshot is taken at, so a graph run is costed against the same prices as
#: every other number in this repository rather than against whatever today is.
PRICES_TAKEN = date(2026, 9, 11)


class GraphReportError(RuntimeError):
    """A graph workload could not be run or compared."""


@dataclass(frozen=True)
class GraphRun:
    """One graph pipeline over the test split, graded."""

    pipeline_id: str
    graph: Graph
    result: RunResult
    stats: GraphStats
    task_ids: tuple[str, ...]
    correct: tuple[int, ...]
    cost_usd: tuple[Decimal, ...]
    outputs: tuple[str, ...]

    @property
    def accuracy(self) -> float:
        return sum(self.correct) / len(self.correct) if self.correct else 0.0


@dataclass(frozen=True)
class GraphReport:
    """Everything running a graph workload produced."""

    workload: WorkloadSpec
    runs: dict[str, GraphRun]
    #: Findings for every graph pipeline this workload declares. The deletion below is chosen
    #: from the baseline's, but a pipeline nobody proves still has traces worth reading.
    findings_by_pipeline: dict[str, list[Finding]]
    baseline_id: str
    candidate_id: str | None
    deleted_step: str | None
    proof: Proof | None
    structural_note: str

    @property
    def findings(self) -> list[Finding]:
        """The baseline's findings: the shape as shipped, which is what a reader is asking about."""
        return self.findings_by_pipeline[self.baseline_id]

    def as_dict(self) -> dict[str, Any]:
        return {
            "workload": {"id": self.workload.id, "name": self.workload.name},
            "origin": "simulated",
            "graphs": {pid: run.graph.as_dict() for pid, run in sorted(self.runs.items())},
            "runs": {
                pid: {
                    "n": len(run.task_ids),
                    "accuracy": run.accuracy,
                    "total_cost_usd": str(run.result.total_cost),
                    "calls": sum(len(t.calls) for t in run.result.tasks),
                    "steps": [
                        {
                            "id": step.id,
                            "kind": step.kind,
                            "model": step.model,
                            "calls": step.calls,
                            "cost_usd": str(step.cost_usd),
                        }
                        for step in run.stats.steps
                    ],
                }
                for pid, run in sorted(self.runs.items())
            },
            "findings": [f.as_dict() for f in self.findings],
            "findings_by_pipeline": {
                pid: [f.as_dict() for f in found]
                for pid, found in sorted(self.findings_by_pipeline.items())
            },
            "deletion": {
                "baseline": self.baseline_id,
                "candidate": self.candidate_id,
                "step": self.deleted_step,
                "structural_note": self.structural_note,
            },
            "proof": self.proof.as_dict() if self.proof is not None else None,
        }


def _adapter(workload: WorkloadSpec, bundle: Bundle, registry: Registry) -> SimulatedAdapter:
    """The deterministic in-process provider, seeded on the workload rather than the pipeline."""
    if workload.simulation is None:
        raise GraphReportError(
            f"{workload.id} declares no `simulation:` block. A graph workload with no committed "
            "recording has nothing to answer its calls, and this build will not invent rates."
        )
    tiers = {role: TierProfile(registry.roles[role], role) for role in ("cheap", "mid", "frontier")}
    responder = GroundedResponder(
        items=list(bundle.items),
        grounding=bundle.grounding,
        tiers=tiers,
        simulation=workload.simulation,
        # Deliberately the workload, not the pipeline. See the module docstring: seeding on the
        # pipeline would hand two pipelines that share an answering step different answers, and
        # the deletion's measured effect would be that difference.
        pipeline_id=workload.id,
        output_contract="json_answer",
    )
    return SimulatedAdapter(simulated_profiles(registry, list(registry.models)), responder)


def _step_stats(
    workload: WorkloadSpec,
    graph: Graph,
    result: RunResult,
    registry: Registry,
    snapshot: PriceSnapshot,
    *,
    successes: int,
) -> GraphStats:
    """Aggregate one run's traces per step."""
    pipeline = workload.pipeline(result.pipeline_id)
    steps: list[StepStats] = []
    chars = 0
    tokens = 0
    for node in graph.steps:
        assert node.spec is not None
        traces = [
            t.step_output(node.id) for t in result.tasks if any(s.id == node.id for s in t.steps)
        ]
        calls = [c for t in result.tasks for c in t.calls if c.step == node.id]
        arguments = tuple(
            s.arguments for t in result.tasks for s in t.steps if s.id == node.id and not s.called
        )
        request_texts = tuple(
            c.request.system_text
            + "\n\n"
            + (c.request.messages[0].text if c.request.messages else "")
            for c in calls
        )
        input_cost = Decimal(0)
        output_cost = Decimal(0)
        input_tokens = 0
        output_tokens = 0
        model = ""
        for call in calls:
            model = call.response.model
            breakdown = snapshot.cost(model, call.response.usage)
            for bucket, amount in breakdown.by_bucket.items():
                if bucket.startswith("output"):
                    output_cost += amount
                else:
                    input_cost += amount
            input_tokens += call.response.usage.total_input
            output_tokens += call.response.usage.total_output
        for text in request_texts:
            chars += len(text)
        tokens += input_tokens
        steps.append(
            StepStats(
                id=node.id,
                kind=node.kind,
                model=model,
                called=node.is_generation,
                calls=len(calls),
                price=snapshot.price_for(model) if model else None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                input_cost_usd=input_cost,
                output_cost_usd=output_cost,
                arguments=arguments,
                outputs=tuple(traces),
                request_texts=request_texts,
                tool=node.spec.tool,
                inputs=node.inputs,
            )
        )
    return GraphStats(
        pipeline_id=pipeline.id,
        graph=graph,
        steps=tuple(steps),
        task_count=len(result.tasks),
        successes=successes,
        # Measured from this run's own requests rather than assumed, so a shared block is put on
        # the same scale as the bill the same requests produced.
        tokens_per_char=(tokens / chars) if chars else 0.0,
    )


def run_graph_pipeline(
    workload: WorkloadSpec,
    bundle: Bundle,
    registry: Registry,
    snapshot: PriceSnapshot,
    pipeline_id: str,
) -> GraphRun:
    """Run one graph pipeline over the test split and grade its output."""
    pipeline = workload.pipeline(pipeline_id)
    graph = compile_graph(pipeline)
    if graph.is_single_call:
        raise GraphReportError(
            f"{pipeline_id} declares no steps, so it is a single call and `tokop report` is what "
            "reports on it. This command is for pipelines that are graphs."
        )
    runner = Runner(
        workload,
        registry,
        snapshot,
        _adapter(workload, bundle, registry),
        provider="simulated",
        grounding=bundle.grounding,
        origin="simulated",
    )
    items = list(bundle.test)
    result = asyncio.run(runner.run_pipeline(pipeline_id, items, split="test"))
    by_id = {item.id: item for item in items}
    correct: list[int] = []
    costs: list[Decimal] = []
    outputs: list[str] = []
    for task in result.tasks:
        item = by_id[task.task_id]
        outcome = grade(task.output, item.gold, item.answer_type, item.aliases)
        correct.append(int(outcome.correct and not task.failed))
        outputs.append(task.output)
        costs.append(
            sum(
                (snapshot.cost(c.response.model, c.response.usage).total for c in task.calls),
                Decimal(0),
            )
        )
    stats = _step_stats(workload, graph, result, registry, snapshot, successes=sum(correct))
    return GraphRun(
        pipeline_id=pipeline_id,
        graph=graph,
        result=result,
        stats=stats,
        task_ids=tuple(t.task_id for t in result.tasks),
        correct=tuple(correct),
        cost_usd=tuple(costs),
        outputs=tuple(outputs),
    )


def _deletion_pair(workload: WorkloadSpec, step_id: str, baseline_id: str) -> str | None:
    """The pipeline that is ``baseline_id`` minus ``step_id``, if the workload ships one."""
    baseline = compile_graph(workload.pipeline(baseline_id))
    wanted = {s.id for s in baseline.steps} - {step_id}
    for pipeline_id, pipeline in sorted(workload.pipelines.items()):
        if pipeline_id == baseline_id or not pipeline.is_graph:
            continue
        other = compile_graph(pipeline)
        if {s.id for s in other.steps} == wanted and other.output_step == baseline.output_step:
            return pipeline_id
    return None


def build_graph_report(
    workload_path: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = 5000,
) -> GraphReport:
    """Run every graph pipeline this workload declares, rank the findings, prove the deletion."""
    path = Path(workload_path)
    workload = load_workload(path if path.is_absolute() else repo_root() / path)
    graph_pipelines = sorted(pid for pid, p in workload.pipelines.items() if p.is_graph)
    if not graph_pipelines:
        raise GraphReportError(
            f"{workload.id} declares no pipeline with `steps:`, so it has no graph to report on. "
            "`tokop report` is what reports on single-call pipelines."
        )
    registry = load_registry()
    bundle = load_bundle(workload)
    snapshot = registry.snapshot(list(registry.roles.values()), PRICES_TAKEN)

    runs = {
        pipeline_id: run_graph_pipeline(workload, bundle, registry, snapshot, pipeline_id)
        for pipeline_id in graph_pipelines
    }
    # The baseline is the pipeline with the most steps: the shape as shipped, before anything was
    # taken out of it. Ties break on the id so the choice does not depend on dict order.
    baseline_id = sorted(runs, key=lambda pid: (-len(runs[pid].graph.steps), pid))[0]
    baseline = runs[baseline_id]
    by_pipeline = {pid: graph_findings(run.stats) for pid, run in sorted(runs.items())}
    findings = by_pipeline[baseline_id]

    step_id = deletable_step(baseline.stats, findings)
    candidate_id = _deletion_pair(workload, step_id, baseline_id) if step_id else None
    proof: Proof | None = None
    note = ""
    if step_id is not None and candidate_id is not None:
        candidate = runs[candidate_id]
        identical = sum(
            1 for a, b in zip(baseline.outputs, candidate.outputs, strict=True) if a == b
        )
        note = (
            f"{candidate_id} is {baseline_id} without {step_id!r}. Nothing read that step's "
            f"output, so the two pipelines answer with the same text: {identical} of "
            f"{len(baseline.outputs)} answers are byte-identical. The accuracy difference below "
            "is zero by construction and the interval around it is a formality, reported because "
            "a proof that skipped it would be a proof with a hole."
            if identical == len(baseline.outputs)
            else (
                f"{candidate_id} is {baseline_id} without {step_id!r}. {identical} of "
                f"{len(baseline.outputs)} answers are byte-identical, so the deletion did move "
                "some outcomes and the interval below is doing real work."
            )
        )
        proof = build_proof(
            ArmResult(
                pipeline_id=baseline_id,
                label=workload.pipeline(baseline_id).name,
                task_ids=baseline.task_ids,
                correct=baseline.correct,
                cost_usd=baseline.cost_usd,
                model_ids=tuple(baseline.result.model_ids),
                origin="simulated",
            ),
            ArmResult(
                pipeline_id=candidate_id,
                label=workload.pipeline(candidate_id).name,
                task_ids=candidate.task_ids,
                correct=candidate.correct,
                cost_usd=candidate.cost_usd,
                model_ids=tuple(candidate.result.model_ids),
                origin="simulated",
            ),
            ProofCost(
                baseline_run_usd=baseline.result.total_cost,
                calibration_runs_usd=Decimal(0),
                candidate_run_usd=candidate.result.total_cost,
                scorer_and_judge_usd=Decimal(0),
                prewarming_usd=Decimal(0),
                other_pipelines_usd=sum(
                    (
                        run.result.total_cost
                        for pid, run in runs.items()
                        if pid not in (baseline_id, candidate_id)
                    ),
                    Decimal(0),
                ),
            ),
            {"test": len(bundle.test), "calibration": len(bundle.calibration)},
            margin=workload.margin,
            seed=seed,
            resamples=resamples,
            items_by_id=bundle.by_id,
            operating_point_note=(
                "Nothing was calibrated. The candidate is the baseline with one step removed, "
                "and which step was chosen came from the baseline's own traces."
            ),
        )
    elif step_id is not None:
        note = (
            f"The findings propose deleting {step_id!r} from {baseline_id}, and this workload "
            "ships no pipeline that is that. Write one and re-run; the proof needs both arms."
        )
    else:
        note = (
            "No finding proposes a deletion this build can prove. G01, G02 and G05 propose "
            "rewriting a step rather than removing one, and a rewrite cannot be evaluated "
            "without the rewritten pipeline."
        )
    return GraphReport(
        workload=workload,
        runs=runs,
        findings_by_pipeline=by_pipeline,
        baseline_id=baseline_id,
        candidate_id=candidate_id,
        deleted_step=step_id,
        proof=proof,
        structural_note=note,
    )


def load_graph_workload(path: str | Path) -> WorkloadSpec:
    """Load a workload and refuse one that has no graph, with the reason."""
    spec = load_workload(Path(path))
    if not any(p.is_graph for p in spec.pipelines.values()):
        raise WorkloadError(f"{spec.id} declares no pipeline with `steps:`")
    return spec
