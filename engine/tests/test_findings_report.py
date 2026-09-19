"""Workload findings and the report (SPEC.md 7.4, 10.5).

The report is the single computation the API, the UI, the e2e tests and the README all read, so
these tests are mostly about it agreeing with itself and with arithmetic done independently.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tokop.core.pricing import ModelPrice, PriceProvenance
from tokop.optimize.findings import (
    CallRow,
    RunStats,
    batch_finding,
    cheaper_tier_finding,
    rank,
    workload_findings,
)
from tokop.optimize.report import (
    METRICS_END,
    METRICS_START,
    build_report,
    method_block,
    method_doc_current,
    readme_block,
    readme_metrics_current,
)

PRICE = ModelPrice(
    model_id="claude-opus-5",
    provider="anthropic",
    input=Decimal("5"),
    output=Decimal("25"),
    cache_write_5m=Decimal("6.25"),
    cache_read=Decimal("0.50"),
    batch_discount=Decimal("0.5"),
    provenance=PriceProvenance(
        source_url="https://platform.claude.com/docs/en/about-claude/pricing",
        retrieved=date(2026, 9, 11),
        verified=True,
    ),
)

DOC = "The returns policy sets out the window for each category. " * 120


def call(
    task: str,
    *,
    uncached: int = 7000,
    read: int = 0,
    write: int = 0,
    out: int = 200,
    cost: str = "0.04",
    attempt: int = 1,
    error: str | None = None,
    prefix: str = "",
    user: str = "",
) -> CallRow:
    return CallRow(
        task_id=task,
        model="claude-opus-5",
        prompt_hash="h",
        tier="frontier",
        attempt=attempt,
        prewarm=False,
        input_uncached=uncached,
        cache_write_5m=write,
        cache_write_1h=0,
        cache_read=read,
        output_visible=out,
        output_reasoning=0,
        cost_usd=Decimal(cost),
        latency_ms=900.0,
        reused=False,
        error=error,
        system_text="You answer from the policy.",
        user_text=user or f"Question {task}\n\n{DOC}",
        static_prefix=prefix,
    )


def stats_of(calls, *, successes: int | None = None, needed: int = 40) -> RunStats:
    tasks = len({c.task_id for c in calls if not c.prewarm})
    return RunStats(
        pipeline_id="B0",
        split="test",
        model_id="claude-opus-5",
        price=PRICE,
        calls=tuple(calls),
        task_count=tasks,
        successes=tasks if successes is None else successes,
        needed_output_tokens=needed,
    )


class TestWorkloadFindings:
    def test_w01_fires_when_an_identical_block_is_never_cached(self) -> None:
        calls = [call(f"t{i}") for i in range(30)]
        found = {f.id for f in workload_findings(stats_of(calls))}
        assert "W01" in found

    def test_w01_does_not_fire_when_the_block_is_cached(self) -> None:
        calls = [
            call(f"t{i}", uncached=50, read=7000, prefix="You answer from the policy.\n\n" + DOC)
            for i in range(30)
        ]
        assert "W01" not in {f.id for f in workload_findings(stats_of(calls))}

    def test_w02_fires_when_requests_diverge_before_the_document(self) -> None:
        calls = [call(f"t{i}") for i in range(30)]
        findings = {f.id: f for f in workload_findings(stats_of(calls))}
        assert "W02" in findings
        assert "diverge" in findings["W02"].title
        assert findings["W02"].confidence == "measured"

    def test_w02_fires_when_the_declared_prefix_varies(self) -> None:
        calls = [
            call(f"t{i}", uncached=50, write=7000, prefix=f"Stamp {i}\n\n{DOC}") for i in range(30)
        ]
        findings = {f.id: f for f in workload_findings(stats_of(calls))}
        assert "W02" in findings
        assert "changes on almost every call" in findings["W02"].title

    def test_w02_fires_when_every_call_writes_and_none_reads(self) -> None:
        """The parallel fan-out with no pre-warm: same prefix, all writes, no reads."""
        prefix = "You answer from the policy.\n\n" + DOC
        calls = [call(f"t{i}", uncached=50, write=7000, prefix=prefix) for i in range(30)]
        findings = {f.id: f for f in workload_findings(stats_of(calls))}
        assert "W02" in findings
        assert "writes the cache and none reads it" in findings["W02"].title
        assert findings["W02"].transform == "prewarm_cache"

    def test_w03_fires_when_output_dwarfs_what_the_grader_reads(self) -> None:
        calls = [call(f"t{i}", out=400) for i in range(30)]
        findings = {f.id: f for f in workload_findings(stats_of(calls, needed=40))}
        assert "W03" in findings
        assert findings["W03"].confidence == "measured"
        # (400 - 40) tok x $25/Mtok x 1,000 calls / 1e6 = $9.00
        assert findings["W03"].projected_usd_per_1k == Decimal("9.000")

    def test_w03_does_not_fire_on_a_terse_pipeline(self) -> None:
        calls = [call(f"t{i}", out=45) for i in range(30)]
        assert "W03" not in {f.id for f in workload_findings(stats_of(calls, needed=40))}

    def test_w06_reports_retries_and_failures(self) -> None:
        calls = [call(f"t{i}") for i in range(28)]
        calls.append(call("t28", attempt=2, cost="0.04"))
        calls.append(call("t29", error="HTTP 529: overloaded", cost="0"))
        findings = {f.id: f for f in workload_findings(stats_of(calls))}
        assert "W06" in findings
        assert "1 retried call(s), 1 failed call(s)" in findings["W06"].evidence

    def test_w05_projects_the_batch_discount_only_when_latency_allows(self) -> None:
        stats = stats_of([call(f"t{i}") for i in range(30)])
        assert batch_finding(stats, latency_sensitive=True) is None
        finding = batch_finding(stats, latency_sensitive=False)
        assert finding is not None
        assert finding.confidence == "projected"
        assert "does not execute batches" in finding.detail

    def test_w04_projects_from_calibration_and_states_its_n(self) -> None:
        frontier = stats_of([call(f"t{i}", cost="0.04") for i in range(100)])
        cheap = stats_of([call(f"t{i}", cost="0.002") for i in range(100)])
        finding = cheaper_tier_finding(
            frontier, cheap, {f"t{i}" for i in range(80)}, {f"t{i}" for i in range(100)}
        )
        assert finding is not None
        assert "n = 100" in finding.evidence
        assert finding.confidence == "projected"
        assert "ceiling" in finding.detail

    def test_w04_returns_nothing_when_the_cheap_tier_never_agrees(self) -> None:
        frontier = stats_of([call(f"t{i}") for i in range(10)])
        cheap = stats_of([call(f"t{i}") for i in range(10)])
        assert cheaper_tier_finding(frontier, cheap, set(), {"t0"}) is None

    def test_findings_are_ranked_by_weighted_dollars(self) -> None:
        calls = [call(f"t{i}", out=400) for i in range(30)]
        ranked = rank(workload_findings(stats_of(calls)))
        weights = [f.weighted_usd for f in ranked]
        assert weights == sorted(weights, reverse=True)

    def test_an_empty_run_produces_no_findings_rather_than_crashing(self) -> None:
        assert workload_findings(stats_of([])) == []


class TestRunStats:
    def test_cost_per_successful_task_and_accuracy(self) -> None:
        stats = stats_of([call(f"t{i}", cost="0.05") for i in range(10)], successes=8)
        assert stats.accuracy == 0.8
        assert stats.total_cost == Decimal("0.50")
        assert stats.cost_per_successful_task == Decimal("0.0625")

    def test_no_successes_gives_no_cost_per_successful_task(self) -> None:
        assert stats_of([call("t0")], successes=0).cost_per_successful_task is None

    def test_prewarm_calls_are_excluded_from_the_per_call_view(self) -> None:
        warm = call("__prewarm__")
        warm = CallRow(**{**warm.__dict__, "prewarm": True})
        stats = stats_of([call("t0"), warm])
        assert len(stats.real_calls) == 1
        assert stats.total_cost == Decimal("0.08")  # both calls still cost money


@pytest.fixture(scope="module")
def report():
    return build_report()


class TestReport:
    def test_it_builds_from_the_committed_fixtures(self, report) -> None:
        assert report["workload"]["id"] == "returns-support"
        assert report["proof"]["candidate"]["n"] == 204
        assert report["provenance"]["fixture_source"] in ("demo", "test")

    def test_every_headline_number_is_present(self, report) -> None:
        headline = report.headline()
        assert set(headline) == {
            "baseline_cost_per_successful_task",
            "candidate_cost_per_successful_task",
            "delta_accuracy",
            "verdict",
            "n",
            "proof_cost_usd",
            "repayment_tasks",
        }
        assert headline["n"] == 204

    def test_it_is_deterministic(self, report) -> None:
        assert build_report().headline() == report.headline()

    def test_simulated_fixtures_are_labelled_as_such(self, report) -> None:
        provenance = report["provenance"]
        if provenance["fixture_source"] == "test":
            assert provenance["is_test_data"] is True
            assert "not a recording" in provenance["note"]

    def test_the_proof_cost_reconciles_with_the_runs(self, report) -> None:
        """Every dollar some run spent is accounted for, and to the right thing.

        The proof is charged for the generations it needed. When the matrix was recorded deeper
        so that scorers could be compared, the extra generations are the price of the
        *comparison* and are reported separately. Together they must be the whole bill: if they
        are not, some money has gone missing or been counted twice.
        """
        proof = Decimal(report["proof"]["proof_cost"]["total_usd"])
        comparison = Decimal(report["cascade"]["scorer_choice"]["search_recording_cost_usd"])
        runs = sum(Decimal(r["total_cost_usd"]) for r in report["runs"])
        assert proof + comparison == runs

    def test_a_run_reports_what_it_actually_spent(self, report) -> None:
        """A run recorded k deep costs more than its single generation per task, and says so."""
        for row in report["runs"]:
            total = Decimal(row["total_cost_usd"])
            single = Decimal(row["single_sample_cost_usd"])
            if row["samples_per_task"] > 1:
                assert total > single, f"{row['pipeline']}/{row['split']} hid its repeats"
            else:
                assert total == single

    def test_scarce_share_never_exceeds_one(self, report) -> None:
        for step in report["waterfall"]:
            assert 0.0 <= step["scarce_share"] <= 1.0

    def test_the_cascade_reports_its_operating_point_and_how_it_was_chosen(self, report) -> None:
        cascade = report["cascade"]
        assert len(cascade["thresholds"]) == 3
        assert cascade["evaluated"] > 100
        assert cascade["runtime_seconds"] >= 0
        note = report["proof"]["operating_point_note"]
        # The note is computed from the search rather than fixed, so it is checked for the
        # claims it has to keep making rather than for an exact string.
        assert "chosen on the calibration split" in note
        assert "before any test result was computed" in note
        assert cascade["scorer_choice"]["label"] in note
        assert f"{cascade['evaluated']:,} threshold settings" in note

    def test_tier_shares_sum_to_one(self, report) -> None:
        shares = [t["share_of_tasks"] for t in report["cascade"]["tiers"]]
        assert sum(shares) == pytest.approx(1.0)

    def test_every_tier_reports_a_scorer_auroc(self, report) -> None:
        for tier in report["cascade"]["tiers"]:
            assert "auroc" in tier

    def test_the_three_required_workload_findings_fire_on_b0(self, report) -> None:
        """M4 acceptance: W01, W02 and W03 fire on B0."""
        fired = {f["id"] for f in report["findings"]}
        assert {"W01", "W02", "W03"} <= fired

    def test_findings_carry_evidence_a_formula_and_a_confidence(self, report) -> None:
        for finding in report["findings"]:
            assert finding["evidence"]
            assert finding["formula"]
            assert finding["confidence"] in ("measured", "projected", "heuristic")

    def test_the_clear_score_improves_from_b0_to_b2(self, report) -> None:
        assert report["lint"]["B2"]["clear_total"] > report["lint"]["B0"]["clear_total"]

    def test_disagreements_open_onto_real_tasks(self, report) -> None:
        for disagreement in report["proof"]["disagreements"]:
            assert disagreement["question"]
            assert disagreement["gold"]
            assert disagreement["baseline_correct"] != disagreement["candidate_correct"]

    def test_the_breakdown_covers_every_question_type(self, report) -> None:
        types = {b["question_type"] for b in report["proof"]["by_type"]}
        assert types == {"lookup", "two_hop", "computation", "exception"}
        assert sum(b["n"] for b in report["proof"]["by_type"]) == 204

    def test_an_inconclusive_verdict_says_inconclusive(self, report) -> None:
        """SPEC.md non-negotiable 5: inconclusive results are displayed as inconclusive."""
        verdict = report["proof"]["verdict"]
        assert verdict["label"] in ("non_inferior", "inconclusive", "worse")
        if verdict["label"] == "inconclusive":
            assert "Inconclusive" in verdict["display"]

    def test_quality_claims_carry_an_interval_and_an_n(self, report) -> None:
        proof = report["proof"]
        for arm in ("baseline", "candidate"):
            accuracy = proof[arm]["accuracy"]
            assert accuracy["low"] <= accuracy["point"] <= accuracy["high"]
            assert accuracy["n"] == 204
            assert accuracy["method"]

    def test_split_sizes_are_stated(self, report) -> None:
        assert report["proof"]["split_sizes"] == {"calibration": 100, "test": 204}

    def test_prices_carry_provenance(self, report) -> None:
        assert report["provenance"]["price_snapshot_id"]
        assert report["provenance"]["prices_verified"] is True

    def test_the_frontier_chart_marks_exactly_one_operating_point(self, report) -> None:
        marked = [p for p in report["cascade"]["frontier_chart"] if p["is_operating_point"]]
        assert len(marked) == 1


class TestReadmeBlock:
    def test_the_block_is_generated_and_delimited(self, report) -> None:
        block = readme_block(report)
        assert block.startswith(METRICS_START)
        assert block.endswith(METRICS_END)
        assert "do not edit by hand" in block

    def test_every_number_in_the_block_comes_from_the_report(self, report) -> None:
        block = readme_block(report)
        n = report["proof"]["candidate"]["n"]
        assert f"{n} test" in block
        assert report["proof"]["verdict"]["display"] in block

    def test_the_committed_readme_is_current(self, report) -> None:
        """SPEC.md 10.5: `report --check` asserts this, so the test does too."""
        current, message = readme_metrics_current(report)
        assert current, message

    def test_simulated_data_is_marked_in_the_readme(self, report) -> None:
        block = readme_block(report)
        if report["provenance"]["is_test_data"]:
            assert "simulated" in block
            assert "DECISIONS.md D1" in block

    def test_the_headline_block_carries_the_evidence_grade(self, report) -> None:
        """UPGRADE_V4.md M14: the first thing a reader meets is the result and its grade."""
        block = readme_block(report)
        assert report["evidence"]["grade"] in block
        assert "docs/METHOD.md" in block

    def test_the_headline_block_is_not_where_the_qualifications_live(self, report) -> None:
        """M14's acceptance check, in the one place it can be asserted mechanically.

        The scorer comparison, the provenance detail and the contract table all moved to the
        method document. They are not deleted and not softened — `test_the_method_document`
        below requires every one of them — they are one click away instead of in front of the
        promise.
        """
        block = readme_block(report)
        for moved in ("What else was measured", "Where these tasks came from", "effective k"):
            assert moved not in block


class TestTheMethodDocument:
    """Everything the README stopped carrying still has to be generated, and still has to be
    checked. A qualification that moves out of sight and out of the build is a deleted one."""

    def test_it_is_generated_and_delimited(self, report) -> None:
        block = method_block(report)
        assert block.startswith(METRICS_START)
        assert block.endswith(METRICS_END)
        assert "do not edit by hand" in block

    def test_the_committed_document_is_current(self, report) -> None:
        current, message = method_doc_current(report)
        assert current, message

    def test_it_names_every_configuration_the_search_compared(self, report) -> None:
        """UPGRADE_V3.md U6: a configuration measured and left out is one nobody can check."""
        block = method_block(report)
        for row in report["cascade"]["scorer_comparison"]:
            assert row["label"] in block

    def test_it_names_the_whole_tie_and_not_a_winner(self, report) -> None:
        """UPGRADE_V3.md U7: a tie is most tempting to round down to one row in prose."""
        ties = report["cascade"]["ties"]
        block = method_block(report)
        for row in ties["tied"]:
            assert row["label"] in block
        assert ties["note"] in block
        assert ties["fallback_reason"] in block

    def test_it_shows_the_whole_evidence_chain_and_not_just_the_grade(self, report) -> None:
        block = method_block(report)
        for link in report["evidence"]["inputs"]:
            assert link["name"] in block
            assert link["source"] in block
