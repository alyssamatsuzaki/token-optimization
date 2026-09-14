"""Reading a judge's reply, and refusing to invent one (UPGRADE_V3.md U1).

``grading.py``'s rule is that an answer nobody can parse is a failure, not a skip. The same rule
applies one level up: a verdict nobody can parse has verified nothing. It is recorded as wrong
and as maximally unsure, which is what puts it in front of the annotation budget instead of
letting it disappear.
"""

from __future__ import annotations

import json

import pytest

from tokop.workloads.demo.judge_responder import (
    ANSWER_CLOSE,
    ANSWER_OPEN,
    ASSUMED_BASE_RATE,
    JUDGE_SENSITIVITY,
    JUDGE_SPECIFICITY,
    extract_reviewed_answer,
    posterior,
    rates_for,
)
from tokop.workloads.verification import (
    JUDGE_V1,
    QueueEntry,
    VerificationError,
    binary_entropy,
    read_queue_results,
    render_queue,
    verify,
)


class TestReadingAVerdict:
    def test_the_json_contract(self) -> None:
        verdict = verify('{"correct": true, "confidence": 0.83, "why": "matches sec-a"}')
        assert verdict.correct == 1
        assert verdict.confidence == pytest.approx(0.83)
        assert verdict.reason == "matches sec-a"
        assert verdict.parsed

    def test_a_fenced_block_is_tolerated(self) -> None:
        reply = '```json\n{"correct": false, "confidence": 0.2, "why": "wrong band"}\n```'
        assert verify(reply).correct == 0

    def test_a_verdict_line_is_accepted_rather_than_thrown_away(self) -> None:
        verdict = verify("Having checked sec-returns-windows,\nVerdict: yes")
        assert verdict.correct == 1
        assert verdict.parsed
        # It gave a verdict and no confidence, so nothing is invented about how sure it was.
        assert verdict.confidence == 0.5

    def test_prose_is_recorded_as_unverified_not_as_an_error(self) -> None:
        verdict = verify("Honestly this one is a bit of a judgement call.")
        assert verdict.correct == 0
        assert not verdict.parsed
        assert verdict.uncertainty == pytest.approx(1.0)

    def test_an_empty_reply_is_unverified(self) -> None:
        assert not verify("").parsed

    def test_a_confidence_outside_zero_to_one_is_ignored_rather_than_clamped(self) -> None:
        """A judge that returned 4.2 did not mean 1.0. Clamping would turn a broken reply into
        a confident one."""
        verdict = verify('{"correct": true, "confidence": 4.2}')
        assert verdict.correct == 1
        assert verdict.confidence == 0.5

    def test_a_verdict_that_is_not_a_verdict_falls_through(self) -> None:
        assert not verify('{"correct": "maybe", "confidence": 0.5}').parsed

    def test_string_booleans_are_read(self) -> None:
        assert verify('{"correct": "yes", "confidence": 0.9}').correct == 1
        assert verify('{"correct": "no", "confidence": 0.9}').correct == 0

    def test_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(VerificationError, match=JUDGE_V1):
            verify("{}", kind="gut-feel-v2")


class TestUncertainty:
    def test_entropy_is_zero_at_certainty_and_one_at_a_coin_flip(self) -> None:
        assert binary_entropy(0.0) == 0.0
        assert binary_entropy(1.0) == 0.0
        assert binary_entropy(0.5) == pytest.approx(1.0)

    def test_entropy_is_symmetric(self) -> None:
        assert binary_entropy(0.2) == pytest.approx(binary_entropy(0.8))

    def test_a_probability_outside_the_unit_interval_is_refused(self) -> None:
        with pytest.raises(VerificationError, match=r"\[0, 1\]"):
            binary_entropy(1.4)


