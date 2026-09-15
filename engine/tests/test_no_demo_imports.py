"""The engine must be able to run a workload without the demo (UPGRADE_V4.md M15).

D41.2 recorded the shape of the problem: "ships one workload" undersells it. The coupling was
not a missing command but a type from one workload's generator — `DemoItem` — sitting in the
signature of the runner, the recorder, the annotator and the report, with `DatasetBundle` beside
it and `build_dataset()` called directly wherever tasks were needed.

Two layers of check, because either alone would miss the interesting failure:

* **Import time.** No neutral module may pull in `tokop.workloads.demo` when it is imported.
  Run in a subprocess, because `sys.modules` in a test process is polluted by every other test.
* **Run time.** Loading a workload that was never generated must not reach for the demo either.
  Half the demo imports are lazy and sit inside functions (`report.py`, `cli.py`, `annotate.py`),
  where the AST scan `test_scorer_isolation.py` uses would never see them.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from tokop.paths import repo_root

NEUTRAL_MODULES = [
    "tokop.workloads.runner",
    "tokop.workloads.bundle",
    "tokop.workloads.item",
    "tokop.workloads.grading",
    "tokop.workloads.provenance",
    "tokop.optimize.report",
    "tokop.optimize.scorers",
    "tokop.optimize.proof",
    "tokop.recorder",
    "tokop.annotate",
]

WORKLOAD_YAML = """
workload:
  id: {id}
  name: A workload nobody generated
  description: Two tasks read from a file, to prove the engine can read a file.
  dataset:
    generator: ingested
    seed: 1
    size: 2
    calibration_size: 1
    source: dataset.jsonl
    grounding: grounding.md
  margin: 0.03
  allowed_providers: [simulated]
pipelines:
  P0:
    name: The only pipeline
    description: One call.
    model_role: frontier
    max_tokens: 64
    user:
      - text: "{{{{grounding}}}}\\n\\n{{{{question}}}}"
