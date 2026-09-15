"""Exact-request reuse: the wrapper that makes recording resumable (SPEC.md 7.1).

In record mode an identical request reuses its cassette instead of calling the provider. That
is the whole mechanism behind "an interrupted ``make record`` resumes where it stopped": the
key is a hash of the request, so a restarted recorder recognises every call it already paid
for and pays for none of them twice.

Reused calls are marked, and their latencies are excluded from latency statistics — a reused
latency describes a disk read, not a provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from tokop.adapters.base import LLMRequest, LLMResponse
from tokop.adapters.cassette import CassetteStore


class _Inner(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def count_tokens(self, request: LLMRequest) -> int | None: ...


class SpendSink(Protocol):
    """Anything that wants to be told about a call that is actually going to be paid for.

    Both hooks fire only on the path that reaches a provider. A cassette hit costs nothing, so
    it is neither checked against a cap nor charged to one — which is what lets an interrupted
    recording replay everything it already paid for even when the budget is spent.
    """

    def before(self, request: LLMRequest) -> None:
        """Called before the call is made. Raise to refuse it; nothing has been spent yet."""
        ...

    def note(self, request: LLMRequest, response: LLMResponse) -> Decimal | None: ...


@dataclass
class RecordingAdapter:
    """Wraps a live (or simulated) adapter with a cassette store.

    ``reuse`` can be turned off for the live confirmation run, where SPEC.md section 6 step 5
    requires every call to be fresh so the run really checks the simulator rather than
    replaying it.
    """

    inner: _Inner
    store: CassetteStore
    origin: str = "live"
    reuse: bool = True
    on_spend: SpendSink | None = None
    reused_count: int = field(default=0, init=False)
    recorded_count: int = field(default=0, init=False)

    @property
    def name(self) -> str:
        return self.inner.name

    async def complete(self, request: LLMRequest) -> LLMResponse:
        key = request.cassette_key()
        if self.reuse:
            cassette = self.store.get(key)
            if cassette is not None:
                self.reused_count += 1
                return cassette.to_response(reused=True)

        if self.on_spend is not None:
            self.on_spend.before(request)
        response = await self.inner.complete(request)
        self.store.record(request, response, origin=self.origin)
        self.recorded_count += 1
        if self.on_spend is not None:
            self.on_spend.note(request, response)
        return response

    async def count_tokens(self, request: LLMRequest) -> int | None:
        return await self.inner.count_tokens(request)