class TestTheReviewQueue:
    def _entry(self, task_id: str) -> QueueEntry:
        return QueueEntry(
            task_id=task_id,
            arm="baseline",
            question="How long is the window?",
            answer='{"answer": "60"}',
            judge_correct=1,
            judge_confidence=0.9,
            sampling_rate=0.3,
        )

    def test_tokop_never_fills_in_a_human_verdict(self) -> None:
        payload = json.loads(render_queue([self._entry("t1"), self._entry("t2")]))
        assert [row["human_correct"] for row in payload["entries"]] == [None, None]

    def test_a_completed_queue_reads_back(self) -> None:
        payload = json.loads(render_queue([self._entry("t1"), self._entry("t2")]))
        payload["entries"][0]["human_correct"] = 1
        results = read_queue_results(json.dumps(payload))
        assert results[("t1", "baseline")].correct == 1
        # The blank row is skipped, not guessed.
        assert ("t2", "baseline") not in results

    def test_a_row_that_is_neither_one_nor_zero_nor_null_is_refused(self) -> None:
        payload = json.loads(render_queue([self._entry("t1")]))
        payload["entries"][0]["human_correct"] = "probably?"
        with pytest.raises(VerificationError, match="neither 1, 0 nor null"):
            read_queue_results(json.dumps(payload))

    def test_a_queue_that_is_not_a_queue_is_refused(self) -> None:
        with pytest.raises(VerificationError, match="valid JSON"):
            read_queue_results("not json at all")
        with pytest.raises(VerificationError, match="`entries` list"):
            read_queue_results('{"schema": "x"}')


class TestTheSimulatedJudge:
    """The invented half. What matters is that its shape is the one it claims."""

    def test_it_is_more_lenient_than_it_is_harsh_at_every_tier(self) -> None:
        """The characteristic LLM-judge failure, and the reason the simulator models
        sensitivity and specificity separately instead of one accuracy."""
        for role in JUDGE_SENSITIVITY:
            assert JUDGE_SENSITIVITY[role] > JUDGE_SPECIFICITY[role]

    def test_checking_is_easier_than_answering(self) -> None:
        """The asymmetry the whole gold-free design rests on: the judge tiers are closer
        together than the answer tiers are."""
        from tokop.workloads.demo.responder import ACCURACY

        answer_gap = ACCURACY["frontier"]["computation"] - ACCURACY["cheap"]["computation"]
        judge_gap = JUDGE_SENSITIVITY["frontier"] - JUDGE_SENSITIVITY["cheap"]
        assert judge_gap < answer_gap

    def test_a_harder_question_type_costs_the_judge_both_rates(self) -> None:
        easy = rates_for("cheap", "lookup")
        hard = rates_for("cheap", "computation")
        assert hard[0] < easy[0]
        assert hard[1] < easy[1]

    def test_the_stated_confidence_cannot_depend_on_the_truth(self) -> None:
        """The check that keeps the fixtures honest. If the judge's confidence were derived
        from whether the answer is actually right, the allocation policy would be reading a
        gold label in disguise and its measured advantage would be an artefact — the mistake
        DECISIONS.md D27 caught in the sampling scorer."""
        import inspect

        signature = inspect.signature(posterior)
        assert set(signature.parameters) == {"sensitivity", "specificity", "says_correct"}
        # And it really is a posterior: saying "correct" must raise the probability above the
        # prior, saying "wrong" must lower it.
        sensitivity, specificity = rates_for("cheap", "lookup")
        assert posterior(sensitivity, specificity, True) > ASSUMED_BASE_RATE
        assert posterior(sensitivity, specificity, False) < ASSUMED_BASE_RATE

    def test_a_better_judge_states_a_sharper_confidence(self) -> None:
        cheap = rates_for("cheap", "lookup")
        frontier = rates_for("frontier", "lookup")
        assert binary_entropy(posterior(*frontier, True)) < binary_entropy(posterior(*cheap, True))

    def test_the_answer_under_review_is_found_by_its_markers(self) -> None:
        prompt = f"Question: q\n\n{ANSWER_OPEN}\n  the answer  \n{ANSWER_CLOSE}\n"
        assert extract_reviewed_answer(prompt) == "the answer"

    def test_a_prompt_with_no_answer_in_it_yields_nothing(self) -> None:
        assert extract_reviewed_answer("Question: q") is None


