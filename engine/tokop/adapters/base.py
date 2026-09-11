"""The adapter contract (SPEC.md 7.1).

Both adapters are written by hand rather than routed through a gateway library. Usage
normalization *is* the product: every line of the mapping from a provider's response to a
dollar figure has to be auditable, and a dependency that "just handles it" would put the most
important code in the build behind someone else's abstraction.

A request is a plain data object, not a provider payload. Each adapter renders it into its own
wire format, which is what makes the same pipeline definition runnable across providers and
what makes the cassette key stable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from tokop.core.usage import TokenUsage

Role = Literal["user", "assistant"]
CacheTTL = Literal["5m", "1h"]


class AdapterError(RuntimeError):
    """A provider call failed in a way the caller has to see."""


class CassetteMiss(AdapterError):
    """Replay mode was asked for a request that was never recorded."""

    def __init__(self, key: str, provider: str, model: str, summary: str) -> None:
        self.key = key
        super().__init__(
            f"no cassette for {provider}/{model} (key {key[:12]}…). Replay never falls back to "
            f"a live call. Request: {summary}"
        )


class Block(BaseModel):
    """One content block. ``cache`` marks an explicit cache breakpoint after this block."""

    text: str
    cache: CacheTTL | None = None

    model_config = {"frozen": True}


class Message(BaseModel):
    role: Role
    blocks: list[Block]

    model_config = {"frozen": True}

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)


class LLMRequest(BaseModel):
    """Everything needed to make one call, in a provider-neutral shape.

    The field order here is also the canonicalization order for the cassette key, and the key
    is computed from JSON with sorted keys. That matters twice: an unstable key order would
    make recordings unreproducible, and it would also break provider prompt caches, which match
    on an exact prefix over tools, then system, then messages.
    """

    provider: str
    model: str
    system: list[Block] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    max_tokens: int = 1024
    temperature: float | None = None
    stop_sequences: list[str] = Field(default_factory=list)
    # Set by pre-warming calls, which send max_tokens: 0 to write a cache entry and bill no
    # output. Rejected by the API alongside streaming, thinking, structured outputs or a forced
    # tool choice, so it is tracked explicitly rather than inferred from max_tokens.
    prewarm: bool = False

    model_config = {"frozen": True}

    def canonical(self) -> dict[str, Any]:
        """The request as it is hashed. Sorted keys, no volatile fields."""
        return {
            "provider": self.provider,
            "model": self.model,
            "system": [{"text": b.text, "cache": b.cache} for b in self.system],
            "messages": [
                {"role": m.role, "blocks": [{"text": b.text, "cache": b.cache} for b in m.blocks]}
                for m in self.messages
            ],
            "tools": self.tools,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stop_sequences": self.stop_sequences,
            "prewarm": self.prewarm,
        }

    def cassette_key(self) -> str:
        """SHA-256 over provider, model and the canonicalized request (SPEC.md 7.1)."""
        payload = json.dumps(self.canonical(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def prompt_hash(self) -> str:
        """Hash of the prompt text alone, for spotting repeated blocks across calls."""
        parts = [b.text for b in self.system] + [b.text for m in self.messages for b in m.blocks]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    @property
    def static_prefix_text(self) -> str:
        """Text up to and including the last cache breakpoint.

        This is what a provider cache can actually hold, so it is what the cacheability checks
        in lint measure against each model's minimum.
        """
        parts: list[str] = []
        last_break = -1
        for index, block in enumerate(self.system):
            parts.append(block.text)
            if block.cache is not None:
                last_break = index
        if last_break < 0:
            return ""
        return "\n\n".join(parts[: last_break + 1])

    @property
    def system_text(self) -> str:
        return "\n\n".join(b.text for b in self.system)

    def summary(self) -> str:
        first = self.messages[0].text[:80] if self.messages else ""
        return f"max_tokens={self.max_tokens} system={len(self.system_text)}ch user={first!r}"


class LLMResponse(BaseModel):
    """One provider response, normalized."""

    text: str
    usage: TokenUsage
    model: str
    provider: str
    latency_ms: float
    ttft_ms: float | None = None
    stop_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    # True when the response came from a cassette with a matching key instead of the provider.
    # Reused calls are excluded from latency statistics (SPEC.md 7.1).
    reused: bool = False
    # Set only by gateways that report an authoritative cost.
    provider_cost_usd: Decimal | None = None
    attempt: int = 1
    error: str | None = None

    model_config = {"frozen": True}

    @property
    def failed(self) -> bool:
        return self.error is not None


class Adapter(Protocol):
    """What the runner needs from a provider."""

    name: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def count_tokens(self, request: LLMRequest) -> int | None:
        """Exact input token count where the provider offers one, otherwise ``None``."""
        ...


def blocks_of(*texts: str, cache_last: CacheTTL | None = None) -> list[Block]:
    """Convenience for building a block list with an optional breakpoint on the last block."""
    if not texts:
        return []
    out = [Block(text=t) for t in texts[:-1]]
    out.append(Block(text=texts[-1], cache=cache_last))
    return out


def user_message(*texts: str) -> Message:
    return Message(role="user", blocks=[Block(text=t) for t in texts])


def concat_text(blocks: Sequence[Block]) -> str:
    return "\n\n".join(b.text for b in blocks)
