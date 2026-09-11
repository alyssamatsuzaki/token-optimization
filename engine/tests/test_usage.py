"""Every trap in SPEC.md 7.2, with the arithmetic worked out by hand in the test.

Payload shapes come from provider documentation examples; recorded payloads replace them as
fixtures arrive (see tests/test_fixtures_usage.py).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tokop.core.usage import (
    TokenUsage,
    UsageMappingError,
    normalize,
    normalize_anthropic,
    normalize_deepseek,
    normalize_openai,
    normalize_openrouter,
)


class TestAnthropicTrap:
    """`input_tokens` counts only the tokens after the last cache breakpoint."""

    def test_total_input_is_the_sum_of_three_fields(self) -> None:
        raw = {
            "input_tokens": 42,
            "cache_creation_input_tokens": 7102,
            "cache_read_input_tokens": 0,
            "output_tokens": 118,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 7102,
                "ephemeral_1h_input_tokens": 0,
            },
        }
        u = normalize_anthropic(raw)
        # By hand: 42 after the breakpoint + 7,102 written + 0 read = 7,144 input tokens.
        assert u.input_uncached == 42
        assert u.cache_write_5m == 7102
        assert u.total_input == 7144
        assert u.total_output == 118
        assert u.total == 7262
        # Taking input_tokens as the total would have undercounted by the whole prefix.
        assert u.total_input - u.input_uncached == 7102

    def test_cache_read_request_counts_the_prefix(self) -> None:
        raw = {
            "input_tokens": 42,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 7102,
            "output_tokens": 118,
        }
        u = normalize_anthropic(raw)
        assert u.cache_read == 7102
        assert u.total_input == 7144
        assert u.sources["cache_read"] == "provider"

    def test_lifetime_split_is_read_from_cache_creation(self) -> None:
        raw = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 1500,
            "cache_read_input_tokens": 0,
            "output_tokens": 5,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 500,
                "ephemeral_1h_input_tokens": 1000,
            },
        }
        u = normalize_anthropic(raw)
        assert (u.cache_write_5m, u.cache_write_1h) == (500, 1000)
        assert u.sources["cache_write_5m"] == "provider"
        assert u.sources["cache_write_1h"] == "provider"

    def test_missing_split_is_attributed_to_5m_and_marked_estimated(self) -> None:
        raw = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 1500,
            "cache_read_input_tokens": 0,
            "output_tokens": 5,
        }
        u = normalize_anthropic(raw)
        assert u.cache_write_5m == 1500
        assert u.sources["cache_write_5m"] == "estimated"
        assert u.source == "mixed"

    def test_inconsistent_split_raises_rather_than_guessing(self) -> None:
        raw = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 1500,
            "output_tokens": 5,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 400,
                "ephemeral_1h_input_tokens": 200,
            },
        }
        with pytest.raises(UsageMappingError, match="do not sum"):
            normalize_anthropic(raw)

    def test_below_the_minimum_prefix_both_cache_fields_read_zero(self) -> None:
        """Appendix B: below the minimum the request runs uncached and returns no error."""
        raw = {
            "input_tokens": 300,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 40,
        }
        u = normalize_anthropic(raw)
        assert (u.cache_write_5m, u.cache_write_1h, u.cache_read) == (0, 0, 0)
        assert u.total_input == 300


class TestOpenAITrap:
    """`prompt_tokens` already includes cached; `completion_tokens` already includes reasoning."""

    def test_cached_is_subtracted_not_added(self) -> None:
        raw = {
            "prompt_tokens": 2000,
            "completion_tokens": 300,
            "total_tokens": 2300,
            "prompt_tokens_details": {"cached_tokens": 1792},
            "completion_tokens_details": {"reasoning_tokens": 256},
        }
        u = normalize_openai(raw)
        # By hand: 2,000 − 1,792 = 208 uncached; 300 − 256 = 44 visible.
        assert u.input_uncached == 208
        assert u.cache_read == 1792
        assert u.output_visible == 44
        assert u.output_reasoning == 256
        # Adding instead of subtracting would have claimed 3,792 input tokens.
        assert u.total_input == 2000
        assert u.total_output == 300

    def test_no_write_bucket_is_invented_for_automatic_caching(self) -> None:
        raw = {
            "prompt_tokens": 2000,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 1024},
        }
        u = normalize_openai(raw)
        assert u.cache_write_5m == 0
        assert u.cache_write_1h == 0

    def test_missing_details_objects_are_all_uncached_and_visible(self) -> None:
        u = normalize_openai({"prompt_tokens": 120, "completion_tokens": 30})
        assert (u.input_uncached, u.cache_read) == (120, 0)
        assert (u.output_visible, u.output_reasoning) == (30, 0)

    def test_cached_exceeding_prompt_raises(self) -> None:
        raw = {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 200},
        }
        with pytest.raises(UsageMappingError, match="negative"):
            normalize_openai(raw)


class TestDeepSeekTrap:
    """DeepSeek partitions prompt_tokens into hit/miss instead of OpenAI's cached_tokens."""

    def test_hit_and_miss_partition_the_prompt(self) -> None:
        raw = {
            "prompt_tokens": 3000,
            "prompt_cache_hit_tokens": 2816,
            "prompt_cache_miss_tokens": 184,
            "completion_tokens": 90,
        }
        u = normalize_deepseek(raw)
        assert u.cache_read == 2816
        assert u.input_uncached == 184
        assert u.total_input == 3000
        assert u.output_visible == 90

    def test_partition_that_does_not_sum_raises(self) -> None:
        raw = {
            "prompt_tokens": 3000,
            "prompt_cache_hit_tokens": 2000,
            "prompt_cache_miss_tokens": 500,
            "completion_tokens": 10,
        }
        with pytest.raises(UsageMappingError, match="do not sum"):
            normalize_deepseek(raw)

    def test_openai_shaped_payload_falls_through_to_the_openai_mapping(self) -> None:
        raw = {
            "prompt_tokens": 500,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 128},
        }
        u = normalize_deepseek(raw)
        assert (u.input_uncached, u.cache_read) == (372, 128)


