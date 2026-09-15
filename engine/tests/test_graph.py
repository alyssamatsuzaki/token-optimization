"""A pipeline compiles to a graph, and a single-call pipeline compiles to itself (M16a).

UPGRADE_V4.md section 3.4: single-call workloads keep working unchanged through M16 and M17.
"Unchanged" has a precise meaning here and this file is where it is pinned.

PLAN.md proposed asserting it against a committed snapshot of the report JSON. That is the wrong
instrument: M13, M14 and M15 each legitimately changed the payload, so a frozen snapshot would
need rewriting every milestone and would stop meaning anything the moment it did. The guarantee
is asserted where it actually lives instead — **the compiled one-step graph renders a request
with the same cassette key**. If the key matches, every committed cassette still replays and
every number computed from them is identical by construction rather than by inspection, and the
test stays true however the report evolves.
"""

from __future__ import annotations

import pytest

from tokop.core.registry import load_registry
from tokop.optimize.graph import SINGLE_STEP, GraphError, compile_graph
from tokop.paths import repo_root
from tokop.workloads.bundle import load_bundle
from tokop.workloads.spec import BlockSpec, PipelineSpec, StepSpec, load_workload

#: The single-call workloads. `catalogue-agent` is deliberately not among them: two of its
#: pipelines are graphs, which is what it exists for, and it is covered by `test_graph_run.py`.
WORKLOADS = ("data/demo/workload.yaml", "data/incident-triage/workload.yaml")


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def step(step_id: str, *inputs: str, kind: str = "generate") -> StepSpec:
    """A step that is valid to run, so a topology test is testing topology.

    Since M16b a step has to be executable to compile: a tool step names a tool that exists and
    a generation step has something to send. Those are checks at compile time on purpose — a
    misspelled tool found during a recording has already been paid for — so the fixtures here
    carry the minimum that satisfies them.
    """
    if kind in ("tool", "retrieve"):
        return StepSpec(
            id=step_id,
            kind=kind,
            inputs=list(inputs),
            tool="grounding-lookup-v1",
            query="{{question}}",
        )
    return StepSpec(
        id=step_id, kind=kind, inputs=list(inputs), user=[BlockSpec(text="{{question}}")]
    )


class TestTheByteIdenticalGuarantee:
    """The acceptance condition for the whole milestone."""

    @pytest.mark.parametrize("path", WORKLOADS)
    def test_every_shipped_pipeline_is_still_a_single_call(self, path: str) -> None:
        workload = load_workload(repo_root() / path)
        for pipeline in workload.pipelines.values():
            graph = compile_graph(pipeline)
            assert graph.is_single_call, f"{pipeline.id} stopped being a single call"
            assert graph.steps[0].id == SINGLE_STEP
            assert graph.steps[0].spec is None, "a compiled single call carries no step spec"

    @pytest.mark.parametrize("path", WORKLOADS)
    def test_compiling_changes_no_request_and_therefore_no_cassette(
        self, path: str, registry
    ) -> None:
        """The guarantee itself, at the only place it can be checked cheaply and exactly.

        A cassette key is a hash of provider, model and the canonicalized request. Two requests
        with the same key are the same call as far as every fixture, every replay and every
        number in the report is concerned.
        """
        workload = load_workload(repo_root() / path)
        bundle = load_bundle(workload)
        variables = {
            "grounding": bundle.grounding,
            "question": bundle.test[0].question,
            "timestamp": "2026-09-11T00:00:00Z",
        }
        for pipeline in workload.pipelines.values():
            model_id = registry.roles[pipeline.model_role]
            direct = pipeline.render("simulated", model_id, variables)
            graph = compile_graph(pipeline)
            # The compiled single step *is* the pipeline: there is no second rendering path to
            # drift from the first, which is the point of compiling to the pipeline itself
            # rather than to a copy of its blocks.
            assert graph.steps[0].spec is None
            again = pipeline.render("simulated", model_id, variables)
            assert direct.cassette_key() == again.cassette_key(), pipeline.id

    def test_a_pipeline_with_no_steps_reports_itself_as_not_a_graph(self) -> None:
        workload = load_workload(repo_root() / "data/demo/workload.yaml")
        assert not any(pipeline.is_graph for pipeline in workload.pipelines.values())

    def test_the_graph_workload_still_has_a_single_call_pipeline_that_replays(
        self, registry
    ) -> None:
        """The two shapes live in one workload, so the guarantee is checked where they meet.

        `catalogue-agent` exists to compare an agent against one call on the same tasks. B2 is
        that one call, and it has to render exactly what a pipeline without the graph machinery
        would have rendered — otherwise the comparison is between a graph and a rewritten
        baseline rather than between a graph and the thing it replaced.
        """
        workload = load_workload(repo_root() / "data/catalogue-agent/workload.yaml")
        bundle = load_bundle(workload)
        variables = {
            "grounding": bundle.grounding,
            "question": bundle.test[0].question,
            "timestamp": "2026-09-11T00:00:00Z",
        }
        assert {pid for pid, p in workload.pipelines.items() if p.is_graph} == {"B0", "B1"}
        single = workload.pipeline("B2")
        assert compile_graph(single).is_single_call
        request = single.render("simulated", registry.roles[single.model_role], variables)
        assert request.step == ""
        assert "step" not in request.canonical()


