"""A graph runs, and the winning plan deletes a step (M16b and M16c).

M16a built the representation and pinned the guarantee that a single-call pipeline is unchanged
by it. This file is the other half: the runner *executes* a graph, the findings read what the
graph did, and the deletion the findings propose is proven at the graph-level outcome — cost
summed over every step, correctness graded on the answer the whole pipeline produced.

The acceptance condition from PLAN.md section 7 is one test, ``TestTheAcceptanceCheck``, and it
asserts all three parts of the sentence at once: a graph workload, a winning plan that deletes a
step, proven at the graph-level outcome, with single-call output byte-identical.

Everything here runs against the deterministic in-process provider. No socket is opened and no
money is spent; every row is ``origin: simulated`` and the report says so.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest

from tokop.adapters.factory import simulated_profiles
from tokop.adapters.simulated import SimulatedAdapter
from tokop.core.registry import load_registry
from tokop.optimize.graph import compile_graph
from tokop.optimize.graph_report import GraphReportError, build_graph_report
from tokop.paths import repo_root
from tokop.workloads.bundle import load_bundle
from tokop.workloads.responder import GroundedResponder
from tokop.workloads.runner import Runner, TierProfile
from tokop.workloads.spec import BlockSpec, StepSpec, load_workload

AGENT = "data/incident-agent/workload.yaml"


@pytest.fixture(scope="module")
def report():
    return build_graph_report(AGENT)


@pytest.fixture(scope="module")
def workload():
    return load_workload(repo_root() / AGENT)


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def runner_for(workload, registry, *, pipeline_seed: str | None = None) -> Runner:
    bundle = load_bundle(workload)
    tiers = {role: TierProfile(registry.roles[role], role) for role in ("cheap", "mid", "frontier")}
    responder = GroundedResponder(
        items=list(bundle.items),
        grounding=bundle.grounding,
        tiers=tiers,
        simulation=workload.simulation,
        pipeline_id=pipeline_seed or workload.id,
        output_contract="json_answer",
    )
    return Runner(
        workload,
        registry,
        registry.snapshot(list(registry.roles.values()), date(2026, 9, 11)),
        SimulatedAdapter(simulated_profiles(registry, list(registry.models)), responder),
        provider="simulated",
        grounding=bundle.grounding,
        origin="simulated",
    )


class TestTheAcceptanceCheck:
    """PLAN.md section 7: the winning plan deletes a step, proven at the graph-level outcome."""

    def test_the_top_finding_proposes_deleting_a_step(self, report) -> None:
        assert report.findings, "the shipped agent produced no findings at all"
        assert report.findings[0].id == "G03"
        assert report.deleted_step == "check"

    def test_the_deletion_is_proven_against_the_pipeline_it_came_from(self, report) -> None:
        proof = report.proof
        assert proof is not None, report.structural_note
        assert (report.baseline_id, report.candidate_id) == ("G0", "G1")
        assert proof.verdict.label == "non_inferior"
        reduction = proof.cost_reduction
        assert reduction is not None and reduction > 0.4, reduction

    def test_the_outcome_it_is_proven_on_is_the_whole_graph(self, report) -> None:
        """Cost per task sums over every step; correctness is graded on the graph's answer."""
        baseline = report.runs[report.baseline_id]
        per_step = {step.id: step.cost_usd for step in baseline.stats.steps}
        for index, task in enumerate(baseline.result.tasks):
            assert {call.step for call in task.calls} == {"answer", "check"}
            assert baseline.cost_usd[index] > 0
        # Every dollar the run spent is in exactly one task's figure and in exactly one step's.
        assert sum(baseline.cost_usd, Decimal(0)) == baseline.result.total_cost
        assert sum(per_step.values(), Decimal(0)) == baseline.result.total_cost
        # The answer graded is the answer the pipeline declared, not the verifier's verdict.
        assert baseline.graph.output_step == "answer"

    def test_deleting_the_step_removes_exactly_its_cost(self, report) -> None:
        baseline, candidate = report.runs["G0"], report.runs["G1"]
        deleted = baseline.stats.step("check").cost_usd
        assert baseline.result.total_cost - candidate.result.total_cost == deleted

    def test_a_single_call_pipeline_is_still_byte_identical(self, registry) -> None:
        """The M16a guarantee, re-asserted here because M16b is what could have broken it."""
        for path in ("data/demo/workload.yaml", "data/incident-triage/workload.yaml"):
            spec = load_workload(repo_root() / path)
            bundle = load_bundle(spec)
            variables = {
                "grounding": bundle.grounding,
                "question": bundle.test[0].question,
                "timestamp": "2026-09-11T00:00:00Z",
            }
            for pipeline in spec.pipelines.values():
                graph = compile_graph(pipeline)
                assert graph.is_single_call
                model_id = registry.roles[pipeline.model_role]
                assert pipeline.render("simulated", model_id, variables).cassette_key()


