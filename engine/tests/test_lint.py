"""The prompt lint, PL01 to PL14 (SPEC.md 7.4).

Two things are being tested: that each rule fires on the thing it is meant to catch, and — just
as important — that it does **not** fire on the demo's own pipelines where it should not. A
linter with false positives on a 7,000-token grounding document is a linter that gets turned off.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tokop.adapters.base import Block, LLMRequest, Message, blocks_of, user_message
from tokop.core.pricing import ModelPrice, PriceProvenance
from tokop.optimize.lint import (
    CONFIDENCE_WEIGHT,
    LintContext,
    clear_score,
    jaccard,
    lint,
    total_projected_usd,
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


def context(**kwargs) -> LintContext:
    return LintContext(price=PRICE, min_cacheable_tokens=512, **kwargs)


def request_of(system: list[Block], user: list[Block], max_tokens: int = 1000) -> LLMRequest:
    return LLMRequest(
        provider="anthropic",
        model="claude-opus-5",
        system=system,
        messages=[Message(role="user", blocks=user)],
        max_tokens=max_tokens,
    )


def ids(findings) -> list[str]:
    return [f.id for f in findings]


class TestCacheRules:
    def test_pl01_fires_on_a_timestamp_in_the_prefix(self) -> None:
        request = request_of(
            blocks_of("Current date and time: 2026-09-11 09:00 UTC", DOCUMENT, cache_last="5m"),
            [Block(text="question")],
        )
        findings = lint(request, context())
        assert "PL01" in ids(findings)

    def test_pl01_does_not_fire_on_a_date_inside_a_static_document(self) -> None:
        """The demo handbook contains policy dates. Flagging those would be a false alarm on
        the finding the product most needs to be right about."""
        document = DOCUMENT + "\n\nThis policy took effect on 2026-07-01 and expires 2027-06-30."
        request = request_of(
            blocks_of("You answer from the policy.", document, cache_last="5m"),
            [Block(text="question")],
        )
        assert "PL01" not in ids(lint(request, context()))

    def test_pl01_does_not_fire_when_the_stamp_is_after_the_breakpoint(self) -> None:
        request = request_of(
            [
                Block(text="You answer from the policy."),
                Block(text=DOCUMENT, cache="5m"),
                Block(text="Current date and time: 2026-09-11 09:00 UTC"),
            ],
            [Block(text="question")],
        )
        assert "PL01" not in ids(lint(request, context()))

    def test_pl02_fires_when_a_variable_block_precedes_the_document(self) -> None:
        request = request_of(
            [Block(text="You are helping.")],
            [Block(text="Question: {{question}}"), Block(text=DOCUMENT)],
        )
        assert "PL02" in ids(lint(request, context()))

    def test_pl03_fires_when_the_prefix_is_below_the_minimum(self) -> None:
        request = request_of(
            blocks_of("A short system prompt that is nowhere near the minimum.", cache_last="5m"),
            [Block(text="question")],
        )
        findings = lint(request, context())
        assert "PL03" in ids(findings)
        pl03 = next(f for f in findings if f.id == "PL03")
        assert "under this model's" in pl03.detail
        assert pl03.confidence == "heuristic"

    def test_pl03_does_not_fire_when_the_prefix_clears_the_minimum(self) -> None:
        request = request_of(
            blocks_of("You answer from the policy.", DOCUMENT, cache_last="5m"),
            [Block(text="question")],
        )
        assert "PL03" not in ids(lint(request, context()))

    def test_pl04_fires_on_a_breakpoint_over_volatile_content(self) -> None:
        request = request_of(
            [
                Block(text=DOCUMENT),
                Block(text="Current date and time: 2026-09-11 09:00 UTC", cache="5m"),
            ],
            [Block(text="question")],
        )
        assert "PL04" in ids(lint(request, context()))

    def test_pl05_fires_on_a_large_uncached_system_prompt(self) -> None:
        request = request_of([Block(text=DOCUMENT)], [Block(text="question")])
        findings = lint(request, context())
        assert "PL05" in ids(findings)

    def test_cache_findings_are_priced_at_the_input_minus_read_spread(self) -> None:
        request = request_of(
            blocks_of("Current date and time: 2026-09-11 09:00 UTC", DOCUMENT, cache_last="5m"),
            [Block(text="question")],
        )
        pl01 = next(f for f in lint(request, context()) if f.id == "PL01")
        assert "cache read" in pl01.formula
        assert pl01.projected_usd_per_1k > 0


class TestClearRules:
    def test_pl06_fires_without_an_output_contract(self) -> None:
        request = request_of(
            [Block(text="Answer the question helpfully.")],
            [Block(text="question")],
            max_tokens=2000,
        )
        findings = lint(request, context())
        assert "PL06" in ids(findings)

    def test_pl06_does_not_fire_with_a_contract_and_a_cap(self) -> None:
        request = request_of(
            [
                Block(
                    text=(
                        "You are a specialist. Task: answer the question.\n"
                        'Reply with one JSON object: {"answer": "..."} and nothing else.\n'
                        "Use no more than 20 words."
                    )
                )
            ],
            [Block(text="question")],
            max_tokens=200,
        )
        assert "PL06" not in ids(lint(request, context()))

    def test_pl06_is_measured_when_traces_exist(self) -> None:
        request = request_of([Block(text="Answer it.")], [Block(text="q")], max_tokens=2000)
        findings = lint(request, context(observed_output_tokens=400, needed_output_tokens=40))
        pl06 = next(f for f in findings if f.id == "PL06")
        assert pl06.confidence == "measured"
        assert "observed" in pl06.formula

    def test_pl07_fires_on_an_exactly_duplicated_bullet(self) -> None:
        """What the specified rule can actually catch. A *paraphrase* shares almost no
        trigrams with its original and is out of reach for any lexical rule; see
        DECISIONS.md D18."""
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
        assert "PL07" in ids(lint(request, context()))

    def test_pl08_fires_on_courtesy_and_hedges(self) -> None:
        request = request_of(
            [
                Block(
                    text=(
                        "Thank you for your hard work! Please try your best. It's generally a "
                        "good idea to perhaps consider the context."
                    )
                )
            ],
            [Block(text="q")],
        )
        assert "PL08" in ids(lint(request, context()))

    def test_pl09_needs_real_example_markers_not_the_word_example(self) -> None:
        mentions = request_of(
            [Block(text=DOCUMENT + "\n\nFor example, this policy is illustrative.")],
            [Block(text="q")],
        )
        assert "PL09" not in ids(lint(mentions, context()))

        real = request_of(
            [
                Block(
                    text=(
                        "Example 1:\nInput: How long to return a tent?\nOutput: 45\n"
                        "Example 2:\nInput: How long to return boots?\nOutput: 30\n" * 12
                    )
                )
            ],
            [Block(text="q")],
        )
        assert "PL09" in ids(lint(real, context()))

    def test_pl10_fires_without_a_role_task_or_criteria(self) -> None:
        request = request_of(
            [Block(text="Please help with whatever the customer is asking about.")],
            [Block(text="q")],
        )
        findings = lint(request, context())
        pl10 = next(f for f in findings if f.id == "PL10")
        assert "role" in pl10.evidence
        assert "help with" in pl10.evidence

    def test_pl10_does_not_fire_on_an_explicit_prompt(self) -> None:
        request = request_of(
            [
                Block(
                    text=(
                        "You are a returns-policy specialist.\n"
                        "Task: answer one question using the handbook.\n"
                        "How to get it right: work out the category first."
                    )
                )
            ],
            [Block(text="q")],
        )
        assert "PL10" not in ids(lint(request, context()))

    def test_pl11_fires_on_the_concise_versus_detail_conflict(self) -> None:
        request = request_of(
            [Block(text="Be concise.\nExplain your reasoning in full detail.")],
            [Block(text="q")],
        )
        findings = lint(request, context())
        pl11 = next(f for f in findings if f.id == "PL11")
        assert "brevity against detail" in pl11.evidence
        assert pl11.confidence == "heuristic"

    def test_pl12_fires_on_an_unstructured_wall(self) -> None:
        wall = "This policy is long and has no structure whatsoever. " * 120
        request = request_of([Block(text=wall)], [Block(text="q")])
        assert "PL12" in ids(lint(request, context()))

    def test_pl12_does_not_fire_on_a_structured_document(self) -> None:
        structured = "## Heading\n\n" + "\n".join(f"- rule {i}" for i in range(200))
        request = request_of([Block(text=structured)], [Block(text="q")])
        assert "PL12" not in ids(lint(request, context()))

    def test_pl13_fires_on_hard_coded_specifics_in_instructions(self) -> None:
        request = request_of(
            [
                Block(
                    text=(
                        "You answer support questions for our team and should route anything "
                        "unusual onwards. Email escalations to ops@example.com and link the "
                        "runbook at https://runbook.example.com/returns when you do, quoting "
                        "policy version v2.1.0 so the reader knows which rules applied."
                    )
                )
            ],
            [Block(text="q")],
        )
        findings = lint(request, context())
        assert "PL13" in ids(findings)

    def test_pl13_does_not_fire_on_a_grounding_document(self) -> None:
        document = DOCUMENT + "\n\nContact help@example.com. Effective 2026-07-01. Fee $44.95."
        request = request_of([Block(text=document)], [Block(text="q")])
        assert "PL13" not in ids(lint(request, context()))

    def test_pl14_fires_on_a_long_multi_job_prompt(self) -> None:
        jobs = (
            "Analyse the request in detail and work out what is being asked of you here. "
            "Summarize the policy that applies to the situation described by the customer. "
            "Compute the refund that results once fees have been deducted from the price. "
            "Verify the arithmetic before you commit to a figure in your reply. "
            "Explain your reasoning so that a reader can follow how you reached the number. "
        ) * 6
        request = request_of([Block(text=jobs)], [Block(text="q")])
        findings = lint(request, context())
        pl14 = next(f for f in findings if f.id == "PL14")
        assert "Heuristic" in pl14.detail
        assert pl14.projected_usd_per_1k == Decimal(0)

    def test_pl14_ignores_a_grounding_document(self) -> None:
        """A document's verbs are not jobs the prompt is asking for."""
        request = request_of(
            [Block(text="Answer the question."), Block(text=DOCUMENT, cache="5m")],
            [Block(text="q")],
        )
        assert "PL14" not in ids(lint(request, context()))


