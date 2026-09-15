"""The tools a graph step can call, and the refusal for every one Tokop has not implemented.

A tool step is the part of an agent that is not a model call. It spends nothing, which makes it
easy to add and easy to leave in, and the interesting question about one is never what it costs
but what it puts into the next step's prompt. That is the number `tokop graph` prints beside it.

The retriever here is deliberately a program and deliberately fallible. A retriever that could
not miss would make the agent look better than any real one, and the milestone's comparison
would be decided by the fixture rather than by the arithmetic.
"""

from __future__ import annotations

import json

import pytest

from tokop.paths import repo_root
from tokop.workloads.tools import (
    DEFAULT_SECTIONS,
    ToolContext,
    ToolError,
    get_tool,
    grounding_lookup,
    register_tool,
    registered_tools,
    significant_words,
    split_sections,
    stem,
)

CATALOGUE = repo_root() / "data/catalogue-agent/catalogue.md"


@pytest.fixture(scope="module")
def catalogue() -> str:
    return CATALOGUE.read_text()


def look_up(question: str, document: str) -> str:
    return grounding_lookup(question, ToolContext(grounding=document, question=question))


class TestTheRegistry:
    def test_a_tool_nobody_implemented_is_refused_and_the_message_names_the_ones_that_exist(
        self,
    ) -> None:
        with pytest.raises(ToolError) as caught:
            get_tool("vector-search-v9")
        assert "grounding-lookup-v1" in str(caught.value)
        assert "does not stand in" in str(caught.value)

    def test_registering_the_same_name_twice_is_refused(self) -> None:
        tool = get_tool("grounding-lookup-v1")
        with pytest.raises(ToolError, match="already registered"):
            register_tool(tool)

    def test_the_build_ships_exactly_what_it_says_it_ships(self) -> None:
        assert registered_tools() == ("grounding-lookup-v1",)


class TestSplittingADocument:
    def test_a_document_with_no_headings_is_one_section(self) -> None:
        """Nothing in it says where one part ends, so there is nothing to choose between."""
        assert split_sections("just some prose\n\nand more of it") == [
            "just some prose\n\nand more of it"
        ]

    def test_an_empty_document_has_no_sections(self) -> None:
        assert split_sections("   \n\n  ") == []

    def test_text_before_the_first_heading_is_kept(self) -> None:
        sections = split_sections("preamble text\n\n## one\n\nbody\n\n## two\n\nbody")
        assert len(sections) == 3
        assert sections[0].startswith("preamble")

    def test_the_catalogue_splits_into_its_entries(self, catalogue: str) -> None:
        sections = split_sections(catalogue)
        headings = [section.splitlines()[0] for section in sections]
        assert sum(1 for h in headings if h.startswith("## service:")) == 48
        assert "## Change freeze" in headings


class TestScoring:
    def test_a_plural_and_its_singular_are_the_same_word(self) -> None:
        """An entry says "Pages outside working hours" and a question asks "does it page"."""
        assert stem("pages") == "page"
        assert stem("hours") == "hour"
        assert stem("is") == "is", "too short to be a plural"
        assert stem("across") == "across", "a double s is not a plural"

    def test_words_every_question_carries_are_dropped(self) -> None:
        assert significant_words("which team owns the thing") == ["team", "own", "thing"]

    def test_a_rare_word_outweighs_a_common_one(self) -> None:
        """The whole reason scoring is inverse document frequency and not a count.

        The section that is *about* the query shares one rare word with it; three others share
        two common ones. Counting shared words puts the right section last.
        """
        document = (
            "## alpha\n\nalert page hours\n\n"
            "## beta\n\nalert page hours\n\n"
            "## gamma\n\nalert page hours\n\n"
            "## delta\n\nzarquon\n"
        )
        assert look_up("zarquon alert page hours", document).startswith("## delta")

    def test_a_query_that_matches_nothing_returns_nothing(self) -> None:
        """A retriever that always returns something is one whose failures are invisible."""
        assert look_up("zarquon", "## one\n\nnothing relevant here\n") == ""

    def test_a_query_of_nothing_but_common_words_returns_nothing(self) -> None:
        assert look_up("what is it", "## one\n\nnothing relevant here\n") == ""


class TestOnTheCatalogue:
    def test_it_returns_a_small_fraction_of_the_document(self, catalogue: str) -> None:
        """If retrieval returned most of the document there would be nothing to compare."""
        got = look_up("Which team owns payments-gateway?", catalogue)
        assert 0 < len(got) < len(catalogue) / 4

    def test_it_returns_the_number_of_sections_it_says_it_returns(self, catalogue: str) -> None:
        got = look_up("Which team owns the audit-writer service?", catalogue)
        assert len(split_sections(got)) == DEFAULT_SECTIONS

    def test_it_finds_the_governing_section_for_most_tasks_and_not_all_of_them(
        self, catalogue: str
    ) -> None:
        """The measured miss rate, pinned so it cannot quietly become zero or one.

        This is the number the agent's accuracy rests on: a model cannot read a section it was
        not given. It is a property of this retriever on this document, and it is reported as
        such rather than described as retrieval working.
        """
        rows = [
            json.loads(line)
            for line in (repo_root() / "data/catalogue-agent/dataset.jsonl")
            .read_text()
            .splitlines()
            if line.strip()
        ]
        found = sum(
            1
            for row in rows
            if all(section in look_up(row["question"], catalogue) for section in row["sections"])
        )
        assert 0.80 <= found / len(rows) <= 0.95, f"{found} of {len(rows)}"

    def test_it_is_deterministic(self, catalogue: str) -> None:
        """A recording made against a retriever that drifts is a recording of nothing."""
        question = (
            "A change freeze is in effect. Does an alert on audit-writer raised at 03:00 "
            "on a Sunday page?"
        )
        assert look_up(question, catalogue) == look_up(question, catalogue)

    def test_it_never_sees_the_answer(self) -> None:
        """A retriever handed the gold would fetch the section containing it every time."""
        assert "gold" not in ToolContext.__dataclass_fields__
        assert set(ToolContext.__dataclass_fields__) == {"grounding", "question"}