class TestOpenRouterCost:
    def test_authoritative_cost_is_returned_separately(self) -> None:
        raw = {"prompt_tokens": 100, "completion_tokens": 20, "cost": "0.00042"}
        usage, cost = normalize_openrouter(raw)
        assert usage.total == 120
        assert cost == Decimal("0.00042")

    def test_absent_cost_is_none_not_zero(self) -> None:
        _, cost = normalize_openrouter({"prompt_tokens": 100, "completion_tokens": 20})
        assert cost is None

    def test_non_numeric_cost_raises(self) -> None:
        with pytest.raises(UsageMappingError, match=r"usage\.cost"):
            normalize_openrouter({"prompt_tokens": 1, "completion_tokens": 1, "cost": {}})


class TestAggregation:
    def test_addition_sums_buckets_and_degrades_source(self) -> None:
        a = normalize_anthropic(
            {"input_tokens": 10, "cache_creation_input_tokens": 100, "output_tokens": 5}
        )
        b = normalize_anthropic(
            {
                "input_tokens": 20,
                "cache_creation_input_tokens": 200,
                "output_tokens": 7,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 200,
                    "ephemeral_1h_input_tokens": 0,
                },
            }
        )
        total = a + b
        assert total.input_uncached == 30
        assert total.cache_write_5m == 300
        assert total.output_visible == 12
        # a's split was estimated, so the sum cannot claim to be provider-reported.
        assert total.sources["cache_write_5m"] == "estimated"
        assert total.sources["input_uncached"] == "provider"

    def test_bucket_items_lists_only_non_zero_buckets(self) -> None:
        u = normalize_openai({"prompt_tokens": 10, "completion_tokens": 2})
        assert [name for name, _, _ in u.bucket_items()] == ["input_uncached", "output_visible"]

    def test_empty_usage_is_estimated_not_provider(self) -> None:
        assert TokenUsage().source == "estimated"


class TestDispatch:
    def test_each_configured_mapping_name_resolves(self) -> None:
        raw = {"prompt_tokens": 10, "completion_tokens": 2}
        assert normalize("openai", raw).total == 12
        assert normalize("openrouter", raw).total == 12
        assert normalize("deepseek", raw).total == 12
        assert normalize("anthropic", {"input_tokens": 10, "output_tokens": 2}).total == 12

    def test_unknown_mapping_names_the_known_ones(self) -> None:
        with pytest.raises(UsageMappingError, match="anthropic, deepseek, openai, openrouter"):
            normalize("nope", {})

    def test_non_numeric_field_raises(self) -> None:
        with pytest.raises(UsageMappingError, match="expected a number"):
            normalize_openai({"prompt_tokens": "many", "completion_tokens": 1})