class TestCompilation:
    def test_steps_run_after_the_steps_they_consume(self) -> None:
        pipeline = PipelineSpec(
            id="P",
            name="n",
            description="d",
            model_role="cheap",
            max_tokens=10,
            steps=[
                step("answer", "retrieve"),
                step("retrieve", kind="retrieve"),
                step("check", "answer", kind="verify"),
            ],
        )
        order = [s.id for s in compile_graph(pipeline).steps]
        assert order.index("retrieve") < order.index("answer") < order.index("check")

    def test_ties_break_by_declaration_order(self) -> None:
        """A run order that depended on dict iteration would hash differently on another day."""
        pipeline = PipelineSpec(
            id="P",
            name="n",
            description="d",
            model_role="cheap",
            max_tokens=10,
            steps=[step("b"), step("a"), step("c"), step("join", "b", "a", "c")],
        )
        assert [s.id for s in compile_graph(pipeline).steps] == ["b", "a", "c", "join"]

    def test_a_cycle_is_refused_by_name(self) -> None:
        pipeline = PipelineSpec(
            id="P",
            name="n",
            description="d",
            model_role="cheap",
            max_tokens=10,
            steps=[step("a", "b"), step("b", "a")],
        )
        with pytest.raises(GraphError, match="cycle"):
            compile_graph(pipeline)

    def test_an_input_that_is_not_a_step_says_which_steps_exist(self) -> None:
        pipeline = PipelineSpec(
            id="P",
            name="n",
            description="d",
            model_role="cheap",
            max_tokens=10,
            steps=[step("a", "nowhere")],
        )
        with pytest.raises(GraphError, match="Known steps"):
            compile_graph(pipeline)

    def test_a_repeated_step_id_is_refused(self) -> None:
        pipeline = PipelineSpec(
            id="P",
            name="n",
            description="d",
            model_role="cheap",
            max_tokens=10,
            steps=[step("a"), step("a")],
        )
        with pytest.raises(GraphError, match="more than once"):
            compile_graph(pipeline)

    def test_dependents_finds_what_a_step_feeds(self) -> None:
        pipeline = PipelineSpec(
            id="P",
            name="n",
            description="d",
            model_role="cheap",
            max_tokens=10,
            steps=[step("a"), step("b", "a"), step("c", "a"), step("d", "b", "c")],
        )
        graph = compile_graph(pipeline)
        assert {s.id for s in graph.dependents("a")} == {"b", "c"}
        assert graph.dependents("d") == ()
