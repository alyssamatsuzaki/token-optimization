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