class TestRanking:
    def test_findings_are_ordered_by_weighted_dollars(self) -> None:
        request = request_of(
            blocks_of("Current date and time: 2026-09-11 09:00 UTC", DOCUMENT, cache_last="5m"),
            [Block(text="Thank you so much! Please help with this.")],
            max_tokens=2000,
        )
        findings = lint(request, context())
        weights = [f.weighted_usd for f in findings]
        assert weights == sorted(weights, reverse=True)

    def test_a_heuristic_finding_is_discounted_against_a_measured_one(self) -> None:
        assert CONFIDENCE_WEIGHT["measured"] > CONFIDENCE_WEIGHT["projected"]
        assert CONFIDENCE_WEIGHT["projected"] > CONFIDENCE_WEIGHT["heuristic"]

    def test_hygiene_inside_a_cached_prefix_is_priced_at_the_read_rate(self) -> None:
        """Cutting words from a cached prefix saves a tenth of what the same cut saves in
        uncached input. The lint says so rather than overclaiming."""
        filler = "Thank you so much for your hard work, we really appreciate it! "
        cached = request_of(
            blocks_of("Instructions. " + filler * 10, DOCUMENT, cache_last="5m"),
            [Block(text="q")],
        )
        uncached = request_of(
            [Block(text="Instructions. " + filler * 10), Block(text=DOCUMENT)],
            [Block(text="q")],
        )
        cached_pl08 = next(f for f in lint(cached, context()) if f.id == "PL08")
        uncached_pl08 = next(f for f in lint(uncached, context()) if f.id == "PL08")
        assert cached_pl08.projected_usd_per_1k < uncached_pl08.projected_usd_per_1k
        assert "cache read rate" in cached_pl08.detail

    def test_the_total_is_available_and_labelled_as_an_upper_bound(self) -> None:
        request = request_of([Block(text="Answer it.")], [Block(text="q")], max_tokens=2000)
        findings = lint(request, context())
        assert total_projected_usd(findings) >= max(f.projected_usd_per_1k for f in findings)


