"""Normalize provider usage payloads into one set of buckets.

Every provider reports token usage in its own shape, and several of them report *overlapping*
totals — adding the fields together double-counts. This module is the single place that knows
the difference, because a cost number is only as trustworthy as this mapping (SPEC.md 7.2).

Each bucket carries its own source: ``provider`` when the number came straight out of the
response, ``estimated`` when Tokop had to derive it. Deriving is rare and always documented at
the call site; nothing is silently invented.

The raw payload is kept next to the buckets on every call so an auditor can redo the mapping.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

Source = Literal["provider", "estimated"]

BUCKETS: tuple[str, ...] = (
    "input_uncached",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
    "output_visible",
    "output_reasoning",
    # Reserved. v1 does not account for image, video or audio generation (SPEC.md section 3).
    "image_in",
    "audio_in",
    "image_out",
)


class UsageMappingError(ValueError):
    """A usage payload could not be mapped without inventing a number."""


class TokenUsage(BaseModel):
    """Normalized token usage for one provider call."""

    input_uncached: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output_visible: int = 0
    output_reasoning: int = 0
    image_in: int = 0
    audio_in: int = 0
    image_out: int = 0

    sources: dict[str, Source] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True}

    @property
    def total_input(self) -> int:
        """Every input token the request actually carried, cached or not.

        This is the number Anthropic's ``input_tokens`` is *not*: see ``normalize_anthropic``.
        """
        return self.input_uncached + self.cache_write_5m + self.cache_write_1h + self.cache_read

    @property
    def total_output(self) -> int:
        """Visible plus reasoning. Both bill at the output rate on every provider in v1."""
        return self.output_visible + self.output_reasoning

    @property
    def total(self) -> int:
        return self.total_input + self.total_output

    @property
    def source(self) -> str:
        """``provider``, ``estimated``, or ``mixed`` when buckets disagree."""
        present = {self.sources[b] for b in BUCKETS if getattr(self, b) and b in self.sources}
        if not present:
            return "provider" if self.raw else "estimated"
        if len(present) == 1:
            return next(iter(present))
        return "mixed"

    def bucket_items(self) -> list[tuple[str, int, Source]]:
        """Non-zero buckets with their value and source, in display order."""
        return [
            (b, getattr(self, b), self.sources.get(b, "provider"))
            for b in BUCKETS
            if getattr(self, b)
        ]

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Aggregate usage across calls. A bucket is ``provider`` only if every part was."""
        sources: dict[str, Source] = {}
        for b in BUCKETS:
            mine, theirs = self.sources.get(b), other.sources.get(b)
            if mine is None and theirs is None:
                continue
            sources[b] = "estimated" if "estimated" in (mine, theirs) else "provider"
        return TokenUsage(
            **{b: getattr(self, b) + getattr(other, b) for b in BUCKETS},
            sources=sources,
            raw={},
        )


def _require_non_negative(name: str, value: int, context: str) -> int:
    if value < 0:
        raise UsageMappingError(
            f"{name} came out negative ({value}) while mapping {context}. The payload does not "
            "match the documented shape; refusing to guess."
        )
    return value


def _int(payload: dict[str, Any], key: str, default: int = 0) -> int:
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, int | float):
        raise UsageMappingError(f"usage field {key!r} is {type(value).__name__}, expected a number")
    return int(value)


