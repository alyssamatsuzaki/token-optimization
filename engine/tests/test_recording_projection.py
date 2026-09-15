"""What a recording is expected to cost, priced before the first call (M13, UPGRADE_V4.md M13.2).

Everything here runs against the deterministic simulated provider: no socket is opened and
nothing is spent. What is being checked is the arithmetic an operator would be shown, and the
gates that stand between `tokop record` and a charge.

The fixture set this repository ships was built by that same simulated provider at real prices,
so its recorded costs are the natural thing to check the projection against: the band has to
contain them, step by step. A projection that did not would be a number nobody could use to
choose a budget.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from tokop.core.budget import SpendGuard
from tokop.core.registry import load_registry
from tokop.core.stats import PowerEstimate, StatsError
from tokop.paths import fixtures_dir, repo_root
from tokop.recorder import Recorder, project_from_pilot
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


@pytest.fixture(scope="module")
def recorded_costs() -> dict[tuple[str, str, str], Decimal]:
    """What each step of the committed fixture set actually cost."""
    manifest = json.loads((fixtures_dir() / "test" / "manifest.json").read_text())
    return {
        (run["pipeline"], run["split"], run["tier"]): Decimal(run["cost_usd"])
        for run in manifest["runs"]
    }


def make_recorder(workload, registry, bundle, tmp_path) -> Recorder:
    return Recorder(
        workload,
        registry,
        bundle,
        registry.snapshot(list(registry.roles.values()), date(2026, 9, 11)),
        tmp_path / "cassettes",
        provider="simulated",
        origin="simulated",
    )


class TestThePlanIsData:
    def test_it_lists_every_step_the_run_will_make(self, workload, registry, bundle, tmp_path):
        """One list drives both the projection and the run, so they cannot drift apart."""
        plan = make_recorder(workload, registry, bundle, tmp_path).plan()
        assert [(s.pipeline, s.split, s.tier) for s in plan] == [
            ("B2", "calibration", "cheap"),
            ("B2", "calibration", "mid"),
            ("B2", "calibration", "frontier"),
            ("B2", "test", "cheap"),
            ("B2", "test", "mid"),
            ("B2", "test", "frontier"),
            ("B0", "test", "frontier"),
            ("B1", "test", "frontier"),
            ("B2c", "test", "frontier"),
        ]

    def test_cheapest_first(self, workload, registry, bundle, tmp_path):
        """SPEC.md section 6: a generator bug should be found after a few dollars, not twenty."""
        plan = make_recorder(workload, registry, bundle, tmp_path).plan()
        calibration = [s for s in plan if s.split == "calibration"]
        assert [s.tier for s in calibration] == ["cheap", "mid", "frontier"]
        assert plan.index(calibration[-1]) < min(
            index for index, s in enumerate(plan) if s.split == "test"
        )


class TestTheProjectionBracketsTheRealCost:
    """The acceptance check for the projection: every recorded step lands inside its band."""

    async def test_every_step_of_the_committed_recording_falls_in_its_band(
        self, workload, registry, bundle, tmp_path, recorded_costs
    ):
        projection = await make_recorder(workload, registry, bundle, tmp_path).project(None)
        assert projection.steps
        for step in projection.steps:
            actual = recorded_costs[(step.pipeline, step.split, step.tier)]
            assert step.floor_usd <= actual <= step.ceiling_usd, (
                f"{step.pipeline}/{step.split}/{step.tier}: ${actual} outside "
                f"${step.floor_usd}-${step.ceiling_usd}"
            )

    async def test_the_total_brackets_the_total(
        self, workload, registry, bundle, tmp_path, recorded_costs
    ):
        projection = await make_recorder(workload, registry, bundle, tmp_path).project(None)
        assert projection.floor_usd <= sum(recorded_costs.values()) <= projection.ceiling_usd

    async def test_the_cached_prefix_is_priced_as_a_prefix_and_not_as_fresh_input(
        self, workload, registry, bundle, tmp_path
    ):
        """The tokenizer-generation trap, pinned.

        An exact count from a Claude 4.7-era model and a base-counter count of the same text
        differ by about 30%. Subtracting one from the other books that gap as text that changes
        every call and prices thousands of cached tokens at the full input rate. The committed
        recording says what the cached prefix really is, to the token.
        """
        manifest = json.loads((fixtures_dir() / "test" / "manifest.json").read_text())
        written = {(run["pipeline"], run["split"], run["tier"]): run for run in manifest["runs"]}
        projection = await make_recorder(workload, registry, bundle, tmp_path).project(None)
        b1 = next(s for s in projection.steps if s.pipeline == "B1")
        assert written[("B1", "test", "frontier")]  # the step exists in the recording
        # B1's whole point is a cacheable prefix; B0's is not having one.
        b0 = next(s for s in projection.steps if s.pipeline == "B0")
        assert b0.prefix_tokens == 0
        assert b1.prefix_tokens > 8_000
        assert b1.volatile_tokens < 200, "the tokenizer gap has been booked as volatile text"

    async def test_it_says_which_counter_produced_the_numbers(
        self, workload, registry, bundle, tmp_path
    ):
        projection = await make_recorder(workload, registry, bundle, tmp_path).project(None)
        assert projection.counting_calls == len(projection.steps)
        assert projection.exact_input
        assert "counted exactly by the provider" in "\n".join(projection.lines())


class TestTheCapDecision:
    async def test_a_cap_under_the_floor_refuses(self, workload, registry, bundle, tmp_path):
        projection = await make_recorder(workload, registry, bundle, tmp_path).project(
            Decimal("1.00")
        )
        assert projection.exceeds_cap
        assert "cannot finish" in "\n".join(projection.lines())

    async def test_a_cap_inside_the_band_warns_and_does_not_refuse(
        self, workload, registry, bundle, tmp_path
    ):
        """A cap between floor and ceiling is a real possibility, not an error.

        The recording may stop partway, and that is safe: it stops on a refused call rather
        than mid-charge, and every call it paid for is a cassette a later run replays.
        """
        projection = await make_recorder(workload, registry, bundle, tmp_path).project(None)
        midpoint = (projection.floor_usd + projection.ceiling_usd) / 2
        inside = await make_recorder(workload, registry, bundle, tmp_path).project(midpoint)
        assert not inside.exceeds_cap
        assert inside.may_stop_early
        assert "may stop partway" in "\n".join(inside.lines())


class TestThePilot:
    async def test_it_measures_output_length_and_leaves_its_calls_behind(
        self, workload, registry, bundle, tmp_path
    ):
        """SPEC.md section 6 step 1. The pilot's spend is not a separate purchase."""
        recorder = make_recorder(workload, registry, bundle, tmp_path)
        outcome = await recorder.pilot(tasks=2)
        assert outcome.tasks > 0
        assert outcome.cost > 0
        assert any(tokens > 0 for tokens in outcome.output_tokens)
        assert outcome.remaining_tasks == outcome.total_tasks - outcome.tasks
        assert len(recorder.store) > 0, "the pilot's calls are cassettes the run can replay"

    async def test_the_refined_projection_uses_measured_output(
        self, workload, registry, bundle, tmp_path
    ):
        recorder = make_recorder(workload, registry, bundle, tmp_path)
        outcome = await recorder.pilot(tasks=2)
        refined = project_from_pilot(
            outcome.cost, outcome.tasks, outcome.output_tokens, outcome.total_tasks, None
        )
        assert refined.projected_usd > outcome.cost
        assert refined.p95_output_tokens > 0
        assert refined.within_budget, "no budget means nothing to exceed"


