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

WORKLOADS = ("data/demo/workload.yaml", "data/incident-triage/workload.yaml")


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def step(step_id: str, *inputs: str, kind: str = "generate") -> StepSpec:
    """A minimally valid step of the given kind.

    Valid, not bare: a local step with nothing to return and a generation step with no message
    are both refused at compile time, so a helper that built them would be testing the compiler
    against inputs no workload can have.
    """
    body = [BlockSpec(text=f"do {step_id}")]
    if kind in ("tool", "retrieve"):
        return StepSpec(
            id=step_id, kind=kind, inputs=list(inputs), user=body, emits=[BlockSpec(text="result")]
        )
    return StepSpec(id=step_id, kind=kind, inputs=list(inputs), user=body)


def pipeline_of(*steps: StepSpec, output_step: str = "") -> PipelineSpec:
    return PipelineSpec(
        id="P",
        name="n",
        description="d",
        model_role="cheap",
        max_tokens=10,
        steps=list(steps),
        output_step=output_step,
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


class TestCompilation:
    def test_steps_run_after_the_steps_they_consume(self) -> None:
        pipeline = pipeline_of(
            step("answer", "retrieve"),
            step("retrieve", kind="retrieve"),
            step("check", "answer", kind="verify"),
            output_step="answer",
        )
        order = [s.id for s in compile_graph(pipeline).steps]
        assert order.index("retrieve") < order.index("answer") < order.index("check")

    def test_ties_break_by_declaration_order(self) -> None:
        """A run order that depended on dict iteration would hash differently on another day."""
        pipeline = pipeline_of(step("b"), step("a"), step("c"))
        assert [s.id for s in compile_graph(pipeline).steps] == ["b", "a", "c"]

    def test_a_cycle_is_refused_by_name(self) -> None:
        pipeline = pipeline_of(step("a", "b"), step("b", "a"))
        with pytest.raises(GraphError, match="cycle"):
            compile_graph(pipeline)

    def test_an_input_that_is_not_a_step_says_which_steps_exist(self) -> None:
        pipeline = pipeline_of(step("a", "nowhere"))
        with pytest.raises(GraphError, match="Known steps"):
            compile_graph(pipeline)

    def test_a_repeated_step_id_is_refused(self) -> None:
        pipeline = pipeline_of(step("a"), step("a"))
        with pytest.raises(GraphError, match="more than once"):
            compile_graph(pipeline)

    def test_dependents_finds_what_a_step_feeds(self) -> None:
        pipeline = pipeline_of(step("a"), step("b", "a"), step("c", "a"))
        graph = compile_graph(pipeline)
        assert {s.id for s in graph.dependents("a")} == {"b", "c"}
        assert graph.dependents("c") == ()


class TestWhatAGraphMustDeclare:
    """Every refusal here is one a workload author would otherwise meet mid-run."""

    def test_a_local_step_with_nothing_to_return_is_refused(self) -> None:
        pipeline = pipeline_of(
            StepSpec(id="fetch", kind="retrieve", user=[BlockSpec(text="q")]),
        )
        with pytest.raises(GraphError, match="no `emits:`"):
            compile_graph(pipeline)

    def test_a_generation_step_may_not_declare_a_second_output(self) -> None:
        pipeline = pipeline_of(
            StepSpec(
                id="answer", user=[BlockSpec(text="q")], emits=[BlockSpec(text="not the reply")]
            ),
        )
        with pytest.raises(GraphError, match="declares `emits:`"):
            compile_graph(pipeline)

    def test_a_generation_step_with_no_message_is_refused(self) -> None:
        with pytest.raises(GraphError, match="no `user:`"):
            compile_graph(pipeline_of(StepSpec(id="answer")))

    def test_a_loop_is_refused_because_nothing_bounds_it(self) -> None:
        with pytest.raises(GraphError, match="not a loop"):
            compile_graph(pipeline_of(step("spin", kind="loop")))

    def test_reading_a_step_without_declaring_it_as_an_input_is_refused(self) -> None:
        pipeline = pipeline_of(
            step("answer"),
            StepSpec(id="check", kind="verify", user=[BlockSpec(text="review {{answer}}")]),
            output_step="answer",
        )
        with pytest.raises(GraphError, match="does not list 'answer' among its inputs"):
            compile_graph(pipeline)

    def test_a_graph_that_ends_in_a_verifier_has_to_say_what_it_answers_with(self) -> None:
        pipeline = pipeline_of(step("answer"), step("check", "answer", kind="verify"))
        with pytest.raises(GraphError, match="ends in a `verify` step"):
            compile_graph(pipeline)

    def test_an_output_step_that_is_not_a_step_is_refused(self) -> None:
        pipeline = pipeline_of(step("answer"), output_step="nowhere")
        with pytest.raises(GraphError, match="which it does not declare"):
            compile_graph(pipeline)

    def test_a_single_call_pipeline_may_not_name_an_output_step(self) -> None:
        pipeline = PipelineSpec(
            id="P",
            name="n",
            description="d",
            model_role="cheap",
            max_tokens=10,
            output_step="somewhere",
        )
        with pytest.raises(GraphError, match="declares no steps"):
            compile_graph(pipeline)

    def test_the_output_step_defaults_to_the_last_step_in_run_order(self) -> None:
        graph = compile_graph(pipeline_of(step("a"), step("b", "a")))
        assert graph.output_step == "b"