def normalize_anthropic(raw: dict[str, Any]) -> TokenUsage:
    """Map an Anthropic Messages API ``usage`` object.

    The trap: ``input_tokens`` counts **only the tokens after the last cache breakpoint**, so
    total input is ``input_tokens + cache_creation_input_tokens + cache_read_input_tokens``.
    Adding ``input_tokens`` to a separately computed total double-counts nothing, but *treating
    it as the total* undercounts a cached request by the whole prefix.

    ``cache_creation`` splits writes by lifetime into ``ephemeral_5m_input_tokens`` and
    ``ephemeral_1h_input_tokens``, which bill at 1.25x and 2x base input respectively. When the
    object is absent but ``cache_creation_input_tokens`` is non-zero, the split is attributed to
    the 5-minute lifetime (the API default) and that bucket is marked ``estimated``.

    Extended-thinking tokens are included in ``output_tokens`` and are not broken out, so they
    land in ``output_visible``. Billing is unaffected: thinking bills at the output rate.

    https://platform.claude.com/docs/en/build-with-claude/prompt-caching — checked 2026-09-11.
    """
    sources: dict[str, Source] = {}
    input_uncached = _require_non_negative("input_uncached", _int(raw, "input_tokens"), "anthropic")
    cache_read = _require_non_negative(
        "cache_read", _int(raw, "cache_read_input_tokens"), "anthropic"
    )
    creation_total = _require_non_negative(
        "cache_creation_input_tokens", _int(raw, "cache_creation_input_tokens"), "anthropic"
    )

    creation = raw.get("cache_creation")
    if isinstance(creation, dict):
        write_5m = _int(creation, "ephemeral_5m_input_tokens")
        write_1h = _int(creation, "ephemeral_1h_input_tokens")
        if creation_total and write_5m + write_1h != creation_total:
            raise UsageMappingError(
                f"cache_creation splits ({write_5m} + {write_1h}) do not sum to "
                f"cache_creation_input_tokens ({creation_total})"
            )
        if write_5m:
            sources["cache_write_5m"] = "provider"
        if write_1h:
            sources["cache_write_1h"] = "provider"
    else:
        # No breakdown. The 5-minute lifetime is the API default, so attribute there and say so.
        write_5m, write_1h = creation_total, 0
        if write_5m:
            sources["cache_write_5m"] = "estimated"

    output_visible = _require_non_negative(
        "output_visible", _int(raw, "output_tokens"), "anthropic"
    )

    if input_uncached:
        sources["input_uncached"] = "provider"
    if cache_read:
        sources["cache_read"] = "provider"
    if output_visible:
        sources["output_visible"] = "provider"

    return TokenUsage(
        input_uncached=input_uncached,
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        cache_read=cache_read,
        output_visible=output_visible,
        output_reasoning=0,
        sources=sources,
        raw=dict(raw),
    )


def normalize_openai(raw: dict[str, Any]) -> TokenUsage:
    """Map an OpenAI Chat Completions ``usage`` object.

    The trap runs the other way from Anthropic's: ``prompt_tokens`` **already includes**
    ``prompt_tokens_details.cached_tokens``, and ``completion_tokens`` **already includes**
    ``completion_tokens_details.reasoning_tokens``. Subtract to get the uncached and visible
    parts; adding them double-counts.

    OpenAI's prompt caching is automatic and carries no write charge, so both write buckets
    stay zero rather than being invented.

    https://platform.openai.com/docs/api-reference/chat — checked 2026-09-11.
    """
    prompt = _require_non_negative("prompt_tokens", _int(raw, "prompt_tokens"), "openai")
    completion = _require_non_negative(
        "completion_tokens", _int(raw, "completion_tokens"), "openai"
    )

    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    if not isinstance(prompt_details, dict) or not isinstance(completion_details, dict):
        raise UsageMappingError("prompt_tokens_details / completion_tokens_details must be objects")

    cached = _int(prompt_details, "cached_tokens")
    reasoning = _int(completion_details, "reasoning_tokens")

    input_uncached = _require_non_negative(
        "input_uncached", prompt - cached, "openai (cached_tokens exceeds prompt_tokens)"
    )
    output_visible = _require_non_negative(
        "output_visible",
        completion - reasoning,
        "openai (reasoning_tokens exceeds completion_tokens)",
    )

    sources: dict[str, Source] = {}
    for bucket, value in (
        ("input_uncached", input_uncached),
        ("cache_read", cached),
        ("output_visible", output_visible),
        ("output_reasoning", reasoning),
    ):
        if value:
            sources[bucket] = "provider"

    return TokenUsage(
        input_uncached=input_uncached,
        cache_read=cached,
        output_visible=output_visible,
        output_reasoning=reasoning,
        sources=sources,
        raw=dict(raw),
    )