"""

ITEMS = [
    {
        "id": "t1",
        "question": "What is the escalation code for a disk-full alert?",
        "gold": "E12",
        "answer_type": "enum",
        "split": "calibration",
    },
    {
        "id": "t2",
        "question": "What is the escalation code for a certificate expiry?",
        "gold": "E31",
        "answer_type": "enum",
        "split": "test",
        "unknown_column": "kept, not dropped",
    },
]


def run_python(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        cwd=repo_root(),
        timeout=300,
    )


class TestImportTime:
    @pytest.mark.parametrize("module", NEUTRAL_MODULES)
    def test_a_neutral_module_does_not_import_the_demo(self, module: str) -> None:
        result = run_python(f"""
            import importlib, sys
            importlib.import_module({module!r})
            leaked = sorted(m for m in sys.modules if m.startswith("tokop.workloads.demo"))
            print("\\n".join(leaked))
        """)
        assert result.returncode == 0, result.stderr
        leaked = [line for line in result.stdout.splitlines() if line]
        assert not leaked, f"{module} imports {leaked}"


class TestRunTime:
    def test_loading_a_workload_nobody_generated_reaches_for_nothing_demo(self, tmp_path) -> None:
        """The check the AST scan cannot make: lazy imports fire during the call, not the import.

        A workload with `generator: ingested` has no template space, no policy and no handbook,
        and every one of those is something the demo would have supplied.
        """
        directory = tmp_path / "ingested"
        directory.mkdir()
        (directory / "workload.yaml").write_text(WORKLOAD_YAML.format(id="ingested"))
        (directory / "grounding.md").write_text("E12 disk full. E31 certificate expiry.\n")
        (directory / "dataset.jsonl").write_text(
            "\n".join(json.dumps(item, sort_keys=True) for item in ITEMS)
        )

        result = run_python(f"""
            import sys
            from tokop.workloads.bundle import load_bundle
            from tokop.workloads.spec import load_workload
            from pathlib import Path

            workload = load_workload(Path({str(directory / "workload.yaml")!r}))
            bundle = load_bundle(workload)
            assert len(bundle.items) == 2, bundle.items
            assert len(bundle.calibration) == 1
            assert len(bundle.test) == 1
            assert bundle.grounding.startswith("E12")
            assert bundle.test[0].gold == "E31"
            assert bundle.test[0].extra == {{"unknown_column": "kept, not dropped"}}
            leaked = sorted(m for m in sys.modules if m.startswith("tokop.workloads.demo"))
            print("\\n".join(leaked))
        """)
        assert result.returncode == 0, result.stderr
        leaked = [line for line in result.stdout.splitlines() if line]
        assert not leaked, f"loading an ingested workload imported {leaked}"

    def test_grading_an_ingested_answer_needs_nothing_from_the_demo(self) -> None:
        result = run_python("""
            import sys
            from tokop.workloads.grading import grade
            from tokop.workloads.item import IngestedItem

            item = IngestedItem(id="t", question="q", gold="E31", answer_type="enum")
            outcome = grade("Final answer: E31", item.gold, item.answer_type, item.aliases)
            assert outcome.correct, outcome
            assert item.router_view() == {"id": "t", "question": "q"}
            leaked = sorted(m for m in sys.modules if m.startswith("tokop.workloads.demo"))
            print("\\n".join(leaked))
        """)
        assert result.returncode == 0, result.stderr
        assert not [line for line in result.stdout.splitlines() if line]


class TestTheAcceptanceCheck:
    """UPGRADE_V4.md M15: prove a second workload, and fail if the demo is imported doing it.

    This is the one that could not be written before M15b, because until the report took a
    workload there was nothing to point at a second one. It runs the whole report — replay,
    calibration search, cascade simulation, proof — over `data/incident-triage/`, then reads
    `sys.modules`.
    """

    def test_proving_a_second_workload_imports_nothing_from_the_demo(self) -> None:
        result = run_python("""
            import sys
            from tokop.optimize.report import build_report

            payload = build_report(workload_path="data/incident-triage/workload.yaml")
            assert payload["workload"]["id"] == "incident-triage", payload["workload"]
            assert payload["proof"]["candidate"]["n"] == 99, payload["proof"]["candidate"]["n"]
            verdicts = ("non_inferior", "inconclusive", "worse")
            assert payload["proof"]["verdict"]["label"] in verdicts
            grades = ("recording", "simulated", "insufficient")
            assert payload["evidence"]["grade"] in grades
            leaked = sorted(m for m in sys.modules if m.startswith("tokop.workloads.demo"))
            print("\\n".join(leaked))
        """)
        assert result.returncode == 0, result.stderr
        leaked = [line for line in result.stdout.splitlines() if line]
        assert not leaked, f"proving the second workload imported {leaked}"

    def test_the_second_workload_is_not_the_demo_in_disguise(self) -> None:
        """A second workload that differs only in wording tests the plumbing and nothing else.

        The answer mix is the check that matters: the demo is money and day counts, this is
        escalation codes, team names and yes/no. A scorer feature that only works on numbers
        would pass on one and fail on the other, which is the kind of thing a second workload
        exists to find.
        """
        from tokop.paths import repo_root
        from tokop.workloads.bundle import load_bundle
        from tokop.workloads.spec import load_workload

        demo = load_bundle(load_workload(repo_root() / "data/demo/workload.yaml"))
        second = load_bundle(load_workload(repo_root() / "data/incident-triage/workload.yaml"))

        demo_types = {item.answer_type for item in demo.items}
        second_types = {item.answer_type for item in second.items}
        assert second_types - demo_types or demo_types - second_types, (
            f"both workloads use exactly {demo_types}; the second tests only the plumbing"
        )
        assert not ({item.question for item in second.items} & {i.question for i in demo.items})

    def test_its_provenance_refuses_certification_for_the_honest_reason(self) -> None:
        """Not one of these tasks was observed in production, and the gate says so."""
        result = run_python("""
            from tokop.optimize.report import build_report

            provenance = build_report(
                workload_path="data/incident-triage/workload.yaml"
            )["dataset_provenance"]
            assert provenance["real_traffic_items"] == 0
            assert provenance["program_generated_items"] == 150
            assert not provenance["certifiable"]
            assert any("real recorded traffic" in r for r in provenance["refusals"])
            print("ok")
        """)
        assert result.returncode == 0, result.stderr


class TestTheDemoStillSatisfiesTheProtocol:
    def test_a_demo_item_is_an_item(self) -> None:
        """`Item` is a protocol so `DemoItem` satisfies it without inheriting anything."""
        from tokop.workloads.demo.dataset import build
        from tokop.workloads.item import Item

        item = next(iter(build().items))
        assert isinstance(item, Item)
        assert item.template_id and item.question_type

    def test_the_demo_bundle_is_the_neutral_bundle(self) -> None:
        from tokop.workloads.bundle import Bundle
        from tokop.workloads.demo.dataset import build

        assert isinstance(build(), Bundle)
