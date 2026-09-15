"""RECORD_BUDGET_USD is a cap, not a label (M13, UPGRADE_V4.md M13.2).

Until M13 nothing in the engine called ``SpendGuard.preflight``. The recorder consulted the
guard once per *step* — one or two hundred tasks — with a projection of zero, under a comment
saying the live adapter checked each call; no adapter did. So ``B0`` on the test split was a
single uninterruptible purchase, and a recording could pass its cap by the width of a whole
step before anything noticed (DECISIONS.md D42).

These tests are the reason to believe the cap now holds. They run against the deterministic
simulated provider, which opens no socket and spends nothing; what they check is the
arithmetic and the control flow that a live run would take.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tokop.adapters.recording import RecordingAdapter
from tokop.core.budget import BudgetExceeded, SpendGuard
from tokop.core.registry import load_registry
from tokop.paths import repo_root
from tokop.recorder import GuardSink, Recorder
from tokop.workloads.demo.dataset import build as build_dataset
from tokop.workloads.spec import load_workload


@pytest.fixture(scope="module")
def registry():
    return load_registry()


@pytest.fixture(scope="module")
def workload():
    return load_workload(repo_root() / "data/demo/workload.yaml")


@pytest.fixture(scope="module")
def bundle():
    return build_dataset()


def make_recorder(workload, registry, bundle, tmp_path, guard: SpendGuard) -> Recorder:
    return Recorder(
        workload,
        registry,
        bundle,
        registry.snapshot(list(registry.roles.values()), date(2026, 9, 11)),
        tmp_path / "cassettes",
        provider="simulated",
        origin="simulated",
        guard=guard,
        concurrency=4,
    )


class TestTheCapStopsARecording:
    """The acceptance check: a cap reached mid-step stops the step."""

    async def test_a_cap_reached_partway_stops_the_recording(
        self, workload, registry, bundle, tmp_path
    ) -> None:
        items = list(bundle.calibration)[:24]
        guard = SpendGuard(run_cap=Decimal("0.01"))
        recorder = make_recorder(workload, registry, bundle, tmp_path, guard)

        with pytest.raises(BudgetExceeded) as caught:
            await recorder._step("B2", "calibration", "cheap", items)

        assert caught.value.cap_name == "run"
        # Stopped partway, which is the whole point: some calls were paid for and the rest
        # never happened. A cap that only reported at the end of the step would show all 24.
        assert 0 < len(recorder.store) < len(items)

    async def test_a_refusal_is_not_recorded_as_a_split_of_failed_tasks(
        self, workload, registry, bundle, tmp_path
    ) -> None:
        """A budget stop must not look like a bad model.

        ``run_pipeline`` gathers with ``return_exceptions=True`` so that a provider failure
        becomes an unsuccessful task and stays visible (non-negotiable 8). Absorbing a budget
        refusal the same way would spend the entire cap and then hand the report an accuracy
        figure that describes the budget rather than the model.
        """
        items = list(bundle.calibration)[:24]
        guard = SpendGuard(run_cap=Decimal("0.01"))
        recorder = make_recorder(workload, registry, bundle, tmp_path, guard)
        with pytest.raises(BudgetExceeded):
            await recorder._step("B2", "calibration", "cheap", items)

    async def test_the_guard_is_charged_per_call_and_not_per_step(
        self, workload, registry, bundle, tmp_path
    ) -> None:
        items = list(bundle.calibration)[:8]
        guard = SpendGuard(run_cap=Decimal("10.00"))
        recorder = make_recorder(workload, registry, bundle, tmp_path, guard)
        step = await recorder._step("B2", "calibration", "cheap", items)

        assert guard.run_spend > 0
        # Every call the step made was charged exactly once, so the guard's total and the
        # step's own accounting agree. Before M13 the guard saw one lump after the fact.
        assert guard.run_spend == step.cost


class TestReplayedCallsAreFree:
    async def test_a_resumed_recording_replays_past_the_cap_without_paying_again(
        self, workload, registry, bundle, tmp_path
    ) -> None:
        """The reason the hooks fire on a cassette miss and nowhere else.

        A recording that stopped at its cap has to be able to replay everything it already paid
        for — otherwise resuming is impossible exactly when it matters. A cassette hit costs
        nothing, so it is neither refused near the cap nor billed to it.
        """
        items = list(bundle.calibration)[:8]
        first = make_recorder(workload, registry, bundle, tmp_path, SpendGuard(Decimal("10.00")))
        await first._step("B2", "calibration", "cheap", items)
        recorded = len(first.store)

        spent = SpendGuard(run_cap=Decimal(0))
        second = make_recorder(workload, registry, bundle, tmp_path, spent)
        step = await second._step("B2", "calibration", "cheap", items)

        assert len(second.store) == recorded, "a replay should record nothing new"
        assert spent.run_spend == Decimal(0), "a replayed call is not a purchase"
        assert step.accuracy > 0, "the replayed step still produced its answers"


class TestResumingAfterTheCap:
    """The scenario M13b actually has to survive: stopped at the cap, resumed, finished.

    SPEC.md section 7.1 and the M2 acceptance test already show that an interrupted recording
    sends no request twice. What that test cannot see is the money, because nothing charged the
    guard per call until M13. This one watches the spend: the second run pays for the calls the
    first never reached and for nothing it already bought.
    """

    async def test_a_resumed_recording_pays_only_for_what_is_left(
        self, workload, registry, bundle, tmp_path
    ) -> None:
        items = list(bundle.calibration)[:24]

        stopped = SpendGuard(run_cap=Decimal("0.02"))
        first = make_recorder(workload, registry, bundle, tmp_path, stopped)
        with pytest.raises(BudgetExceeded):
            await first._step("B2", "calibration", "cheap", items)
        paid_first = stopped.run_spend
        bought = len(first.store)
        assert 0 < bought < len(items)

        resumed = SpendGuard(run_cap=Decimal("10.00"))
        second = make_recorder(workload, registry, bundle, tmp_path, resumed)
        step = await second._step("B2", "calibration", "cheap", items)

        assert len(second.store) > bought, "the resumed run recorded the rest"
        assert resumed.run_spend < step.cost, "it was billed for less than the whole step"
        # Nothing was bought twice: the two runs together paid for the step exactly once.
        assert paid_first + resumed.run_spend == step.cost


class TestEveryAdapterIsCharged:
    """A path that skips the sink is a path that spends without a cap."""

    def test_generation_and_entailment_adapters_both_carry_the_sink(
        self, workload, registry, bundle, tmp_path
    ) -> None:
        recorder = make_recorder(workload, registry, bundle, tmp_path, SpendGuard())
        for adapter in (recorder._adapter("B2"), recorder._entailment_adapter()):
            assert isinstance(adapter, RecordingAdapter)
            assert isinstance(adapter.on_spend, GuardSink)
            assert adapter.on_spend.guard is recorder.guard


class TestTheProjection:
    def test_it_bounds_the_call_above(self, workload, registry, bundle, tmp_path) -> None:
        """The guard errs towards refusing: input uncached, output at the cap.

        The real call reads most of its input from the cache and stops well short of
        ``max_tokens``, so the projection is an over-estimate by construction — which is what a
        guard needs, and why it is not the number an operator is asked to approve.
        """
        recorder = make_recorder(workload, registry, bundle, tmp_path, SpendGuard())
        item = next(iter(bundle.calibration))
        model_id = registry.roles["cheap"]
        request = recorder._runner("B2").render(workload.pipeline("B2"), item, model_id)

        projection = recorder.sink.projection(request)
        price = recorder.snapshot.price_for(model_id)
        ceiling = Decimal(request.max_tokens) * price.output / Decimal(1_000_000)

        assert projection > ceiling, "the input side is missing from the projection"