def normalize_deepseek(raw: dict[str, Any]) -> TokenUsage:
    """Map a DeepSeek ``usage`` object.

    DeepSeek is OpenAI-compatible in request shape but reports cache hits as
    ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens`` rather than OpenAI's
    ``prompt_tokens_details.cached_tokens``. The two fields partition ``prompt_tokens``, so the
    miss count *is* the uncached bucket; no subtraction is needed.

    https://api-docs.deepseek.com/guides/kv_cache — field names per SPEC.md 7.2, 2026-09-11.
    """
    if "prompt_cache_hit_tokens" not in raw and "prompt_cache_miss_tokens" not in raw:
        # Not a DeepSeek-shaped payload: fall through to the OpenAI mapping rather than
        # reporting an all-uncached request that may in fact have hit the cache.
        return normalize_openai(raw)

    hit = _require_non_negative(
        "prompt_cache_hit_tokens", _int(raw, "prompt_cache_hit_tokens"), "deepseek"
    )
    miss = _require_non_negative(
        "prompt_cache_miss_tokens", _int(raw, "prompt_cache_miss_tokens"), "deepseek"
    )
    prompt = _int(raw, "prompt_tokens", hit + miss)
    if prompt and hit + miss != prompt:
        raise UsageMappingError(
            f"prompt_cache_hit_tokens + prompt_cache_miss_tokens ({hit} + {miss}) do not sum to "
            f"prompt_tokens ({prompt})"
        )

    completion = _require_non_negative(
        "completion_tokens", _int(raw, "completion_tokens"), "deepseek"
    )
    completion_details = raw.get("completion_tokens_details") or {}
    reasoning = (
        _int(completion_details, "reasoning_tokens") if isinstance(completion_details, dict) else 0
    )
    output_visible = _require_non_negative("output_visible", completion - reasoning, "deepseek")

    sources: dict[str, Source] = {}
    for bucket, value in (
        ("input_uncached", miss),
        ("cache_read", hit),
        ("output_visible", output_visible),
        ("output_reasoning", reasoning),
    ):
        if value:
            sources[bucket] = "provider"

    return TokenUsage(
        input_uncached=miss,
        cache_read=hit,
        output_visible=output_visible,
        output_reasoning=reasoning,
        sources=sources,
        raw=dict(raw),
    )


def normalize_openrouter(raw: dict[str, Any]) -> tuple[TokenUsage, Decimal | None]:
    """Map an OpenRouter ``usage`` object and return any authoritative cost it carried.

    OpenRouter proxies many upstream providers and reports usage in OpenAI's shape. When it
    also returns a ``cost``, that number is what the account is actually charged, so it is
    stored next to the registry-computed cost with ``cost_source=provider``; the caller flags
    disagreements above 2% rather than silently preferring one (SPEC.md 7.2).

    https://openrouter.ai/docs/api-reference/overview — field names per SPEC.md 7.2, 2026-09-11.
    """
    usage = normalize_openai(raw)
    cost_raw = raw.get("cost")
    if cost_raw is None:
        return usage, None
    if not isinstance(cost_raw, int | float | str):
        raise UsageMappingError(f"usage.cost is {type(cost_raw).__name__}, expected a number")
    return usage, Decimal(str(cost_raw))


MAPPINGS = {
    "anthropic": normalize_anthropic,
    "openai": normalize_openai,
    "deepseek": normalize_deepseek,
}


def normalize(mapping: str, raw: dict[str, Any]) -> TokenUsage:
    """Dispatch by the ``usage_mapping`` name in config/providers.yaml."""
    if mapping == "openrouter":
        return normalize_openrouter(raw)[0]
    try:
        fn = MAPPINGS[mapping]
    except KeyError:
        raise UsageMappingError(
            f"no usage mapping named {mapping!r}; known mappings: "
            f"{', '.join(sorted([*MAPPINGS, 'openrouter']))}"
        ) from None
    return fn(raw)