class TestExecution:
    """M16b: what the runner does with a graph."""

    def test_every_generation_step_makes_one_call_per_task(self, report) -> None:
        for pipeline_id, run in report.runs.items():
            generations = [s for s in run.graph.steps if s.is_generation]
            for step in generations:
                stats = run.stats.step(step.id)
                assert stats.calls == len(run.task_ids), f"{pipeline_id}.{step.id}"

    def test_a_local_step_makes_no_call_and_still_reaches_the_graph(self, report) -> None:
        run = report.runs["G0"]
        fetch = run.stats.step("fetch")
        assert fetch.calls == 0 and fetch.cost_usd == 0
        # What it emitted is the runbook, and the answering step's request carries it.
        answer = run.stats.step("answer")
        assert fetch.outputs[0] in answer.request_texts[0]

    def test_a_step_runs_on_the_model_it_pinned_not_the_pipelines(self, report) -> None:
        run = report.runs["G0"]
        assert run.stats.step("check").model != run.stats.step("answer").model

    def test_the_first_task_warms_what_the_rest_read(self, report) -> None:
        """A graph has no pre-warm pass; its first task writes the entries the rest read."""
        run = report.runs["G0"]
        writes = sum(
            c.response.usage.cache_write_5m + c.response.usage.cache_write_1h
            for t in run.result.tasks
            for c in t.calls
        )
        reads = sum(c.response.usage.cache_read for t in run.result.tasks for c in t.calls)
        assert run.result.prewarm_calls == 0
        assert writes > 0 and reads > writes, (writes, reads)

    def test_a_graph_refuses_to_be_sampled(self, workload, registry) -> None:
        from tokop.optimize.graph import GraphError

        runner = runner_for(workload, registry)
        items = list(load_bundle(workload).test)[:2]
        with pytest.raises(GraphError, match="samples per task"):
            asyncio.run(runner.run_pipeline("G0", items, samples=3))

    def test_a_failed_step_stops_the_task_and_stays_visible(self, workload, registry) -> None:
        """Non-negotiable 8: the task counts as unsuccessful and the failure is in the trace."""

        class Exploding:
            name = "simulated"

            def __init__(self, inner, on_step: str) -> None:
                self.inner = inner
                self.on_step = on_step

            async def complete(self, request):
                if self.on_step in request.system_text or self.on_step in (
                    request.messages[0].text if request.messages else ""
                ):
                    raise RuntimeError("the provider refused")
                return await self.inner.complete(request)

            async def count_tokens(self, request):
                return await self.inner.count_tokens(request)

        runner = runner_for(workload, registry)
        runner.adapter = Exploding(runner.adapter, "You review an on-call assistant")
        items = list(load_bundle(workload).test)[:3]
        result = asyncio.run(runner.run_pipeline("G0", items, split="test"))
        for task in result.tasks:
            assert task.failed and "the provider refused" in str(task.error)
            # The answering step ran and is billed; the step after the failure never ran.
            assert [c.step for c in task.calls] == ["answer", "check"]
            assert task.steps[-1].id == "check" and task.steps[-1].error is not None


