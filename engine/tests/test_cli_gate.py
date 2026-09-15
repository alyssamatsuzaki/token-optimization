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
import re
import subprocess

import pytest

from tokop.paths import repo_root

TOKOP = repo_root() / "engine" / ".venv" / "bin" / "tokop"

#: Rich writes colour into tracebacks, and where it breaks a highlighted path depends on the
#: terminal width and the length of the absolute path. Strip the escapes before looking for a
#: substring, or the test measures the runner's console rather than the CLI's behaviour.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(text: str) -> str:
    return ANSI.sub("", text)


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

    def test_a_workload_that_does_not_exist_is_refused(self) -> None:
        """Until M15 this refused *every* workload but the demo by name. What it must still
        refuse is one that is not there — silently falling back to the demo's fixtures would be
        the worst available violation of non-negotiable 1.

        The output is stripped of ANSI escapes before the path is looked for. Rich colours a
        traceback's path, and where it breaks the highlight depends on the terminal width and
        on how long the absolute path is — so on a CI runner the escape codes land between the
        directory and the filename and a plain substring search fails on formatting rather than
        on behaviour.
        """
        result = prove("--workload", "data/mine/workload.yaml")
        assert result.returncode != 0, result.stdout + result.stderr
        assert "data/mine/workload.yaml" in plain(result.stdout + result.stderr)

    def test_a_second_workload_is_answered_with_its_own_numbers(self) -> None:
        """The same guarantee, now that a second workload runs (UPGRADE_V4.md M15).

        The danger was never the refusal; it was reporting one workload's numbers under
        another's name. So this asserts the two disagree: a different split size, a different
        title, and a different verdict sentence from the demo's.
        """
        second = prove("--workload", "data/incident-triage/workload.yaml")
        demo = prove()
        assert "Incident triage from a runbook" in second.stdout
        assert "n = 99" in second.stdout, second.stdout
        assert "n = 200" in demo.stdout
        assert "Returns-policy" not in second.stdout

    def test_a_graph_workload_is_answered_with_its_own_numbers(self) -> None:
        """A third workload, and the first whose baseline makes more than one call per task."""
        third = prove("--workload", "data/catalogue-agent/workload.yaml")
        assert third.returncode in (0, 1), third.stdout + third.stderr
        assert "Service catalogue agent" in third.stdout
        assert "n = 100" in third.stdout, third.stdout
        assert "Incident triage" not in third.stdout

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


class TestRecordRefusesBeforeItSpends:
    """The gates in front of a charge, exercised through the real CLI (UPGRADE_V4.md M13).

    No network call is made and none could be: every assertion here is about a refusal that
    happens before any adapter is built. The key in the environment is a string, not a
    credential — it exists only to get past the consent gate so the gate behind it can be seen.
    """

    def record(self, *args: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
        if not TOKOP.exists():
            pytest.skip("the tokop CLI is not installed; run make setup")
        env = {**os.environ, "TOKOP_MODE": "live", **env_overrides}
        return subprocess.run(
            [str(TOKOP), "record", *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=repo_root(),
            timeout=600,
        )

    def test_replay_mode_refuses(self) -> None:
        result = self.record(TOKOP_MODE="replay")
        assert result.returncode == 2
        assert "only runs with TOKOP_MODE=live" in result.stderr

    def test_no_key_refuses(self) -> None:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        result = subprocess.run(
            [str(TOKOP), "record"],
            capture_output=True,
            text=True,
            env=dict(env, TOKOP_MODE="live", RECORD_BUDGET_USD="25"),
            cwd=repo_root(),
            timeout=600,
        )
        assert result.returncode == 2
        assert "ANTHROPIC_API_KEY is not set" in result.stderr

    def test_a_key_without_a_budget_refuses(self) -> None:
        """Setting RECORD_BUDGET_USD *is* the consent to spend. Its absence is a refusal."""
        env = dict(os.environ)
        env.pop("RECORD_BUDGET_USD", None)
        result = subprocess.run(
            [str(TOKOP), "record"],
            capture_output=True,
            text=True,
            env=dict(env, TOKOP_MODE="live", ANTHROPIC_API_KEY="not-a-real-key"),
            cwd=repo_root(),
            timeout=600,
        )
        assert result.returncode == 2
        assert "RECORD_BUDGET_USD is not set" in result.stderr

    def test_an_underpowered_split_refuses_before_any_adapter_is_built(self) -> None:
        """The gate that matters most: it fires after consent and before anything is priced.

        The demo's test split is 200 tasks and needs 204 at its observed discordance, so this
        is the real refusal rather than a contrived one. Recording it would spend the whole
        budget to reproduce the "inconclusive" the repository already has.
        """
        result = self.record(ANTHROPIC_API_KEY="not-a-real-key", RECORD_BUDGET_USD="25")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "204" in result.stdout + result.stderr
        assert "--underpowered" in result.stderr
        assert "inconclusive" in result.stderr
