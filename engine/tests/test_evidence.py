"""The evidence grade, and the chain that produces it (UPGRADE_V4.md M14.2).

The grade is the load-bearing word on the first screen, so what is tested here is mostly that it
cannot be talked upwards: no input softens and no average hides a blocking link. The demo's
conclusive result reads `simulated`, because its traces are invented rather than recorded.
"""

from __future__ import annotations

import copy

import pytest

from tokop.optimize.evidence import assess
from tokop.optimize.report import ReportPayload, build_report


@pytest.fixture(scope="module")
def report() -> ReportPayload:
    return build_report()


def edited(report: ReportPayload, **paths: object) -> ReportPayload:
    """A copy of the payload with dotted paths overwritten, for the cases fixtures cannot make."""
    data = copy.deepcopy(report.data)
    for dotted, value in paths.items():
        keys = dotted.split("__")
        target = data
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    return ReportPayload(data)


class TestTheDemoGradesHonestly:
    def test_a_conclusive_simulated_verdict_is_labelled_simulated(self, report) -> None:
        """The result holds for the fixture traces without claiming production evidence."""
        evidence = assess(report)
        assert evidence.grade == "simulated"
        assert report["proof"]["verdict"]["label"] == "non_inferior"
        assert report["proof"]["power"]["sufficient"] is True
        assert evidence.as_dict()["blocking"] == []

    def test_every_input_names_where_its_number_came_from(self, report) -> None:
        for link in assess(report).inputs:
            assert link.source, f"{link.name} does not say where its number came from"
            assert link.value

    def test_a_link_that_is_not_ok_says_what_would_change_it(self, report) -> None:
        """A reader should never have to guess whether a weak link can be fixed."""
        for link in assess(report).inputs:
            if link.standing != "ok":
                assert link.what_would_change_it, f"{link.name} is {link.standing} with no remedy"

    def test_the_chain_covers_what_the_upgrade_asked_for(self, report) -> None:
        names = " ".join(link.name for link in assess(report).inputs).lower()
        for expected in ("real traffic", "split", "disagree", "grader", "price"):
            assert expected in names


class TestTheGradeIsTheLowestRung:
    def test_a_clean_simulated_run_still_reads_simulated(self, report) -> None:
        """Fix the verdict and the split, and the simulated traces still hold it down."""
        fixed = edited(
            report,
            proof__verdict={**report["proof"]["verdict"], "label": "non_inferior"},
            proof__power={**report["proof"]["power"], "sufficient": True},
        )
        assert assess(fixed).grade == "simulated"

    def test_a_recorded_run_over_real_traffic_reads_recording(self, report) -> None:
        provenance = dict(report["dataset_provenance"])
        provenance["real_traffic_items"] = provenance["n"]
        recorded = edited(
            report,
            proof__verdict={**report["proof"]["verdict"], "label": "non_inferior"},
            proof__power={**report["proof"]["power"], "sufficient": True},
            dataset_provenance=provenance,
            provenance={
                **report["provenance"],
                "is_test_data": False,
                "origin": "live",
                "base_token_counter": "o200k_base",
            },
        )
        assert assess(recorded).grade == "recording"

    def test_one_unverified_price_blocks_a_recording(self, report) -> None:
        """Cost per successful task is the headline, so an unverified price unverifies it."""
        provenance = dict(report["dataset_provenance"])
        provenance["real_traffic_items"] = provenance["n"]
        broken = edited(
            report,
            proof__verdict={**report["proof"]["verdict"], "label": "non_inferior"},
            proof__power={**report["proof"]["power"], "sufficient": True},
            dataset_provenance=provenance,
            provenance={
                **report["provenance"],
                "is_test_data": False,
                "origin": "live",
                "base_token_counter": "o200k_base",
                "prices_verified": False,
                "unverified_models": ["claude-opus-5"],
            },
        )
        evidence = assess(broken)
        assert evidence.grade == "insufficient"
        assert "Prices" in evidence.as_dict()["blocking"]

    def test_a_worse_verdict_never_grades_above_insufficient(self, report) -> None:
        worse = edited(report, proof__verdict={**report["proof"]["verdict"], "label": "worse"})
        assert assess(worse).grade == "insufficient"


class TestTheSummaryBlock:
    """Every number the first screen shows, computed here rather than in a component."""

    def test_it_carries_the_six_figures_the_screen_needs(self, report) -> None:
        summary = report["summary"]
        assert summary["current"]["cost_per_successful_task_usd"]
        assert summary["recommended"]["cost_per_successful_task_usd"]
        assert summary["saving"]["fraction"] > 0
        assert summary["quality"]["allowed_points"] == pytest.approx(3.0)
        assert summary["quality"]["n"] == 204
        assert summary["verdict"]["display"]

    def test_it_agrees_with_the_proof_it_summarises(self, report) -> None:
        """Two places showing the same figure is how two figures start to disagree."""
        summary, proof = report["summary"], report["proof"]
        assert summary["current"]["cost_per_successful_task_usd"] == str(
            proof["baseline"]["cost_per_successful_task"]["point"]
        )
        assert summary["saving"]["fraction"] == proof["cost_reduction"]
        assert summary["quality"]["delta_points"] == pytest.approx(
            proof["delta_accuracy"]["point"] * 100
        )
        assert summary["verdict"]["label"] == proof["verdict"]["label"]

    def test_simulated_data_is_marked_inside_the_summary(self, report) -> None:
        """UPGRADE_V4.md section 3.3: the label goes everywhere, the new block included."""
        assert report["summary"]["is_test_data"] is True
        assert report["summary"]["provenance_mark"] == "~"