class TestTheWorkloadSchema:
    """A judged workload that cannot be judged is refused when it loads, not halfway through
    an annotation run after money has been spent on the cheap judge."""

    def _write(self, tmp_path: object, extra: str) -> object:
        from pathlib import Path

        base = """
workload:
  id: w
  name: W
  description: d
  dataset: {generator: demo, seed: 1, size: 10, calibration_size: 4}
"""
        path = Path(str(tmp_path)) / "workload.yaml"
        path.write_text(base + extra)
        return path

    def test_grading_defaults_to_gold_so_existing_workloads_are_untouched(
        self, tmp_path: object
    ) -> None:
        from tokop.workloads.spec import load_workload

        workload = load_workload(self._write(tmp_path, ""))  # type: ignore[arg-type]
        assert workload.grading == "gold"
        assert workload.judge is None
        assert workload.annotation is None

    def test_judged_without_a_judge_block_is_refused(self, tmp_path: object) -> None:
        from tokop.workloads.spec import WorkloadError, load_workload

        with pytest.raises(WorkloadError, match="defines no `judge:` block"):
            load_workload(self._write(tmp_path, "  grading: judged\n"))  # type: ignore[arg-type]

    def test_an_unknown_grading_mode_is_refused(self, tmp_path: object) -> None:
        from tokop.workloads.spec import WorkloadError, load_workload

        with pytest.raises(WorkloadError, match="gold"):
            load_workload(self._write(tmp_path, "  grading: vibes\n"))  # type: ignore[arg-type]

    def test_a_judge_prompt_that_cannot_see_the_answer_is_refused(self, tmp_path: object) -> None:
        from tokop.workloads.spec import WorkloadError, load_workload

        extra = """
judge:
  user:
    - text: "Customer question: {{question}}"
"""
        with pytest.raises(WorkloadError, match=r"never references \{\{answer\}\}"):
            load_workload(self._write(tmp_path, extra))  # type: ignore[arg-type]

    def test_an_annotation_budget_without_a_judge_is_refused(self, tmp_path: object) -> None:
        from tokop.workloads.spec import WorkloadError, load_workload

        extra = "\nannotation:\n  budget_usd: 5\n"
        with pytest.raises(WorkloadError, match="no `judge:` block"):
            load_workload(self._write(tmp_path, extra))  # type: ignore[arg-type]

    def test_an_unknown_allocation_policy_is_refused(self, tmp_path: object) -> None:
        from tokop.workloads.spec import WorkloadError, load_workload

        extra = """
judge:
  user:
    - text: "{{question}} {{answer}}"
annotation:
  budget_usd: 5
  policy: whatever-is-cheapest
"""
        with pytest.raises(WorkloadError, match="cost-optimal"):
            load_workload(self._write(tmp_path, extra))  # type: ignore[arg-type]

    def test_a_zero_sampling_floor_is_refused(self, tmp_path: object) -> None:
        from tokop.workloads.spec import WorkloadError, load_workload

        extra = """
judge:
  user:
    - text: "{{question}} {{answer}}"
annotation:
  budget_usd: 5
  min_rate: 0
"""
        with pytest.raises(WorkloadError, match="inverse weight infinite"):
            load_workload(self._write(tmp_path, extra))  # type: ignore[arg-type]

    def test_the_demo_workload_declares_a_judge_and_a_budget(self) -> None:
        from tokop.paths import repo_root
        from tokop.workloads.spec import load_workload

        workload = load_workload(repo_root() / "data/demo/workload.yaml")
        assert workload.judge is not None
        assert workload.annotation is not None
        assert workload.annotation.budget_usd > 0
        # The demo keeps its answer key: the judged estimate is computed *beside* the gold one
        # so the two can be compared, which is the whole demonstration.
        assert workload.grading == "gold"
