"""A deterministic in-process provider, used to build fixtures and to test the recorder.

**This is not a provider.** It opens no socket, and it is refused in live mode. It exists
because this build has no API credentials (DECISIONS.md D1) and the product still has to be
exercised end to end: the runner, the cassette store, usage normalization, the cascade, the
proof and every screen all need a response matrix to compute over.

What it simulates honestly:

* **Prompt caching economics.** It tracks which prefixes have been written, honours explicit
  breakpoints, and — like the real API — silently declines to cache a prefix below the model's
  minimum, reporting zero in both cache fields rather than erroring.
* **Tokenizer generations.** Models on the newer tokenizer count about 30% higher than Haiku
  4.5 for the same text, so a cache prefix that clears Haiku's 4,096-token minimum is sized
  the way it would really have to be.
* **Usage payload shape.** It emits an Anthropic-shaped ``usage`` object, so the same
  normalization code runs over it as over a real recording.

What it cannot simulate: whether a model is actually right. Answer quality comes from a
responder the caller supplies, and everything downstream of it is labelled ``simulated``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tokop.adapters.base import AdapterError, LLMRequest, LLMResponse
from tokop.core.tokenize import BaseCounter, get_base_counter
from tokop.core.usage import normalize_anthropic

ORIGIN = "simulated"

#: Token multipliers relative to the base counter, by tokenizer generation. Anthropic's models
#: from Claude 4.7 onward produce about 30% more tokens for the same text than Haiku 4.5.
GENERATION_RATIO = {"newer": 1.30, "previous": 1.00}


@dataclass(frozen=True)
class ModelProfile:
    """How one simulated model counts, caches and paces."""

    model_id: str
    min_cacheable_tokens: int
    tokenizer_generation: str = "newer"
    ms_per_output_token: float = 8.0
    base_latency_ms: float = 420.0

    @property
    def ratio(self) -> float:
        try:
            return GENERATION_RATIO[self.tokenizer_generation]
        except KeyError:
            raise AdapterError(
                f"unknown tokenizer generation {self.tokenizer_generation!r} for "
                f"{self.model_id}; expected one of {sorted(GENERATION_RATIO)}"
            ) from None


Responder = Callable[[LLMRequest], str]


@dataclass
class SimulatedAdapter:
    """Answers requests from a supplied responder and reports plausible usage."""

    profiles: dict[str, ModelProfile]
    responder: Responder
    name: str = ORIGIN
    counter: BaseCounter = field(default_factory=get_base_counter)
    #: Prefix hashes already written to the simulated cache, with the tokens they hold.
    _cache: dict[str, int] = field(default_factory=dict, init=False)
    calls: int = field(default=0, init=False)

    def profile(self, model_id: str) -> ModelProfile:
        try:
            return self.profiles[model_id]
        except KeyError:
            raise AdapterError(
                f"no simulated profile for {model_id!r}; known: {', '.join(sorted(self.profiles))}"
            ) from None

    def _tokens(self, text: str, profile: ModelProfile) -> int:
        return round(self.counter.count(text) * profile.ratio)

    def reset_cache(self) -> None:
        """Forget every cached prefix, as a 5-minute lifetime expiring would."""
        self._cache.clear()

    def _usage_payload(self, request: LLMRequest, output_text: str) -> dict[str, Any]:
        """Build an Anthropic-shaped usage object for this request.

        The split mirrors the real API: ``input_tokens`` counts **only** what follows the last
        breakpoint, and the prefix lands in either the write or the read bucket.
        """
        profile = self.profile(request.model)
        prefix_text = request.static_prefix_text
        full_input = self._tokens(request.system_text, profile) + sum(
            self._tokens(b.text, profile) for m in request.messages for b in m.blocks
        )
        prefix_tokens = self._tokens(prefix_text, profile) if prefix_text else 0

        cache_write = 0
        cache_read = 0
        if prefix_tokens:
            if prefix_tokens < profile.min_cacheable_tokens:
                # Below the minimum the request runs uncached and returns no error; both cache
                # fields read 0 and the whole prefix is billed as ordinary input.
                pass
            else:
                digest = hashlib.sha256(f"{request.model}\x00{prefix_text}".encode()).hexdigest()
                if digest in self._cache:
                    cache_read = prefix_tokens
                else:
                    cache_write = prefix_tokens
                    self._cache[digest] = prefix_tokens

        after_breakpoint = max(0, full_input - cache_write - cache_read)
        ttl = next(
            (b.cache for b in reversed(request.system) if b.cache is not None),
            "5m",
        )
        usage: dict[str, Any] = {
            "input_tokens": after_breakpoint,
            "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read,
            "output_tokens": self._tokens(output_text, profile),
        }
        if cache_write:
            usage["cache_creation"] = {
                "ephemeral_5m_input_tokens": cache_write if ttl == "5m" else 0,
                "ephemeral_1h_input_tokens": cache_write if ttl == "1h" else 0,
            }
        return usage

    async def complete(self, request: LLMRequest) -> LLMResponse:
        profile = self.profile(request.model)
        self.calls += 1

        # A pre-warm call writes the cache entry and bills no output, exactly as max_tokens: 0
        # does on the real API.
        output_text = "" if request.prewarm else self.responder(request)
        if not request.prewarm and request.max_tokens <= 0:
            raise AdapterError("max_tokens must be positive unless the request is a pre-warm")

        usage_raw = self._usage_payload(request, output_text)
        usage = normalize_anthropic(usage_raw)

        # Deterministic latency: a seeded jitter keyed on the request, so a re-record produces
        # the same number and a diff shows only what really changed.
        jitter_seed = int(request.cassette_key()[:8], 16) / 0xFFFFFFFF
        latency = (
            profile.base_latency_ms * (0.85 + 0.3 * jitter_seed)
            + usage.output_visible * profile.ms_per_output_token
        )
        ttft = profile.base_latency_ms * (0.85 + 0.3 * jitter_seed)

        stop_reason = "max_tokens" if request.prewarm else "end_turn"
        raw: dict[str, Any] = {
            "id": f"msg_sim_{request.cassette_key()[:16]}",
            "type": "message",
            "role": "assistant",
            "model": request.model,
            "content": [{"type": "text", "text": output_text}],
            "stop_reason": stop_reason,
            "usage": usage_raw,
            "_tokop_origin": ORIGIN,
        }
        return LLMResponse(
            text=output_text,
            usage=usage,
            model=request.model,
            provider=self.name,
            latency_ms=round(latency, 2),
            ttft_ms=round(ttft, 2),
            stop_reason=stop_reason,
            raw=raw,
        )

    async def count_tokens(self, request: LLMRequest) -> int | None:
        profile = self.profile(request.model)
        total = self._tokens(request.system_text, profile) + sum(
            self._tokens(b.text, profile) for m in request.messages for b in m.blocks
        )
        return total
