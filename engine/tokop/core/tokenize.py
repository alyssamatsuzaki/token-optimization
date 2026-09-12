"""Token counting and estimation (SPEC.md 7.3).

Three sources of truth, in descending order of authority:

1. **Provider-reported usage**, after a call. Always wins; nothing here overrides it.
2. **An exact counting endpoint**, before a call. Anthropic's ``count_tokens`` for Anthropic
   models, ``tiktoken``'s ``o200k_base`` for OpenAI models.
3. **An estimate**: a base counter's output times a ratio fitted per model on recorded
   provider-reported usage.

Every number that leaves this module says which of the three it is and, for estimates, names
the method. That is the whole contract: a reader must never have to guess whether a token count
was measured or inferred (SPEC.md non-negotiable 2).

Anthropic's models from Claude 4.7 onward use a newer tokenizer that produces roughly 30% more
tokens for the same text than earlier models such as Haiku 4.5, which is exactly the kind of
systematic gap the fitted ratio exists to absorb.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Protocol

CountSource = Literal["exact", "estimated"]

MIN_CALLS_FOR_RATIO = 8


class TokenizerUnavailable(RuntimeError):
    """A tokenizer vocabulary could not be loaded."""


@dataclass(frozen=True)
class TokenCount:
    """A token count and the honest story of where it came from."""

    tokens: int
    source: CountSource
    method: str

    @property
    def is_exact(self) -> bool:
        return self.source == "exact"

    def as_dict(self) -> dict[str, object]:
        return {"tokens": self.tokens, "source": self.source, "method": self.method}


class BaseCounter(Protocol):
    """A deterministic text-to-token-count function with a name."""

    name: str

    def count(self, text: str) -> int: ...


class O200kCounter:
    """``tiktoken``'s ``o200k_base``: exact for OpenAI models, the base for everything else."""

    name = "o200k_base"

    def __init__(self) -> None:
        try:
            import tiktoken
        except ImportError as exc:  # pragma: no cover - tiktoken is a hard dependency
            raise TokenizerUnavailable("tiktoken is not installed") from exc
        try:
            self._encoding = tiktoken.get_encoding("o200k_base")
        except Exception as exc:
            raise TokenizerUnavailable(
                "tiktoken could not load the o200k_base vocabulary. It is downloaded on first "
                "use from openaipublic.blob.core.windows.net; that host is unreachable here. "
                "Set TIKTOKEN_CACHE_DIR to a directory holding the vocabulary to use it offline."
            ) from exc

    def count(self, text: str) -> int:
        return len(self._encoding.encode(text, disallowed_special=()))


# Approximates a GPT-style pre-tokenizer: contractions, a space-prefixed word, a digit run of
# at most three, a punctuation run, or a whitespace run.
_PRETOKEN = re.compile(
    r"'(?:[sdmt]|ll|ve|re)|[ ]?[^\s\d\W]+|[ ]?\d{1,3}|[ ]?[^\s\w]+|\s+",
    re.UNICODE,
)

_ALPHA_CHARS_PER_TOKEN = 4.2


class ApproxCounter:
    """A documented deterministic approximation, used when ``o200k_base`` cannot be loaded.

    It pre-tokenizes with a regex close to the GPT-4 pattern and then estimates sub-tokens per
    piece: a word of L letters costs roughly ``L / 4.2`` tokens (the observed average for
    English prose under byte-level BPE), digit runs cost one token per run of up to three
    digits, and punctuation costs about one token per character.

    It is **only ever a base for an estimate**, never reported as exact, and its name travels
    with every number it produces so no reader can mistake it for a real BPE count. Per-model
    ratio fitting (below) absorbs its systematic bias as soon as any recorded usage exists.
    """

    name = "bytes-bpe-approx-v1"

    def count(self, text: str) -> int:
        if not text:
            return 0
        total = 0
        for piece in _PRETOKEN.findall(text):
            stripped = piece.strip()
            if not stripped:
                # A whitespace run: roughly one token per four spaces, at least one.
                total += max(1, round(len(piece) / 4))
            elif stripped.isdigit():
                total += 1
            elif stripped[0].isalpha() or stripped[0] == "'":
                total += max(1, round(len(stripped) / _ALPHA_CHARS_PER_TOKEN))
            else:
                total += len(stripped)
        return total


@lru_cache(maxsize=1)
def get_base_counter() -> BaseCounter:
    """The best base counter this installation can load.

    Prefers ``o200k_base``. Falls back to the approximation **loudly**: the returned counter's
    ``name`` is carried into the ``method`` string of every estimate, so the fallback is visible
    in the UI, in the API payload, and in the report rather than hidden behind a plausible
    number (DECISIONS.md D3).
    """
    try:
        return O200kCounter()
    except TokenizerUnavailable:
        return ApproxCounter()


def base_counter_name() -> str:
    return get_base_counter().name