class TestPower:
    def test_it_reads_the_split_the_verdict_would_need(self):
        estimate = PowerEstimate.from_counts(baseline_only=6, candidate_only=7, n=200, margin=0.03)
        assert estimate.required_n == 204
        assert not estimate.is_sufficient
        assert estimate.shortfall == 4
        assert "4 more" in estimate.display

    def test_a_candidate_already_past_the_margin_cannot_be_settled_by_more_tasks(self):
        estimate = PowerEstimate.from_counts(baseline_only=40, candidate_only=2, n=200, margin=0.03)
        assert estimate.required_n is None
        assert estimate.shortfall == 0
        assert "No split size settles this" in estimate.display

    def test_a_tighter_alpha_needs_more_tasks(self):
        loose = PowerEstimate.from_counts(baseline_only=6, candidate_only=7, n=200, alpha=0.05)
        tight = PowerEstimate.from_counts(baseline_only=6, candidate_only=7, n=200, alpha=0.01)
        assert tight.required_n is not None and loose.required_n is not None
        assert tight.required_n > loose.required_n

    def test_it_refuses_counts_that_cannot_have_come_from_the_split(self):
        with pytest.raises(StatsError):
            PowerEstimate.from_counts(baseline_only=150, candidate_only=150, n=200)
        with pytest.raises(StatsError):
            PowerEstimate.from_counts(baseline_only=1, candidate_only=1, n=0)


class TestTheOverrideIsRecorded:
    """`--underpowered` prints that it is stored. It has to be stored (UPGRADE_V4.md M13.1)."""

    def test_the_manifest_carries_the_flag(self, workload, registry, bundle, tmp_path):
        from tokop.recorder import RecordingReport

        assert RecordingReport().manifest()["underpowered"] is False
        overridden = RecordingReport(underpowered=True)
        assert overridden.manifest()["underpowered"] is True

    def test_the_report_says_whether_a_recording_overrode_the_refusal(self):
        from tokop.optimize.report import build_report

        provenance = build_report()["provenance"]
        assert provenance["recorded_underpowered"] is False

    def test_the_report_names_the_split_that_would_settle_the_verdict(self):
        """The acceptance criterion: an inconclusive result says the exact n, not just how far.

        Whether the answer is four more tasks or four thousand is the difference between
        recording again tomorrow and abandoning the comparison, and a verdict that only says
        "inconclusive" leaves the reader to guess which.
        """
        from tokop.optimize.report import build_report

        proof = build_report()["proof"]
        power = proof["power"]
        assert power["required_n"] == power["observed_n"] + power["shortfall"]
        assert power["shortfall"] == proof["verdict"]["additional_tasks_needed"]
        assert str(power["required_n"]) in power["display"]


class TestTheGuardIsWiredIntoARecording:
    def test_a_recording_runs_under_the_cap_it_was_given(
        self, workload, registry, bundle, tmp_path
    ):
        guard = SpendGuard(run_cap=Decimal("5.00"))
        recorder = Recorder(
            workload,
            registry,
            bundle,
            registry.snapshot(list(registry.roles.values()), date(2026, 9, 11)),
            tmp_path / "cassettes",
            provider="simulated",
            origin="simulated",
            guard=guard,
        )
        assert recorder.sink.guard is guard
        assert recorder.sink.snapshot is recorder.snapshot
