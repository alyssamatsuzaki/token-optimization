"""Token counting, the fitted ratio, and the honesty of the method string (SPEC.md 7.3)."""

from __future__ import annotations

import pytest

from tokop.core.tokenize import (
    ApproxCounter,
    ModelRatio,
    TokenEstimator,
    TokenizerUnavailable,
    base_counter_name,
    fit_ratio,
    get_base_counter,
)


class TestApproxCounter:
    def test_empty_text_is_zero(self) -> None:
        assert ApproxCounter().count("") == 0

    def test_prose_lands_near_four_characters_per_token(self) -> None:
        text = (
            "Returns are accepted within thirty days of delivery for most product categories, "
            "and gold members receive an additional fourteen days on every order they place."
        )
        tokens = ApproxCounter().count(text)
        # ~155 characters of English prose; byte-level BPE lands in the mid-30s.
        assert 25 <= tokens <= 50
        assert 2.5 <= len(text) / tokens <= 6.0

    def test_is_deterministic(self) -> None:
        counter = ApproxCounter()
        assert counter.count("the same text") == counter.count("the same text")

    def test_longer_text_costs_more_tokens(self) -> None:
        counter = ApproxCounter()
        assert counter.count("word " * 100) > counter.count("word " * 10)

    def test_digits_and_punctuation_are_counted_separately(self) -> None:
        counter = ApproxCounter()
        assert counter.count("12345") >= 2
        assert counter.count("!!!!") >= 4

    def test_names_itself(self) -> None:
        assert ApproxCounter().name == "bytes-bpe-approx-v1"


class TestBaseCounterSelection:
    def test_a_base_counter_is_always_available_and_named(self) -> None:
        counter = get_base_counter()
        assert counter.name in ("o200k_base", "bytes-bpe-approx-v1")
        assert base_counter_name() == counter.name
        assert counter.count("hello world") > 0

    def test_exact_openai_count_refuses_to_use_the_approximation(self) -> None:
        estimator = TokenEstimator(base=ApproxCounter())
        with pytest.raises(TokenizerUnavailable, match="not loadable here"):
            estimator.count_openai_exact("hello", "gpt-5")


class TestFitRatio:
    def test_perfect_proportional_data_recovers_the_ratio(self) -> None:
        base = [100, 200, 300, 400, 500, 600, 700, 800]
        provider = [130, 260, 390, 520, 650, 780, 910, 1040]
        r = fit_ratio("claude-opus-5", base, provider)
        assert r.ratio == pytest.approx(1.3)
        assert r.fitted_on_calls == 8
        assert r.is_fitted

    def test_the_newer_anthropic_tokenizer_shows_up_as_a_ratio_near_1_3(self) -> None:
        """SPEC.md 7.3: models from Claude 4.7 on produce ~30% more tokens than Haiku 4.5."""
        base = [1000] * 10
        newer = [1300] * 10
        older = [1000] * 10
        assert fit_ratio("opus", base, newer).ratio == pytest.approx(1.3)
        assert fit_ratio("haiku", base, older).ratio == pytest.approx(1.0)

    def test_zero_counts_carry_no_slope_information_and_are_dropped(self) -> None:
        r = fit_ratio("m", [0, 100, 0, 200], [0, 150, 5, 300])
        assert r.ratio == pytest.approx(1.5)
        assert r.fitted_on_calls == 2

    def test_no_usable_pairs_falls_back_to_one_and_says_it_is_unfitted(self) -> None:
        r = fit_ratio("m", [], [])
        assert r.ratio == 1.0
        assert r.fitted_on_calls == 0
        assert not r.is_fitted

    def test_ragged_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="line up"):
            fit_ratio("m", [1, 2], [1])

    def test_few_calls_is_not_considered_fitted(self) -> None:
        r = fit_ratio("m", [100, 200], [130, 260])
        assert r.ratio == pytest.approx(1.3)
        assert not r.is_fitted


class TestTokenEstimator:
    def test_an_estimate_is_marked_estimated_and_names_its_method(self) -> None:
        estimator = TokenEstimator(base=ApproxCounter())
        estimator.set_ratio(ModelRatio("claude-opus-5", 1.3, 312))
        count = estimator.count("some prompt text here", "claude-opus-5")
        assert count.source == "estimated"
        assert not count.is_exact
        assert count.method == "≈ bytes-bpe-approx-v1 × 1.3, fitted on 312 calls"

    def test_an_unfitted_model_says_so_rather_than_implying_a_fit(self) -> None:
        estimator = TokenEstimator(base=ApproxCounter())
        count = estimator.count("text", "brand-new-model")
        assert "unfitted default" in count.method
        assert "no recorded calls yet" in count.method

    def test_the_ratio_actually_scales_the_count(self) -> None:
        estimator = TokenEstimator(base=ApproxCounter())
        plain = estimator.count("a longer piece of prompt text", "m").tokens
        estimator.set_ratio(ModelRatio("m", 2.0, 50))
        assert estimator.count("a longer piece of prompt text", "m").tokens == 2 * plain

    def test_wrapping_a_provider_count_marks_it_exact(self) -> None:
        estimator = TokenEstimator(base=ApproxCounter())
        count = estimator.exact_count(7102, "anthropic count_tokens")
        assert count.is_exact
        assert count.as_dict() == {
            "tokens": 7102,
            "source": "exact",
            "method": "anthropic count_tokens",
        }

    def test_count_refuses_to_be_told_it_was_exact(self) -> None:
        estimator = TokenEstimator(base=ApproxCounter())
        with pytest.raises(ValueError, match="only ever estimates"):
            estimator.count("text", "m", exact_counter_ran=True)

    def test_base_name_is_exposed_for_the_ui(self) -> None:
        assert TokenEstimator(base=ApproxCounter()).base_name == "bytes-bpe-approx-v1"
