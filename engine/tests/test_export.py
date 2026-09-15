"""Deploy exports artifacts; it does not carry traffic (UPGRADE_V4.md M14.4).

SPEC.md section 3 excludes any gateway for other applications' traffic, so what "Deploy" can
honestly mean here is a diff, a config file and a routing rule a team applies themselves. These
tests check the two things that make such an artifact worth having: that it is generated from
the report rather than typed beside it, and that it carries the evidence it rests on — a config
file that looks authoritative while describing an inconclusive result is worse than none.
"""

from __future__ import annotations

import ast
import json

import pytest

from tokop.optimize.export import ARTIFACTS, ExportError, export
from tokop.optimize.report import ReportPayload, build_report
from tokop.paths import repo_root
from tokop.workloads.spec import load_workload


@pytest.fixture(scope="module")
def report() -> ReportPayload:
    return build_report()


@pytest.fixture(scope="module")
def workload():
    return load_workload(repo_root() / "data/demo/workload.yaml")


class TestTheRoutingRule:
    def test_it_is_python_that_parses(self, report, workload, tmp_path) -> None:
        """A routing rule nobody can run is a paragraph with indentation."""
        result = export(report, "routing-rule", tmp_path, workload)
        ast.parse(result.text)

    def test_it_routes_the_way_the_cascade_does(self, report, workload, tmp_path) -> None:
        """Executed, not just parsed: the thresholds and the escalation order are the claim."""
        namespace: dict[str, object] = {}
        exec(
            compile(export(report, "routing-rule", tmp_path, workload).text, "rule", "exec"),
            namespace,
        )
        tiers = namespace["TIERS"]
        route = namespace["route"]
        assert isinstance(tiers, list)
        cascade = report["cascade"]["tiers"]
        assert [t[0] for t in tiers] == [row["tier"] for row in cascade]
        assert [t[1] for t in tiers] == [row["model_id"] for row in cascade]

        # A task every tier is confident about stops at the cheapest.
        assert route("q", lambda _task, _tier: 1.0) == cascade[0]["model_id"]  # type: ignore[operator]
        # A task nothing is confident about lands on the last tier, which has no threshold.
        assert route("q", lambda _task, _tier: 0.0) == cascade[-1]["model_id"]  # type: ignore[operator]

    def test_the_last_tier_has_no_threshold_to_clear(self, report, workload, tmp_path) -> None:
        namespace: dict[str, object] = {}
        exec(
            compile(export(report, "routing-rule", tmp_path, workload).text, "rule", "exec"),
            namespace,
        )
        assert namespace["TIERS"][-1][2] is None  # type: ignore[index]


class TestTheCascadeConfig:
    def test_it_is_json_and_matches_the_report(self, report, workload, tmp_path) -> None:
        config = json.loads(export(report, "cascade-config", tmp_path, workload).text)
        assert config["workload"] == report["workload"]["id"]
        assert config["base_pipeline"] == report["cascade"]["base_pipeline"]
        assert [t["tier"] for t in config["tiers"]] == [
            row["tier"] for row in report["cascade"]["tiers"]
        ]

    def test_it_carries_the_result_it_rests_on(self, report, workload, tmp_path) -> None:
        """Including the awkward parts. A config that omits them invites a reader to assume."""
        proved = json.loads(export(report, "cascade-config", tmp_path, workload).text)["proved"]
        assert proved["verdict"] == report["proof"]["verdict"]["label"]
        assert proved["evidence"] == report["evidence"]["grade"]
        assert proved["n"] == report["proof"]["candidate"]["n"]
        assert proved["origin"] == report["provenance"]["origin"]


class TestThePromptDiff:
    def test_it_goes_from_what_runs_today_to_what_is_recommended(
        self, report, workload, tmp_path
    ) -> None:
        result = export(report, "prompt-diff", tmp_path, workload)
        assert result.text.startswith("--- " + report["proof"]["baseline"]["pipeline"])
        assert report["cascade"]["base_pipeline"] in result.text.splitlines()[1]
        assert any(line.startswith("+") for line in result.text.splitlines())

    def test_it_names_the_cache_breakpoint_the_rewrite_adds(
        self, report, workload, tmp_path
    ) -> None:
        """The cache-friendly order is most of the saving, and a diff that hid the breakpoint
        would show the reader a prompt rewrite and not the thing that made it cheap."""
        assert "[cache:" in export(report, "prompt-diff", tmp_path, workload).text


class TestWhatItRefuses:
    def test_an_unknown_artifact(self, report, workload, tmp_path) -> None:
        with pytest.raises(ExportError, match="unknown artifact"):
            export(report, "gateway", tmp_path, workload)  # type: ignore[arg-type]

    def test_a_prompt_diff_without_the_prompts(self, report, tmp_path) -> None:
        with pytest.raises(ExportError, match="needs the workload"):
            export(report, "prompt-diff", tmp_path, None)


class TestEveryArtifactWritesAFile:
    def test_all_three(self, report, workload, tmp_path) -> None:
        for artifact in ARTIFACTS:
            result = export(report, artifact, tmp_path, workload)
            assert result.path.exists()
            assert result.path.read_text() == result.text
            assert result.note, f"{artifact} does not say what it is"
