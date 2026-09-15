"""The judge must never be able to reach a gold answer (UPGRADE_V3.md U1).

The same test `test_scorer_isolation.py` runs for scorers, for the same reason and with more at
stake. A scorer that could see gold would make the routing circular; a **judge** that could see
gold would make the entire gold-free proof circular — the estimate would be a gold measurement
wearing a judge's name, and a user with no labels would get nothing at all.

Three layers, as for scorers:
  * structural — an ``AnswerView`` has no field that could carry gold;
  * source — neither the verifier nor the allocation policy imports a gold-aware helper;
  * behavioural — a verdict is a function of the judge's reply and nothing else.

The **simulator** is exempt and must be: ``workloads/demo/judge_responder.py`` invents how often
a judge is wrong, and it cannot do that without knowing what is right. That is the same licence
``workloads/demo/responder.py`` already has. The line this file defends is that no *engine*
module crosses it.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from pathlib import Path

import pytest

from tokop.optimize import annotation
from tokop.paths import repo_root
from tokop.workloads import verification
from tokop.workloads.demo.generator import DemoItem
from tokop.workloads.spec import VARIABLE, load_workload
from tokop.workloads.verification import AnswerView, VerificationError, verifier_kind

GOLD_FIELDS = {"gold", "correct", "answer_type", "question_type", "sections", "aliases", "params"}

#: `grade` and the checkers all see gold. A verifier may import the *contract* parsers —
#: reading JSON out of a fenced block is not knowledge of the answer — and nothing else.
FORBIDDEN = {"grade", "check_number", "check_yes_no", "check_exact", "Grade"}

GOLD_FREE_MODULES = (verification, annotation)


class TestStructuralIsolation:
    def test_an_answer_view_has_no_field_that_could_carry_gold(self) -> None:
        names = {f.name for f in fields(AnswerView)}
        assert not (names & GOLD_FIELDS), f"AnswerView exposes {names & GOLD_FIELDS}"

    def test_an_answer_view_is_not_a_dataset_item(self) -> None:
        item_fields = {f.name for f in fields(DemoItem)}
        assert "gold" in item_fields
        assert "gold" not in {f.name for f in fields(AnswerView)}
        assert not issubclass(AnswerView, DemoItem)

    def test_the_allocation_features_carry_no_label(self) -> None:
        """The policy decides *who* gets a strong label. If it could see the answer key it
        would sample the items it already knew the answer to, and the correction term would be
        measuring nothing."""
        names = {f.name for f in fields(annotation.ItemFeatures)}
        assert not (names & GOLD_FIELDS), f"ItemFeatures exposes {names & GOLD_FIELDS}"


class TestSourceIsolation:
    @pytest.mark.parametrize("module", GOLD_FREE_MODULES, ids=lambda m: m.__name__)
    def test_no_gold_aware_helper_is_imported(self, module: object) -> None:
        source = Path(inspect.getfile(module)).read_text()  # type: ignore[arg-type]
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module and "grading" in node.module:
                imported.update(alias.name for alias in node.names)
        assert not (imported & FORBIDDEN), f"{module} imports {imported & FORBIDDEN}"

    @pytest.mark.parametrize("module", GOLD_FREE_MODULES, ids=lambda m: m.__name__)
    def test_gold_is_never_read_as_an_attribute_or_a_key(self, module: object) -> None:
        source = Path(inspect.getfile(module)).read_text()  # type: ignore[arg-type]
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith(("#", '"', "*"))
        )
        assert ".gold" not in code
        assert '["gold"]' not in code
        assert "'gold'" not in code


class TestThePromptItself:
    def test_the_judge_prompt_can_only_render_variables_that_are_not_gold(self) -> None:
        """The last place gold could leak is the prompt. The runner supplies exactly four
        variables to a judge and none of them is an answer key; this asserts the demo's judge
        prompt asks for nothing else, so a workload cannot smuggle one in through a template."""
        workload = load_workload(repo_root() / "data/demo/workload.yaml")
        assert workload.judge is not None
        rendered = "".join(block.text for block in [*workload.judge.system, *workload.judge.user])
        referenced = set(VARIABLE.findall(rendered))
        assert referenced <= {"grounding", "question", "answer", "timestamp"}
        assert not (referenced & GOLD_FIELDS)

    def test_the_runner_supplies_exactly_those_variables(self) -> None:
        from datetime import date

        from tokop.core.registry import load_registry
        from tokop.workloads.runner import Runner

        registry = load_registry()
        workload = load_workload(repo_root() / "data/demo/workload.yaml")
        runner = Runner(
            workload,
            registry,
            registry.snapshot(list(registry.roles.values()), date(2026, 9, 11)),
            adapter=None,  # type: ignore[arg-type]
            provider="simulated",
            grounding="HANDBOOK",
        )
        variables = runner.judge_variables(
            AnswerView(task_id="t1", question="q", answer="a", arm="baseline")
        )
        assert set(variables) == {"grounding", "question", "answer", "timestamp"}


class TestBehaviouralIsolation:
    def test_a_verdict_is_a_function_of_the_reply_and_nothing_else(self) -> None:
        reply = '{"correct": true, "confidence": 0.8, "why": "matches sec-a"}'
        first = verifier_kind("judge-v1").parse(reply)
        second = verifier_kind("judge-v1").parse(reply)
        assert first == second

    def test_an_unreadable_verdict_is_a_failure_not_a_skip(self) -> None:
        """The same rule `grading.py` applies to an unreadable answer. A judge that returned
        prose has not verified anything, and treating that as 'no opinion' would quietly drop
        the item out of the denominator."""
        verdict = verifier_kind("judge-v1").parse("I think this looks broadly fine, honestly.")
        assert verdict.correct == 0
        assert not verdict.parsed
        assert verdict.uncertainty == pytest.approx(1.0)

    def test_an_unknown_verifier_names_the_registry(self) -> None:
        with pytest.raises(VerificationError, match="judge-v1"):
            verifier_kind("vibes-v9")