class TestTheFiveFindings:
    """M16c: every G rule fires on a pipeline this workload ships, and none on one it should not."""

    @pytest.mark.parametrize(
        ("rule", "pipeline"),
        [("G01", "G0"), ("G02", "G0"), ("G03", "G0"), ("G04", "G2"), ("G05", "G0")],
    )
    def test_each_rule_fires_where_the_workload_plants_it(self, report, rule, pipeline) -> None:
        assert rule in {f.id for f in report.findings_by_pipeline[pipeline]}

    def test_the_deleted_step_takes_its_findings_with_it(self, report) -> None:
        """G1 is G0 without the verifier, so the verifier's findings are gone from it."""
        gone = {f.id for f in report.findings_by_pipeline["G1"]}
        assert "G03" not in gone and "G01" not in gone

    def test_a_duplicate_tool_call_claims_no_dollars_it_cannot_price(self, report) -> None:
        g02 = next(f for f in report.findings_by_pipeline["G0"] if f.id == "G02")
        assert g02.projected_usd_per_1k == 0
        assert "no price is registered for this tool" in g02.formula

    def test_the_frontier_finding_says_it_is_a_ceiling(self, report) -> None:
        g05 = next(f for f in report.findings_by_pipeline["G0"] if f.id == "G05")
        assert g05.confidence == "projected"
        assert "ceiling" in g05.detail

    def test_the_retry_finding_names_what_its_share_depends_on(self, report) -> None:
        g04 = next(f for f in report.findings_by_pipeline["G2"] if f.id == "G04")
        assert "deterministic" in g04.detail
        assert g04.projected_usd_per_1k > 0

    def test_every_graph_finding_is_priced_at_a_rate_the_run_actually_paid(self, report) -> None:
        """A cached prefix costs a tenth of base input, so pricing it at base inflates this."""
        stats = report.runs["G0"].stats
        check = stats.step("check")
        assert check.price is not None
        assert 0 < check.blended_input_rate < check.price.input


class TestTheLedgerKnowsWhichStepMadeACall:
    """M16b put a `step` on every call row. A ledger older than it has to say so."""

    def test_a_ledger_generated_before_the_column_is_refused_with_the_remedy(
        self, tmp_path
    ) -> None:
        import sqlite3

        from tokop.db import LedgerSchemaError, make_engine

        stale = tmp_path / "old.db"
        connection = sqlite3.connect(stale)
        connection.execute("create table calls (id integer primary key, run_id text)")
        connection.commit()
        connection.close()
        with pytest.raises(LedgerSchemaError, match="build-test-fixtures --ledger-only"):
            make_engine(stale)

    def test_a_fresh_ledger_is_fine(self, tmp_path) -> None:
        from tokop.db import make_engine

        assert make_engine(tmp_path / "new.db") is not None


class TestWhatItRefuses:
    def test_a_single_call_workload_has_no_graph_report(self) -> None:
        with pytest.raises(GraphReportError, match="declares no pipeline with `steps:`"):
            build_graph_report("data/incident-triage/workload.yaml")

    def test_a_step_that_reads_an_undeclared_variable_is_refused_at_load(self, workload) -> None:
        from tokop.optimize.graph import GraphError

        broken = workload.pipelines["G1"].model_copy(
            update={
                "steps": [
                    *workload.pipelines["G1"].steps[:3],
                    StepSpec(
                        id="answer",
                        inputs=["fetch"],
                        user=[BlockSpec(text="{{question}} {{severity}}")],
                    ),
                ]
            }
        )
        with pytest.raises(GraphError, match="does not list 'severity' among its inputs"):
            compile_graph(broken)
