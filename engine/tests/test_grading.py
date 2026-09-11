"""Checkers (SPEC.md section 6). An unparseable answer is a failure, never a skip."""

from __future__ import annotations

import pytest

from tokop.workloads.grading import (
    Grade,
    extract_answer,
    extract_json_answer,
    grade,
    normalize,
    parse_number,
    parse_yes_no,
)


class TestExtraction:
    def test_json_contract(self) -> None:
        out = '{"answer": "74", "evidence": "quote", "section": "sec-loyalty"}'
        assert extract_answer(out) == "74"
        assert extract_json_answer(out)["section"] == "sec-loyalty"

    def test_json_inside_a_fenced_block(self) -> None:
        assert extract_answer('```json\n{"answer": "yes"}\n```') == "yes"

    def test_final_answer_line(self) -> None:
        assert extract_answer("Long reasoning...\n\nFinal answer: 45") == "45"

    def test_the_last_final_answer_line_wins(self) -> None:
        """A model that restates the line has refined its answer, not added a second one."""
        assert extract_answer("Final answer: 40\nOn reflection...\nFinal answer: 45") == "45"

    def test_a_trailing_period_is_stripped(self) -> None:
        assert extract_answer("Final answer: store credit.") == "store credit"

    def test_a_short_bare_answer_is_accepted(self) -> None:
        assert extract_answer("74") == "74"

    def test_a_long_answer_with_no_contract_is_unparseable(self) -> None:
        essay = "This is a long response that never commits to an answer. " * 5
        assert extract_answer(essay) is None

    def test_empty_output_is_unparseable(self) -> None:
        assert extract_answer("") is None
        assert extract_answer("   \n ") is None

    def test_malformed_json_is_not_silently_accepted(self) -> None:
        assert extract_json_answer('{"answer": ') is None

    def test_json_without_an_answer_key_falls_through(self) -> None:
        assert extract_json_answer('{"result": "45"}') == {"result": "45"}
        assert extract_answer('{"result": "45"}') is None


class TestParsers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("45", "45"),
            ("$129.00", "129.00"),
            ("1,234.56", "1234.56"),
            ("15%", "15"),
            ("-4", "-4"),
            ("the answer is 74 days", "74"),
        ],
    )
    def test_number_parsing(self, text: str, expected: str) -> None:
        from decimal import Decimal

        assert parse_number(text) == Decimal(expected)

    def test_number_parsing_fails_loudly(self) -> None:
        assert parse_number("no digits here") is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("yes", "yes"), ("Yes.", "yes"), ("no", "no"), ("Not allowed", "no"), ("true", "yes")],
    )
    def test_yes_no_parsing(self, text: str, expected: str) -> None:
        assert parse_yes_no(text) == expected

    def test_yes_no_parsing_rejects_other_words(self) -> None:
        assert parse_yes_no("maybe") is None

    def test_normalization_folds_case_accents_and_whitespace(self) -> None:
        assert normalize("  Store   Crédit! ") == "store credit"


class TestGrade:
    def test_numbers_use_a_hundredth_tolerance(self) -> None:
        assert grade("Final answer: 129.00", "129.00", "money").correct
        assert grade("Final answer: 129.005", "129.00", "money").correct
        assert not grade("Final answer: 129.02", "129.00", "money").correct

    def test_yes_no_is_exact_after_normalization(self) -> None:
        assert grade('{"answer": "Yes"}', "yes", "yes_no").correct
        assert not grade('{"answer": "no"}', "yes", "yes_no").correct

    def test_strings_accept_an_alias(self) -> None:
        result = grade("Final answer: credit", "store credit", "string", ("credit",))
        assert result.correct

    def test_a_short_sentence_containing_the_answer_counts(self) -> None:
        assert grade('{"answer": "as store credit"}', "store credit", "string").correct

    def test_a_wrong_enum_fails(self) -> None:
        assert not grade('{"answer": "tier_1"}', "tier_3", "enum").correct

    def test_unparseable_output_is_a_failure_not_an_error(self) -> None:
        essay = "I considered several possibilities but cannot be certain. " * 6
        result = grade(essay, "45", "number")
        assert result.correct is False
        assert result.parse_failed
        assert "unparseable answer is a failure" in result.reason

    def test_a_parseable_but_non_numeric_answer_fails_cleanly(self) -> None:
        result = grade('{"answer": "not covered"}', "45", "number")
        assert not result.correct
        assert result.parsed is None

    def test_a_bad_gold_answer_raises_rather_than_grading(self) -> None:
        """A dataset bug must not quietly become a model failure."""
        with pytest.raises(ValueError, match="dataset is wrong"):
            grade("Final answer: 45", "not-a-number", "number")

    def test_an_unknown_answer_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown answer type"):
            grade("Final answer: 45", "45", "haiku")

    def test_grade_serializes(self) -> None:
        assert set(Grade(True, "45", "why").as_dict()) == {"correct", "parsed", "reason"}