def counter_named(name: str) -> BaseCounter:
    """The counter with this name, or the best available one when it cannot be loaded.

    Committed fixtures record which counter built them, and everything computed over them is
    reproduced with that counter rather than with whichever one this machine happens to have —
    otherwise the same repository produces different generated documents on different machines
    (DECISIONS.md D26).

    ``ApproxCounter`` needs no vocabulary and is therefore always honourable. ``o200k_base`` is
    downloaded on first use, so a machine without egress to the vocabulary host cannot honour it;
    it falls back, and the report's ``token_counter_matches_fixtures`` says so rather than
    pretending the numbers are the recorded ones.
    """
    if name == ApproxCounter.name:
        return ApproxCounter()
    if name == O200kCounter.name:
        try:
            return O200kCounter()
        except TokenizerUnavailable:
            return ApproxCounter()
    raise TokenizerUnavailable(
        f"unknown token counter {name!r}. Known counters: "
        f"{O200kCounter.name!r}, {ApproxCounter.name!r}."
    )


@dataclass(frozen=True)
class ModelRatio:
    """A per-model correction from base-counter tokens to that model's real tokens."""

    model_id: str
    ratio: float
    fitted_on_calls: int

    @property
    def is_fitted(self) -> bool:
        return self.fitted_on_calls >= MIN_CALLS_FOR_RATIO

    def describe(self, base: str) -> str:
        if self.is_fitted:
            return f"≈ {base} × {self.ratio:.3g}, fitted on {self.fitted_on_calls} calls"
        return f"≈ {base} × {self.ratio:.3g}, unfitted default (no recorded calls yet)"


DEFAULT_RATIO = 1.0


def fit_ratio(
    model_id: str,
    base_tokens: Sequence[int],
    provider_tokens: Sequence[int],
) -> ModelRatio:
    """Least-squares ratio through the origin: sum(base*provider) / sum(base^2).

    A ratio rather than a regression with an intercept, because the relationship really is
    multiplicative — two tokenizers disagree about how finely they split the same text, not
    about a fixed per-request overhead. Calls where either count is zero carry no information
    about the slope and are dropped.
    """
    if len(base_tokens) != len(provider_tokens):
        raise ValueError(
            f"base and provider token counts must line up: {len(base_tokens)} vs "
            f"{len(provider_tokens)}"
        )
    pairs = [(b, p) for b, p in zip(base_tokens, provider_tokens, strict=True) if b > 0 and p > 0]
    if not pairs:
        return ModelRatio(model_id=model_id, ratio=DEFAULT_RATIO, fitted_on_calls=0)
    numerator = sum(b * p for b, p in pairs)
    denominator = sum(b * b for b, _ in pairs)
    return ModelRatio(
        model_id=model_id,
        ratio=numerator / denominator,
        fitted_on_calls=len(pairs),
    )


class TokenEstimator:
    """Counts tokens for a model, exactly where possible and by fitted ratio otherwise."""

    def __init__(
        self,
        ratios: dict[str, ModelRatio] | None = None,
        base: BaseCounter | None = None,
    ) -> None:
        self._base = base or get_base_counter()
        self._ratios = ratios or {}

    @property
    def base_name(self) -> str:
        return self._base.name

    def set_ratio(self, ratio: ModelRatio) -> None:
        self._ratios[ratio.model_id] = ratio

    def ratio_for(self, model_id: str) -> ModelRatio:
        return self._ratios.get(model_id, ModelRatio(model_id, DEFAULT_RATIO, 0))

    def count(self, text: str, model_id: str, *, exact_counter_ran: bool = False) -> TokenCount:
        """Estimate tokens for ``text`` under ``model_id``.

        ``exact_counter_ran`` is for callers that already obtained a provider count and want the
        result shaped the same way; it never makes an estimate claim to be exact on its own.
        """
        base_tokens = self._base.count(text)
        if exact_counter_ran:
            raise ValueError(
                "exact counts come from exact_count(); this method only ever estimates"
            )
        ratio = self.ratio_for(model_id)
        return TokenCount(
            tokens=round(base_tokens * ratio.ratio),
            source="estimated",
            method=ratio.describe(self._base.name),
        )

    def exact_count(self, tokens: int, method: str) -> TokenCount:
        """Wrap a count that came from a real counting endpoint."""
        return TokenCount(tokens=tokens, source="exact", method=method)

    def count_openai_exact(self, text: str, model_id: str) -> TokenCount:
        """Exact for OpenAI models, which really are tokenized with ``o200k_base``.

        Raises when the vocabulary is unavailable rather than passing the approximation off as
        exact — an OpenAI cost quoted from a guess would be wrong in a way the reader could not
        see.
        """
        if self._base.name != "o200k_base":
            raise TokenizerUnavailable(
                f"an exact count for {model_id} needs the o200k_base vocabulary, which is not "
                f"loadable here (the base counter is {self._base.name}). Use count() for an "
                "estimate that names its method."
            )
        return TokenCount(
            tokens=self._base.count(text),
            source="exact",
            method="o200k_base (tiktoken)",
        )
