"""`tokop prove` as a CI gate: the exit code is the contract.

The GitHub workflow in `.github/workflows/proof-gate.yml` does nothing but run this command and
let its exit status decide the build. That makes the exit code the whole interface, so it is
tested here rather than trusted. These run the real CLI in a subprocess: an exit code asserted
by calling a function in-process is not the thing CI actually observes.

Nothing here makes a network call. The report is computed over the committed simulated
fixtures, in replay mode.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tokop.paths import repo_root

TOKOP = repo_root() / "engine" / ".venv" / "bin" / "tokop"


def prove(*args: str) -> subprocess.CompletedProcess[str]:
    if not TOKOP.exists():
        pytest.skip("the tokop CLI is not installed; run make setup")
    env = dict(os.environ, TOKOP_MODE="replay")
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run(
        [str(TOKOP), "prove", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root(),
        timeout=600,
    )


class TestProveGate:
    def test_an_inconclusive_verdict_fails_the_build(self) -> None:
        """The demo's own verdict is inconclusive at the 3-point margin, and the gate says so.

        This is the case worth pinning: a gate that passed on "we could not tell" would be
        worse than no gate, because it would read as evidence the change was safe.
        """
        result = prove()
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Inconclusive" in result.stdout

    def test_a_non_inferior_verdict_passes_the_build(self) -> None:
        result = prove("--margin", "0.10")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Non-inferior" in result.stdout

    def test_the_margin_is_applied_and_not_merely_echoed(self) -> None:
        """The same data, two margins, two verdicts: the number changes the answer."""
        strict, loose = prove(), prove("--margin", "0.10")
        assert strict.returncode != loose.returncode
        assert "3-point margin" in strict.stdout
        assert "10-point margin" in loose.stdout

    def test_an_unsupported_workload_is_refused_rather_than_answered(self) -> None:
        """DECISIONS.md D24: reporting the demo's numbers under another workload's name would
        be the worst possible violation of non-negotiable 1, so it exits 2 instead."""
        result = prove("--workload", "data/mine/workload.yaml")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "not supported" in result.stderr

    def test_simulated_data_is_announced_on_stderr(self) -> None:
        """Non-negotiable 2. A CI log that did not say this would let simulated numbers pass
        for recorded ones in the one place nobody looks twice."""
        result = prove("--margin", "0.10")
        assert "simulated test data, not a recording" in result.stderr


class TestProveIsUsableFromCI:
    def test_the_cli_runs_with_no_api_key_in_the_environment(self) -> None:
        """CI has no credentials and must not need any: the proof reads committed fixtures.

        `prove()` strips ANTHROPIC_API_KEY from the subprocess environment, so a gate that had
        quietly grown a dependency on a live call would fail here rather than in someone's
        pipeline.
        """
        result = prove("--margin", "0.10")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "API_KEY" not in result.stderr


class TestTheJudgedGate:
    """`--grading judged` gates on the estimate a workload without labels would get.

    The exit code is the whole interface here too, and it must come from the *judged* verdict
    rather than the gold one — otherwise a user with no labels would be gated on a number they
    could never have computed.
    """

    def test_the_judged_verdict_decides_the_exit_code(self) -> None:
        gold, judged = prove(), prove("--grading", "judged")
        assert "WITHOUT GOLD" in judged.stdout
        # Both are inconclusive at the demo's 3-point margin, for different reasons: the gold
        # interval straddles the margin narrowly, the judged one widely.
        assert gold.returncode == 1
        assert judged.returncode == 1

    def test_a_wide_enough_margin_clears_the_judged_verdict_too(self) -> None:
        result = prove("--grading", "judged", "--margin", "0.20")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "WITHOUT GOLD:  Non-inferior" in result.stdout

    def test_the_gold_and_judged_gates_can_disagree(self) -> None:
        """The point of having both. At a margin the gold interval clears and the judged one
        does not, the judged gate fails — the label-free evidence is genuinely weaker, and a
        gate that hid that would be advertising a proof nobody ran."""
        gold = prove("--margin", "0.05")
        judged = prove("--grading", "judged", "--margin", "0.05")
        assert gold.returncode == 0, gold.stdout + gold.stderr
        assert judged.returncode == 1, judged.stdout + judged.stderr

    def test_an_unknown_grading_mode_is_refused(self) -> None:
        result = prove("--grading", "vibes")
        assert result.returncode == 2
        assert "gold" in result.stderr and "judged" in result.stderr

    def test_the_report_names_what_the_annotation_cost_and_what_it_saved(self) -> None:
        """UPGRADE_V3.md U1's third acceptance check, as a user meets it."""
        result = prove()
        assert "strong-only grading needs" in result.stdout
        assert "judge bias, measured" in result.stdout

    def test_a_budget_above_the_recorded_one_is_refused_rather_than_approximated(self) -> None:
        result = prove("--grading", "judged", "--annotation-budget", "99")
        assert result.returncode == 2
        assert "tokop annotate" in result.stdout + result.stderr

    def test_a_smaller_budget_replays_a_cheaper_annotation_set(self) -> None:
        result = prove("--annotation-budget", "0.30")
        assert result.returncode == 1
        assert "WITHOUT GOLD" in result.stdout


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    if not TOKOP.exists():
        pytest.skip("the tokop CLI is not installed; run make setup")
    env = dict(os.environ, TOKOP_MODE="replay")
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run(
        [str(TOKOP), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root(),
        timeout=900,
    )


class TestTheCanaryGate:
    """`tokop canary` exits like `tokop prove`: 0 while the certificate stands, 1 when a look
    raises. A scheduled canary gates a deploy the way the proof gates a merge, so the exit code
    is the interface and is tested through a subprocess (UPGRADE_V3.md U5)."""

    def test_a_certificate_is_issued_and_a_look_at_it_is_clear(self, tmp_path) -> None:
        cert = tmp_path / "cert.json"
        issued = run_cli("certificate", "--out", str(cert))
        assert issued.returncode == 0, issued.stdout + issued.stderr
        assert cert.exists()

        looked = run_cli("canary", "--certificate", str(cert), "--log", str(tmp_path / "log.json"))
        assert looked.returncode == 0, looked.stdout + looked.stderr
        assert "CANARY CLEAR" in looked.stdout
        # Replaying the same cassettes cannot produce a different answer, and the canary says
        # so rather than reporting a tautology as a pass.
        assert "not tested" in looked.stdout

    def test_a_swapped_snapshot_identifier_raises_and_fails_the_gate(self, tmp_path) -> None:
        import json

        cert = tmp_path / "cert.json"
        run_cli("certificate", "--out", str(cert))
        payload = json.loads(cert.read_text())
        for snapshot in payload["model_snapshots"]:
            if snapshot["role"] == "frontier":
                snapshot["snapshot"] = snapshot["snapshot"] + "-rev2"
        swapped = tmp_path / "swapped.json"
        swapped.write_text(json.dumps(payload, indent=2, sort_keys=True))

        result = run_cli(
            "canary", "--certificate", str(swapped), "--log", str(tmp_path / "swapped-log.json")
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "CANARY RAISED" in result.stdout
        assert "frontier model changed" in result.stdout

    def test_an_expired_certificate_fails_the_gate(self, tmp_path) -> None:
        cert = tmp_path / "cert.json"
        run_cli("certificate", "--out", str(cert))
        result = run_cli(
            "canary",
            "--certificate",
            str(cert),
            "--log",
            str(tmp_path / "log.json"),
            "--today",
            "2099-01-01",
        )
        assert result.returncode == 1
        assert "expired" in result.stdout

    def test_a_missing_certificate_is_refused_with_the_command_that_makes_one(
        self, tmp_path
    ) -> None:
        result = run_cli("canary", "--certificate", str(tmp_path / "absent.json"))
        assert result.returncode == 2
        assert "tokop certificate" in result.stderr

    def test_provenance_reports_and_does_not_gate(self) -> None:
        """A set that cannot certify is a fact about the set, not a defect in the build."""
        result = run_cli("provenance", "--check")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "CERTIFIABLE: no" in result.stdout
        assert "no real recorded traffic" in result.stdout