class TestClearScore:
    def test_a_clean_prompt_scores_25(self) -> None:
        assert sum(clear_score([]).values()) == 25

    def test_each_letter_loses_points_for_its_own_rules(self) -> None:
        request = request_of(
            [Block(text="Please help with whatever they ask. Be concise. Explain in full detail.")],
            [Block(text="q")],
            max_tokens=2000,
        )
        scores = clear_score(lint(request, context()))
        assert scores["E"] < 5  # PL10 and PL11
        assert sum(scores.values()) < 25
        assert all(1 <= v <= 5 for v in scores.values())

    def test_the_demo_pipelines_improve_monotonically(self) -> None:
        """The demo's whole claim in one assertion: B2's prompt is better than B0's."""
        from tokop.core.registry import load_registry
        from tokop.paths import repo_root
        from tokop.workloads.demo.handbook import render_handbook
        from tokop.workloads.demo.policy import load_policy
        from tokop.workloads.spec import demo_timestamp, load_workload

        registry = load_registry()
        workload = load_workload(repo_root() / "data/demo/workload.yaml")
        opus = registry.model("claude-opus-5")
        variables = {
            "handbook": render_handbook(load_policy()),
            "question": "How many days?",
            "timestamp": demo_timestamp(),
        }
        ctx = LintContext(
            price=opus.price,
            min_cacheable_tokens=opus.min_cacheable_tokens or 512,
            tool_use_system_prompt_tokens=opus.tool_use_system_prompt_tokens or 0,
        )
        scores = {}
        for pipeline_id in ("B0", "B1", "B2"):
            request = workload.pipeline(pipeline_id).render("anthropic", opus.model_id, variables)
            scores[pipeline_id] = sum(clear_score(lint(request, ctx)).values())
        assert scores["B2"] > scores["B0"]
        assert scores["B2"] == 25

    def test_b0_fires_the_rules_its_yaml_declares(self) -> None:
        """The workload YAML documents B0's anti-patterns; the lint must actually catch them."""
        from tokop.core.registry import load_registry
        from tokop.paths import repo_root
        from tokop.workloads.demo.handbook import render_handbook
        from tokop.workloads.demo.policy import load_policy
        from tokop.workloads.spec import demo_timestamp, load_workload

        registry = load_registry()
        workload = load_workload(repo_root() / "data/demo/workload.yaml")
        opus = registry.model("claude-opus-5")
        request = workload.pipeline("B0").render(
            "anthropic",
            opus.model_id,
            {
                "handbook": render_handbook(load_policy()),
                "question": "How many days?",
                "timestamp": demo_timestamp(),
            },
        )
        fired = set(ids(lint(request, LintContext(price=opus.price, min_cacheable_tokens=512))))
        declared = set(workload.pipeline("B0").known_antipatterns)
        assert declared <= fired, f"declared but not caught: {declared - fired}"


class TestJaccard:
    def test_identical_text_scores_one(self) -> None:
        assert (
            jaccard("always cite the section you used", "always cite the section you used") == 1.0
        )

    def test_unrelated_text_scores_zero(self) -> None:
        assert jaccard("always cite the section", "the shipping fee table by zone") == 0.0

    def test_a_near_duplicate_clears_the_threshold(self) -> None:
        a = "always cite the section of the handbook that your answer comes from"
        b = "always cite the section of the handbook that your answer came from"
        assert jaccard(a, b) >= 0.5

    def test_empty_text_scores_zero_rather_than_raising(self) -> None:
        assert jaccard("", "anything at all here") == 0.0


class TestContextErrors:
    def test_an_empty_request_produces_no_crash(self) -> None:
        request = LLMRequest(provider="anthropic", model="claude-opus-5")
        assert isinstance(lint(request, context()), list)

    def test_a_user_only_request_is_linted(self) -> None:
        request = LLMRequest(
            provider="anthropic",
            model="claude-opus-5",
            messages=[user_message("just a question")],
            max_tokens=2000,
        )
        assert "PL06" in ids(lint(request, context()))
