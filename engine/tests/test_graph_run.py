"""A graph runs (M16b, UPGRADE_V4.md M16).

M16a could compile a pipeline into a graph and validate it, and then the runner ran it as
though it had not: one call per task, whatever the pipeline declared. This file is the
difference. What it pins:

* steps run in the order the compiler fixed, and each sees **only** what it declared it
  consumes, because a step that can reach an output it did not declare is a dependency edge
  nobody wrote down;
* the cost of a task is the sum over its steps, which is what lets the optimizer propose
  deleting one rather than only swapping a model;
* a tool call spends nothing and is never a provider call, and its cost lands where it really
  lands — in the prompt of whatever consumes it;
* a retry runs only when the verifier it consumes objected, and a skipped retry returns what it
  would have retried rather than nothing;
* the single-call path is untouched, still rendering an empty ``step`` and therefore the same
  cassette key as every committed fixture.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tokop.adapters.base import AdapterError, CassetteMiss, LLMRequest
from tokop.adapters.cassette import CassetteStore, ReplayAdapter
from tokop.adapters.factory import simulated_profiles
from tokop.adapters.recording import RecordingAdapter
from tokop.adapters.simulated import SimulatedAdapter
from tokop.core.budget import BudgetExceeded, SpendGuard
from tokop.core.registry import load_registry
from tokop.db import Call, ToolCall, make_engine, session_scope
from tokop.optimize.graph import GraphError, compile_graph
from tokop.optimize.steps import step_costs
from tokop.paths import fixtures_dir, repo_root
from tokop.workloads.bundle import load_bundle
from tokop.workloads.responder import GroundedResponder
from tokop.workloads.runner import Runner, TierProfile, persist
from tokop.workloads.spec import BlockSpec, PipelineSpec, StepSpec, WorkloadError, load_workload
from tokop.workloads.tools import ToolError

WORKLOAD = "data/catalogue-agent/workload.yaml"


@pytest.fixture(scope="module")
def registry():
    return load_registry()


@pytest.fixture(scope="module")
def workload():
    return load_workload(repo_root() / WORKLOAD)


@pytest.fixture(scope="module")
def bundle(workload):
    return load_bundle(workload)


@pytest.fixture(scope="module")
def snapshot(registry):
    return registry.snapshot(list(registry.roles.values()), date(2026, 9, 11))


def build_runner(workload, registry, snapshot, bundle, pipeline_id, tmp_path, *, guard=None):
    """A runner over the deterministic in-process provider. Opens no socket, spends nothing."""
    pipeline = workload.pipeline(pipeline_id)
    tiers = {r: TierProfile(registry.roles[r], r) for r in ("cheap", "mid", "frontier")}
    responder = GroundedResponder(
        items=list(bundle.items),
        grounding=bundle.grounding,
        tiers=tiers,
        simulation=workload.simulation,
        pipeline_id=pipeline_id,
        output_contract=pipeline.output_contract,
        graph=compile_graph(pipeline) if pipeline.is_graph else None,
    )
    adapter = RecordingAdapter(
        SimulatedAdapter(simulated_profiles(registry, list(registry.models)), responder),
        CassetteStore(Path(tmp_path) / "cassettes"),
        origin="simulated",
        on_spend=guard,
    )
    return Runner(
        workload,
        registry,
        snapshot,
        adapter,
        provider="simulated",
        grounding=bundle.grounding,
        origin="simulated",
    )


def run(workload, registry, snapshot, bundle, pipeline_id, items, tmp_path, **kwargs):
    runner = build_runner(
        workload, registry, snapshot, bundle, pipeline_id, tmp_path, guard=kwargs.pop("guard", None)
    )
    return asyncio.run(
        runner.run_pipeline(
            pipeline_id, items, model_id=registry.roles["frontier"], split="test", **kwargs
        )
    )


class TestTheGraphRuns:
    def test_every_declared_step_makes_its_calls(
        self, workload, registry, snapshot, bundle, tmp_path
    ) -> None:
        items = list(bundle.test)[:12]
        result = run(workload, registry, snapshot, bundle, "B0", items, tmp_path)

        assert len(result.tasks) == len(items)
        made = {call.step for task in result.tasks for call in task.calls}
        # `lookup` is a tool step and makes no provider call; the other three do.
        assert made <= {"draft", "check", "revise"}
        assert {"draft", "check"} <= made
        for task in result.tasks:
            assert len(task.calls_for("draft")) == 1
            assert len(task.calls_for("check")) == 1
            assert len(task.tool_calls) == 1

    def test_a_step_runs_after_the_steps_it_consumes(
        self, workload, registry, snapshot, bundle, tmp_path
    ) -> None:
        """The check reviews the draft, so it cannot have been sent before the draft came back."""
        items = list(bundle.test)[:6]
        result = run(workload, registry, snapshot, bundle, "B0", items, tmp_path)
        for task in result.tasks:
            drafted = task.calls_for("draft")[0].response.text
            reviewed = task.calls_for("check")[0].request.messages[-1].blocks[-1].text
            assert reviewed == drafted

    def test_a_tool_step_spends_nothing_and_is_not_a_provider_call(
        self, workload, registry, snapshot, bundle, tmp_path
    ) -> None:
        items = list(bundle.test)[:8]
        result = run(workload, registry, snapshot, bundle, "B1", items, tmp_path)
        for task in result.tasks:
            assert [call.step for call in task.calls] == ["draft"]
            assert len(task.tool_calls) == 1
            assert task.tool_calls[0].step == "lookup"
            assert task.tool_calls[0].result.tool == "grounding-lookup-v1"
            # Where the tool's cost actually is: in the prompt of the step that consumes it.
            assert (
                task.tool_calls[0].result.text
                in task.calls_for("draft")[0].request.messages[-1].text
            )

    def test_a_step_sees_only_what_it_declared(self) -> None:
        """A step reaching an output it did not declare is a dependency edge nobody wrote down."""
        pipeline = PipelineSpec(
            id="P",
            name="n",
            description="d",
            model_role="frontier",
            max_tokens=50,
            steps=[
                StepSpec(id="first", user=[BlockSpec(text="{{question}}")]),
                # `second` renders {{first}} without declaring it as an input.
                StepSpec(id="second", inputs=[], user=[BlockSpec(text="{{first}}")]),
                StepSpec(id="join", inputs=["first", "second"], user=[BlockSpec(text="x")]),
            ],
        )
        graph = compile_graph(pipeline)
        with pytest.raises(WorkloadError, match="the workload supplies"):
            Runner.render_step(
                _bare_runner(), pipeline, graph.step("second"), "m", {"question": "q"}
            )

    def test_cost_is_the_sum_over_the_steps(
        self, workload, registry, snapshot, bundle, tmp_path
    ) -> None:
        """The figure that makes 'delete a step' a proposal rather than an opinion."""
        items = list(bundle.test)[:10]
        result = run(workload, registry, snapshot, bundle, "B0", items, tmp_path)
        breakdown = step_costs(compile_graph(workload.pipeline("B0")), result, snapshot)
        by_hand = sum((step.cost_usd for step in breakdown.steps), Decimal(0))
        assert by_hand + breakdown.prewarm_cost_usd == result.total_cost
        assert breakdown.calls == sum(len(task.calls) for task in result.tasks)


class TestTheRetry:
    def test_it_runs_only_when_the_verifier_objected(
        self, workload, registry, snapshot, bundle, tmp_path
    ) -> None:
        from tokop.workloads.verification import parse_verdict

        items = list(bundle.test)[:40]
        result = run(workload, registry, snapshot, bundle, "B0", items, tmp_path)
        objected = 0
        for task in result.tasks:
            verdict = parse_verdict(task.calls_for("check")[0].response.text)
            ran = bool(task.calls_for("revise"))
            assert ran == (verdict.correct == 0), task.task_id
            objected += int(ran)
        # Both branches have to be exercised or the test proves nothing about the condition.
        assert 0 < objected < len(items)

    def test_a_skipped_retry_returns_what_it_would_have_retried(
        self, workload, registry, snapshot, bundle, tmp_path
    ) -> None:
        items = list(bundle.test)[:30]
        result = run(workload, registry, snapshot, bundle, "B0", items, tmp_path)
        skipped = [task for task in result.tasks if not task.calls_for("revise")]
        assert skipped, "no task skipped the retry, so this proves nothing"
        for task in skipped:
            assert tuple(step for step, _ in task.skipped) == ("revise",)
            assert task.output == task.calls_for("draft")[0].response.text

    def test_the_reason_a_step_did_not_run_is_recorded(
        self, workload, registry, snapshot, bundle, tmp_path
    ) -> None:
        items = list(bundle.test)[:20]
        result = run(workload, registry, snapshot, bundle, "B0", items, tmp_path)
        reasons = {reason for task in result.tasks for _, reason in task.skipped}
        assert reasons, "a skipped step with no reason is a gap, not a result"
        assert all("no objection" in reason for reason in reasons)


class TestWhatIsRefused:
    def test_a_graph_refuses_repeated_samples_by_name(
        self, workload, registry, snapshot, bundle, tmp_path
    ) -> None:
        with pytest.raises(AdapterError, match="samples per task"):
            run(
                workload,
                registry,
                snapshot,
                bundle,
                "B0",
                list(bundle.test)[:2],
                tmp_path,
                samples=3,
            )

    def test_a_loop_step_cannot_be_declared(self) -> None:
        """M16a listed `loop` as a kind and nothing could run one (DECISIONS.md D47)."""
        with pytest.raises(ValueError, match="loop"):
            StepSpec(id="again", kind="loop")

    def test_a_retry_with_no_verifier_is_refused(self) -> None:
        pipeline = _pipeline(
            StepSpec(id="draft", user=[BlockSpec(text="q")]),
            StepSpec(id="again", kind="retry", inputs=["draft"], user=[BlockSpec(text="q")]),
        )
        with pytest.raises(GraphError, match="consumes no verify step"):
            compile_graph(pipeline)

    def test_a_retry_that_retries_nothing_is_refused(self) -> None:
        pipeline = _pipeline(
            StepSpec(id="draft", user=[BlockSpec(text="q")]),
            StepSpec(id="check", kind="verify", inputs=["draft"], user=[BlockSpec(text="q")]),
            StepSpec(id="again", kind="retry", inputs=["check"], user=[BlockSpec(text="q")]),
        )
        with pytest.raises(GraphError, match="retries nothing"):
            compile_graph(pipeline)

    def test_a_tool_step_with_no_tool_is_refused(self) -> None:
        with pytest.raises(GraphError, match="names no tool"):
            compile_graph(_pipeline(StepSpec(id="fetch", kind="retrieve")))

    def test_a_tool_nobody_implemented_is_refused_by_name(self) -> None:
        """A `ToolError`, not a `GraphError`: the graph is fine and the tool does not exist.

        Both are `WorkloadError`, which is what every caller catches, so the distinction costs
        nothing and the message names the thing that is actually wrong.
        """
        pipeline = _pipeline(StepSpec(id="fetch", kind="retrieve", tool="vector-search-v9"))
        with pytest.raises(ToolError, match="grounding-lookup-v1"):
            compile_graph(pipeline)
        assert issubclass(ToolError, WorkloadError)

    def test_a_step_may_not_shadow_a_workload_variable(self) -> None:
        pipeline = _pipeline(
            StepSpec(id="grounding", kind="retrieve", tool="grounding-lookup-v1", query="q")
        )
        with pytest.raises(GraphError, match="shadow"):
            compile_graph(pipeline)

    def test_two_final_steps_are_refused_because_neither_is_the_answer(self) -> None:
        pipeline = _pipeline(
            StepSpec(id="a", user=[BlockSpec(text="q")]),
            StepSpec(id="b", user=[BlockSpec(text="q")]),
        )
        with pytest.raises(GraphError, match="does not say which one answers"):
            compile_graph(pipeline)

    def test_a_budget_refusal_ends_the_run_rather_than_failing_every_task(
        self, workload, registry, snapshot, bundle, tmp_path
    ) -> None:
        """A cap is the operator's limit, not a model failure (DECISIONS.md D42).

        Absorbing it would record the remaining tasks as unsuccessful, spend the whole budget
        doing so, and hand the report an accuracy figure that describes a budget.
        """
        from tokop.core.tokenize import get_base_counter
        from tokop.recorder import GuardSink

        guard = GuardSink(SpendGuard(run_cap=Decimal("0.0001")), snapshot, get_base_counter())
        with pytest.raises(BudgetExceeded):
            run(
                workload,
                registry,
                snapshot,
                bundle,
                "B0",
                list(bundle.test)[:5],
                tmp_path,
                guard=guard,
            )


class TestTheSingleCallPathIsUntouched:
    def test_a_pipeline_with_no_steps_still_renders_an_empty_step(self, workload) -> None:
        """The whole byte-identical guarantee, at the field that could have broken it."""
        request = workload.pipeline("B2").render(
            "simulated", "claude-opus-5", {"grounding": "g", "question": "q", "timestamp": "t"}
        )
        assert request.step == ""
        assert "step" not in request.canonical()

    def test_a_graph_step_changes_the_cassette_key_and_an_empty_one_does_not(self) -> None:
        """Why the field exists: a retry re-asking what it just asked is a second call.

        A content-addressed cassette cannot hold two identical requests, and without this
        discriminator a retry would be served the draft's own answer from the store.
        """
        base = LLMRequest(provider="simulated", model="m", max_tokens=10)
        assert base.model_copy(update={"step": ""}).cassette_key() == base.cassette_key()
        assert base.model_copy(update={"step": "revise"}).cassette_key() != base.cassette_key()

    def test_every_committed_cassette_of_a_single_call_workload_still_replays(self) -> None:
        """Replayed against the real fixtures, which is where a rekey would actually show."""
        spec = load_workload(repo_root() / "data/incident-triage/workload.yaml")
        items = list(load_bundle(spec).test)[:15]
        root = fixtures_dir() / "incident-triage" / "cassettes"
        if not root.exists():
            pytest.skip("fixtures/incident-triage/ has not been built")
        registry = load_registry()
        runner = Runner(
            spec,
            registry,
            registry.snapshot(list(registry.roles.values()), date(2026, 9, 11)),
            ReplayAdapter(CassetteStore(root), name="simulated"),
            provider="simulated",
            grounding=load_bundle(spec).grounding,
            origin="simulated",
        )
        result = asyncio.run(
            runner.run_pipeline("B2", items, model_id=registry.roles["frontier"], split="test")
        )
        assert all(not task.failed for task in result.tasks)
        assert all(call.step == "generate" for task in result.tasks for call in task.calls)


class TestTheLedger:
    def test_the_step_that_made_a_call_reaches_the_ledger(
        self, workload, registry, snapshot, bundle, tmp_path
    ) -> None:
        items = list(bundle.test)[:6]
        result = run(workload, registry, snapshot, bundle, "B0", items, tmp_path)
        engine = make_engine(Path(tmp_path) / "ledger.db")
        persist(engine, workload, result, items, snapshot)
        with session_scope(engine) as session:
            steps = {row.step for row in session.scalars(select_calls()).all()}
            assert {"draft", "check"} <= steps
            tools = session.scalars(select_tools()).all()
            assert len(tools) == len(items)
            assert all(row.tool == "grounding-lookup-v1" for row in tools)
            assert all(row.result_chars > 0 for row in tools)


def select_calls():
    from sqlalchemy import select

    return select(Call).where(Call.prewarm.is_(False))


def select_tools():
    from sqlalchemy import select

    return select(ToolCall)


def _pipeline(*steps: StepSpec) -> PipelineSpec:
    return PipelineSpec(
        id="P",
        name="n",
        description="d",
        model_role="frontier",
        max_tokens=50,
        steps=list(steps),
    )


def _bare_runner() -> Runner:
    """A runner with no adapter, for the rendering checks that never make a call."""
    spec = load_workload(repo_root() / WORKLOAD)
    registry = load_registry()
    return Runner(
        spec,
        registry,
        registry.snapshot(list(registry.roles.values()), date(2026, 9, 11)),
        ReplayAdapter(CassetteStore(fixtures_dir() / "catalogue-agent" / "cassettes")),
        provider="simulated",
        grounding="",
        origin="simulated",
    )


__all__ = ["CassetteMiss"]
