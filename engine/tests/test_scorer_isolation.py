"""The scorer must never be able to reach a gold answer (SPEC.md 7.5).

This is the test the spec asks for by name, and it is the load-bearing one for the whole
product. A scorer that can see whether an answer is correct is not estimating anything: it is
reading the answer key, and every saving the cascade reports would be an artefact of that.

Three layers of check:
  * structural — a ``TaskView`` has no field that could carry gold;
  * behavioural — a scorer given identical inputs with different gold answers scores identically;
  * source — no scorer module imports the grader's gold-aware helpers.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from pathlib import Path

import pytest

from tokop.optimize import scorers
from tokop.optimize.scorers import (
    FEATURE_REGISTRY,
    ScorerError,
    TaskView,
    extract,
    fit_tier_scorer,
    resolve_features,
)
from tokop.workloads.demo.generator import DemoItem

GOLD_FIELDS = {"gold", "correct", "answer_type", "question_type", "sections", "aliases", "params"}


class TestStructuralIsolation:
    def test_a_task_view_has_no_field_that_could_carry_gold(self) -> None:
        names = {f.name for f in fields(TaskView)}
        assert not (names & GOLD_FIELDS), f"TaskView exposes {names & GOLD_FIELDS}"

    def test_a_task_view_is_not_a_demo_item(self) -> None:
        """If a scorer were ever handed the dataset item directly, gold would be one attribute
        away. The types are deliberately different."""
        item_fields = {f.name for f in fields(DemoItem)}
        view_fields = {f.name for f in fields(TaskView)}
        assert "gold" in item_fields
        assert "gold" not in view_fields
        assert not issubclass(TaskView, DemoItem)

    def test_every_registered_feature_takes_only_a_task_view(self) -> None:
        for name, feature in FEATURE_REGISTRY.items():
            signature = inspect.signature(feature.fn)
            parameters = list(signature.parameters.values())
            assert len(parameters) == 1, f"{name} takes more than a view"
            annotation = parameters[0].annotation
            assert annotation in (TaskView, "TaskView"), f"{name} takes {annotation}"


class TestSourceIsolation:
    def test_no_scorer_module_imports_a_gold_aware_helper(self) -> None:
        """`grade`, `check_number` and friends all see gold. A scorer may import the
        *contract* parsers (`extract_answer`, `extract_json_answer`) and nothing else."""
        forbidden = {"grade", "check_number", "check_yes_no", "check_exact", "Grade"}
        source = Path(inspect.getfile(scorers)).read_text()
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            is_grading_import = (
                isinstance(node, ast.ImportFrom) and node.module and "grading" in node.module
            )
            if is_grading_import:
                imported.update(alias.name for alias in node.names)
        assert not (imported & forbidden), f"scorers.py imports {imported & forbidden}"

    def test_the_scorer_module_never_mentions_gold(self) -> None:
        source = Path(inspect.getfile(scorers)).read_text()
        code_lines = []
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("*"):
                continue
            code_lines.append(line)
        code = "\n".join(code_lines)
        # "gold" may appear in prose explaining the isolation; it must not appear in an
        # attribute access or a subscript.
        assert ".gold" not in code
        assert '["gold"]' not in code
        assert "'gold'" not in code


class TestBehaviouralIsolation:
    def test_the_same_output_scores_the_same_whatever_the_gold_answer_is(self) -> None:
        """The decisive check: build two views that are identical, and confirm that no feature
        can distinguish them — because the only thing that differs is the answer key, which
        the scorer cannot see."""
        handbook = "## Returns {#sec-a}\n\nBackpacks have a sixty-day window from delivery."
        quote = "Backpacks have a sixty-day window from delivery."
        output = f'{{"answer": "60", "evidence": "{quote}", "section": "sec-a"}}'
        view_a = TaskView(task_id="t1", question="How long?", output=output, context=handbook)
        view_b = TaskView(task_id="t1", question="How long?", output=output, context=handbook)

        features = resolve_features(list(FEATURE_REGISTRY))
        matrix = extract([view_a, view_b], features)
        assert (matrix[0] == matrix[1]).all()

    def test_a_correct_and_an_incorrect_answer_of_the_same_shape_are_indistinguishable(
        self,
    ) -> None:
        """60 is right and 74 is wrong for this question, but no feature knows that: both are
        well-formed answers citing a real section with a real quote."""
        handbook = "## Returns {#sec-a}\n\nBackpacks have a sixty-day window from delivery."
        quote = "Backpacks have a sixty-day window from delivery."
        right = f'{{"answer": "60", "evidence": "{quote}", "section": "sec-a"}}'
        wrong = f'{{"answer": "74", "evidence": "{quote}", "section": "sec-a"}}'
        features = resolve_features(list(FEATURE_REGISTRY))
        a = extract([TaskView("t", "How long?", right, handbook)], features)
        b = extract([TaskView("t", "How long?", wrong, handbook)], features)
        assert (a == b).all(), "a feature distinguished a right answer from a wrong one"

    def test_the_evidence_feature_checks_the_handbook_and_nothing_else(self) -> None:
        """It rewards a quote that really is in the cited section. A model that invented the
        quote, or cited the wrong section, scores zero — and that is knowable without gold."""
        handbook = (
            "## Returns {#sec-a}\n\nBackpacks have a sixty-day window from delivery."
            "\n\n## Fees {#sec-b}\n\nRestocking is ten percent."
        )
        quote = "Backpacks have a sixty-day window from delivery."
        feature = FEATURE_REGISTRY["evidence_quote_found"].fn

        real = f'{{"answer": "60", "evidence": "{quote}", "section": "sec-a"}}'
        invented = (
            '{"answer": "60", "evidence": "Backpacks may be returned within ninety days.",'
            ' "section": "sec-a"}'
        )
        wrong_section = f'{{"answer": "60", "evidence": "{quote}", "section": "sec-b"}}'

        assert feature(TaskView("t", "q", real, handbook)) == 1.0
        assert feature(TaskView("t", "q", invented, handbook)) == 0.0
        assert feature(TaskView("t", "q", wrong_section, handbook)) == 0.0

    def test_fitting_uses_labels_but_the_fitted_scorer_never_sees_them_again(self) -> None:
        views = [
            TaskView(f"t{i}", "q" * (i + 1), '{"answer": "1"}' if i % 2 else "rambling text")
            for i in range(40)
        ]
        labels = [1 if i % 2 else 0 for i in range(40)]
        scorer, _ = fit_tier_scorer("cheap", views, labels, ["output_parses", "output_length"])
        scores = scorer.score(views)
        # The scorer is a function of the views alone: scoring the same views twice, with the
        # labels long gone, gives the same answer.
        assert (scorer.score(views) == scores).all()
        assert not hasattr(scorer, "labels")


class TestFeatureRegistry:
    def test_unknown_features_are_refused_and_the_registry_is_listed(self) -> None:
        with pytest.raises(ScorerError, match="output_parses"):
            resolve_features(["not_a_feature"])

    def test_the_demo_workload_only_asks_for_registered_features(self) -> None:
        from tokop.paths import repo_root
        from tokop.workloads.spec import load_workload

        workload = load_workload(repo_root() / "data/demo/workload.yaml")
        resolve_features(workload.scorer_features)  # raises if any is unknown

    def test_demo_specific_features_are_marked_as_such(self) -> None:
        assert FEATURE_REGISTRY["evidence_quote_found"].workload == "returns-support"
        assert FEATURE_REGISTRY["output_parses"].workload is None
