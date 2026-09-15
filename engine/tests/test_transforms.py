"""Deterministic prompt rewrites (SPEC.md 5.2).

The property that matters most: a safe fix must never change what the prompt *means*. Two of
them make it longer on purpose, and the tests assert that the arithmetic says so rather than
hiding it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tokop.adapters.base import Block, LLMRequest, Message, blocks_of
from tokop.core.pricing import ModelPrice, PriceProvenance
from tokop.optimize.lint import LintContext, clear_score, lint
from tokop.optimize.transforms import (
    OUTPUT_CONTRACT,
    add_breakpoint_after_static,
    add_output_contract,
    apply_safe_fixes,
    move_volatile_after_breakpoint,
    remove_duplicate_instructions,
    reorder_static_first,
    strip_courtesy,
)

PRICE = ModelPrice(
    model_id="claude-opus-5",
    provider="anthropic",
    input=Decimal("5"),
    output=Decimal("25"),
    cache_write_5m=Decimal("6.25"),
    cache_read=Decimal("0.50"),
    min_cacheable_tokens=512,
    provenance=PriceProvenance(
        source_url="https://platform.claude.com/docs/en/about-claude/pricing",
        retrieved=date(2026, 9, 11),
        verified=True,
    ),
)

DOCUMENT = (
    "The returns policy sets out the window for each product category, the restocking fee "
    "charged on a non-defective return, and the shipping fee table by zone and weight band. "
) * 60


def request_of(system: list[Block], user: list[Block], max_tokens: int = 2000) -> LLMRequest:
    return LLMRequest(
        provider="anthropic",
        model="claude-opus-5",
        system=system,
        messages=[Message(role="user", blocks=user)],
        max_tokens=max_tokens,
    )


class TestIndividualTransforms:
    def test_volatile_content_moves_behind_the_breakpoint(self) -> None:
        request = request_of(
            blocks_of("Current date and time: 2026-09-11 09:00 UTC", DOCUMENT, cache_last="5m"),
            [Block(text="question")],
        )
        result = move_volatile_after_breakpoint(request)
        assert result.applied
        # The breakpoint now sits before the timestamp, so the document is cacheable.
        assert result.request.static_prefix_text
        assert "Current date" not in result.request.static_prefix_text
        # The prefix gets slightly *smaller* — the timestamp left it — and infinitely more
        # useful, because it can now be read instead of rewritten on every call. The saving is
        # measured from the whole stable prefix, not from the size delta.
        assert result.prefix_was_volatile
        assert result.savings_per_1k(PRICE)["cache_usd"] > 0
        # Nothing was deleted: the timestamp is still in the prompt.
        assert "Current date and time" in result.request.system_text

    def test_a_document_after_the_question_moves_into_the_system_prompt(self) -> None:
        request = request_of(
            [Block(text="You answer from the policy.")],
            [Block(text="Question: how many days?"), Block(text=DOCUMENT)],
        )
        result = reorder_static_first(request)
        assert result.applied
        assert DOCUMENT in result.request.static_prefix_text
        assert result.cacheable_before == 0
        assert result.cacheable_after > 1000
        # The question survives, in the user message.
        assert "how many days" in result.request.messages[0].text

    def test_a_breakpoint_lands_on_the_last_stable_block(self) -> None:
        request = request_of(
            [
                Block(text="You answer from the policy."),
                Block(text=DOCUMENT),
                Block(text="Current date and time: 2026-09-11 09:00 UTC"),
            ],
            [Block(text="q")],
        )
        result = add_breakpoint_after_static(request)
        assert result.applied
        assert result.request.system[1].cache == "5m"
        assert result.request.system[2].cache is None

    def test_no_breakpoint_is_added_when_everything_varies(self) -> None:
        request = request_of(
            [Block(text="Current date and time: 2026-09-11 09:00 UTC")], [Block(text="q")]
        )
        result = add_breakpoint_after_static(request)
        assert not result.applied
        assert "nothing to cache" in result.note

    def test_courtesy_is_removed_without_touching_the_instruction(self) -> None:
        request = request_of(
            [
                Block(
                    text=(
                        "Thank you for your hard work! Always cite the section you used. "
                        "Please double-check any arithmetic."
                    )
                )
            ],
            [Block(text="q")],
        )
        result = strip_courtesy(request)
        assert result.applied
        text = result.request.system_text
        assert "Always cite the section you used." in text
        assert "double-check any arithmetic" in text
        assert "Thank you" not in text
        assert result.input_delta < 0

    def test_duplicate_instructions_are_removed_keeping_the_first(self) -> None:
        request = request_of(
            [
                Block(
                    text=(
                        "- Always cite the section of the handbook that your answer comes from.\n"
                        "- Do not invent policy; use only the material provided below here.\n"
                        "- Always cite the section of the handbook that your answer comes from.\n"
                    )
                )
            ],
            [Block(text="q")],
        )
        result = remove_duplicate_instructions(request)
        assert result.applied
        assert result.request.system_text.count("Always cite the section") == 1
        assert "Do not invent policy" in result.request.system_text

    def test_a_paraphrase_is_left_alone(self) -> None:
        """Deciding two differently worded rules mean the same thing is a judgement, not a
        deterministic rewrite (DECISIONS.md D18)."""
        request = request_of(
            [
                Block(
                    text=(
                        "- Always cite the section of the handbook that your answer comes from.\n"
                        "- Make sure you mention which part of the handbook you used.\n"
                    )
                )
            ],
            [Block(text="q")],
        )
        result = remove_duplicate_instructions(request)
        assert not result.applied
        assert "no lexically duplicated instruction" in result.note

    def test_the_output_contract_makes_the_prompt_longer_and_says_so(self) -> None:
        request = request_of([Block(text="Answer the question.")], [Block(text="q")], 2000)
        result = add_output_contract(request)
        assert result.applied
        assert result.input_delta > 0, "adding a contract costs input tokens"
        assert result.max_tokens_after == 200
        assert OUTPUT_CONTRACT.splitlines()[0] in result.request.system_text
        savings = result.savings_per_1k(PRICE)
        # (2000 - 200) tok x $25/Mtok x 1,000 = $45.00 of output headroom removed.
        assert savings["output_usd_upper_bound"] == Decimal("45.000")
        assert savings["input_cost_usd"] > 0
        assert savings["net_usd"] > 0

    def test_an_existing_contract_is_not_added_twice(self) -> None:
        request = request_of([Block(text="Answer.")], [Block(text="q")])
        once = add_output_contract(request)
        twice = add_output_contract(once.request)
        assert not twice.applied
        assert "already present" in twice.note


class TestApplySafeFixes:
    @pytest.fixture
    def b0(self):
        from tokop.core.registry import load_registry
        from tokop.paths import repo_root
        from tokop.workloads.demo.handbook import render_handbook
        from tokop.workloads.demo.policy import load_policy
        from tokop.workloads.spec import demo_timestamp, load_workload

        registry = load_registry()
        workload = load_workload(repo_root() / "data/demo/workload.yaml")
        return workload.pipeline("B0").render(
            "anthropic",
            registry.roles["frontier"],
            {
                "grounding": render_handbook(load_policy()),
                "question": "How many days to return a backpack?",
                "timestamp": demo_timestamp(),
            },
        )

    def test_b0_becomes_cacheable_and_capped(self, b0) -> None:
        report = apply_safe_fixes(b0)
        assert report.cacheable_before == 0
        assert report.cacheable_after > 5000
        assert report.max_tokens_after == 200
        assert report.max_tokens_before == 2000

    def test_the_net_input_delta_is_reported_honestly(self, b0) -> None:
        """Removing filler and adding a contract pull in opposite directions. The report shows
        the net, not the flattering half."""
        report = apply_safe_fixes(b0)
        assert report.input_delta > 0, "the contract costs more than the filler saved"
        contract = next(s for s in report.applied if s.transform == "add_output_contract")
        courtesy = next(s for s in report.applied if s.transform == "strip_courtesy")
        assert contract.input_delta > 0
        assert courtesy.input_delta < 0

    def test_the_saving_is_dominated_by_the_cache(self, b0) -> None:
        report = apply_safe_fixes(b0)
        savings = report.savings_per_1k(PRICE)
        assert savings["cache_usd"] > 0
        assert savings["net_usd"] > savings["output_usd_upper_bound"]

    def test_the_lint_agrees_the_prompt_improved(self, b0) -> None:
        context = LintContext(price=PRICE, min_cacheable_tokens=512)
        report = apply_safe_fixes(b0)
        before = sum(clear_score(lint(b0, context)).values())
        after = sum(clear_score(lint(report.after, context)).values())
        assert after > before
        # The cache findings are gone; what is left needs human judgement.
        remaining = {f.id for f in lint(report.after, context)}
        assert not (remaining & {"PL01", "PL02", "PL05"})

    def test_every_skipped_fix_says_why(self, b0) -> None:
        report = apply_safe_fixes(b0)
        for step in report.steps:
            if not step.applied:
                assert step.note, step.transform

    def test_applying_twice_changes_nothing_further(self, b0) -> None:
        once = apply_safe_fixes(b0)
        twice = apply_safe_fixes(once.after)
        assert twice.input_delta == 0
        assert twice.cacheable_after == once.cacheable_after
        assert not twice.applied

    def test_the_question_and_the_document_both_survive(self, b0) -> None:
        report = apply_safe_fixes(b0)
        whole = report.after.system_text + "\n" + "\n".join(m.text for m in report.after.messages)
        assert "How many days to return a backpack?" in whole
        assert "Northwind Ridge Outfitters support handbook" in whole


class TestExtras:
    """The recorded Compare and Brief examples (SPEC.md section 6 step 6)."""

    @pytest.fixture(scope="class")
    @staticmethod
    def extras():
        from tokop.extras import load_extras
        from tokop.paths import fixtures_dir

        loaded = load_extras(fixtures_dir() / "test" / "extras.json")
        if loaded is None:
            pytest.skip("extras have not been built")
        return loaded

    def test_two_compare_examples_one_reasoning_one_extraction(self, extras) -> None:
        ids = {c["id"] for c in extras.compares}
        assert ids == {"reasoning", "extraction"}

    def test_every_column_has_tokens_a_cost_and_a_time_to_first_token(self, extras) -> None:
        for example in extras.compares:
            assert len(example["columns"]) >= 3
            for column in example["columns"]:
                assert column["output_tokens"] > 0
                assert Decimal(column["cost_usd"]) > 0
                assert column["ttft_ms"] is not None
                assert column["origin"] == "simulated"

    def test_the_cheapest_model_really_is_cheapest(self, extras) -> None:
        for example in extras.compares:
            costs = {c["model_id"]: Decimal(c["cost_usd"]) for c in example["columns"]}
            assert costs["claude-haiku-4-5-20251001"] < costs["claude-opus-5"]

    def test_the_synthesis_has_the_fixed_structure(self, extras) -> None:
        reasoning = next(c for c in extras.compares if c["id"] == "reasoning")
        synthesis = reasoning["synthesis"]
        assert set(synthesis) == {
            "agreement",
            "disagreement",
            "unique",
            "likely_errors",
            "final",
        }

    def test_the_brief_is_smaller_than_its_source_and_is_labelled_lossy(self, extras) -> None:
        brief = extras.brief
        assert brief["tokens_after"] < brief["tokens_before"]
        assert brief["reduction"] > 0.3
        assert "lossy" in brief["lossy_note"].lower()
        assert "never automates a chat app" in brief["purpose_note"]
        assert Decimal(brief["cost_usd"]) > 0

    def test_the_brief_uses_a_cheap_model(self, extras) -> None:
        from tokop.core.registry import load_registry

        assert extras.brief["model_id"] == load_registry().roles["cheap"]
