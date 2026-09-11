"""Cassettes: recorded provider calls, keyed by the exact request (SPEC.md 7.1).

The key is the SHA-256 of provider, model and the canonicalized request. That one decision
buys three things:

* **Resumability.** An interrupted ``make record`` can be restarted and will reuse every
  response it already paid for, because an identical request produces an identical key. This
  is why recording a $20 dataset is not a $20 gamble.
* **Replay.** The whole app runs from cassettes with no API keys, which is what the public demo
  and the test suite use.
* **Auditability.** Every cassette holds the request that produced it, so a reader can check
  that the recorded response really belongs to the prompt being shown.

In replay mode a missing key raises ``CassetteMiss`` naming the request. Replay **never** falls
back to a live call: a demo that quietly spends money is worse than one that fails loudly.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from tokop.adapters.base import CassetteMiss, LLMRequest, LLMResponse
from tokop.core.usage import TokenUsage


class Cassette(BaseModel):
    """One recorded call: the request, the raw response, and the usage it reported."""

    key: str
    provider: str
    model: str
    request: dict[str, Any]
    response_text: str
    raw_response: dict[str, Any]
    raw_usage: dict[str, Any]
    usage: TokenUsage
    latency_ms: float
    ttft_ms: float | None = None
    stop_reason: str | None = None
    recorded_at: str
    # "live" for a real provider call, "simulated" for the deterministic in-process provider.
    origin: str = "live"
    error: str | None = None
    attempt: int = 1
    provider_cost_usd: str | None = None
    notes: dict[str, Any] = Field(default_factory=dict)

    def to_response(self, *, reused: bool) -> LLMResponse:
        from decimal import Decimal

        return LLMResponse(
            text=self.response_text,
            usage=self.usage,
            model=self.model,
            provider=self.provider,
            latency_ms=self.latency_ms,
            ttft_ms=self.ttft_ms,
            stop_reason=self.stop_reason,
            raw=self.raw_response,
            reused=reused,
            provider_cost_usd=(
                Decimal(self.provider_cost_usd) if self.provider_cost_usd is not None else None
            ),
            attempt=self.attempt,
            error=self.error,
        )


class CassetteStore:
    """A directory of cassettes, one JSON file per key.

    One file per call rather than one big archive, so that an interrupted recording leaves
    every completed call intact and so that git shows what a re-recording actually changed.
    Writes go through a temporary file and an atomic rename: a process killed mid-write leaves
    the previous cassette, never a truncated one.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._index: dict[str, Path] | None = None

    def path_for(self, key: str) -> Path:
        # Two-character shard so a 1,500-call recording does not make one unlistable directory.
        return self.root / key[:2] / f"{key}.json"

    def has(self, key: str) -> bool:
        return self.path_for(key).exists()

    def get(self, key: str) -> Cassette | None:
        path = self.path_for(key)
        if not path.exists():
            return None
        return Cassette.model_validate_json(path.read_text())

    def require(self, key: str, request: LLMRequest) -> Cassette:
        cassette = self.get(key)
        if cassette is None:
            raise CassetteMiss(key, request.provider, request.model, request.summary())
        return cassette

    def put(self, cassette: Cassette) -> Path:
        path = self.path_for(cassette.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = cassette.model_dump_json(indent=2)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        self._index = None
        return path

    def record(
        self,
        request: LLMRequest,
        response: LLMResponse,
        *,
        origin: str = "live",
        notes: dict[str, Any] | None = None,
    ) -> Cassette:
        cassette = Cassette(
            key=request.cassette_key(),
            provider=request.provider,
            model=request.model,
            request=request.canonical(),
            response_text=response.text,
            raw_response=response.raw,
            raw_usage=response.usage.raw,
            usage=response.usage,
            latency_ms=response.latency_ms,
            ttft_ms=response.ttft_ms,
            stop_reason=response.stop_reason,
            recorded_at=datetime.now(UTC).isoformat(),
            origin=origin,
            error=response.error,
            attempt=response.attempt,
            provider_cost_usd=(
                str(response.provider_cost_usd) if response.provider_cost_usd is not None else None
            ),
            notes=notes or {},
        )
        self.put(cassette)
        return cassette

    def keys(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*/*.json"))

    def all(self) -> list[Cassette]:
        return [
            Cassette.model_validate_json(p.read_text()) for p in sorted(self.root.glob("*/*.json"))
        ]

    def __len__(self) -> int:
        return len(self.keys())

    def stats(self) -> dict[str, Any]:
        cassettes = self.all()
        origins: dict[str, int] = {}
        models: dict[str, int] = {}
        earliest: str | None = None
        latest: str | None = None
        for c in cassettes:
            origins[c.origin] = origins.get(c.origin, 0) + 1
            models[c.model] = models.get(c.model, 0) + 1
            if earliest is None or c.recorded_at < earliest:
                earliest = c.recorded_at
            if latest is None or c.recorded_at > latest:
                latest = c.recorded_at
        return {
            "count": len(cassettes),
            "origins": origins,
            "models": models,
            "recorded_from": earliest,
            "recorded_to": latest,
        }


class ReplayAdapter:
    """Serves recorded responses and nothing else.

    Every call is marked ``reused=True``, which keeps recorded latencies out of latency
    statistics — a replayed p50 would describe a disk read, not a provider.
    """

    def __init__(self, store: CassetteStore, name: str = "replay") -> None:
        self.store = store
        self.name = name

    async def complete(self, request: LLMRequest) -> LLMResponse:
        cassette = self.store.require(request.cassette_key(), request)
        return cassette.to_response(reused=True)

    async def count_tokens(self, request: LLMRequest) -> int | None:
        """No counting endpoint in replay: the recorded usage is the count that matters."""
        cassette = self.store.get(request.cassette_key())
        if cassette is None:
            return None
        return cassette.usage.total_input


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"no run manifest at {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data
