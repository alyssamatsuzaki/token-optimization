"""Anthropic Messages API adapter (SPEC.md 7.1).

Explicit ``cache_control`` breakpoints rather than automatic caching, because the product's
whole argument about prompt order is that *where* the breakpoint sits changes the bill, and an
automatically managed breakpoint would hide the thing being measured.

https://platform.claude.com/docs/en/api/messages — checked 2026-09-11.
https://platform.claude.com/docs/en/build-with-claude/prompt-caching — checked 2026-09-11.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from tokop.adapters.base import AdapterError, Block, LLMRequest, LLMResponse
from tokop.core.usage import normalize_anthropic

API_VERSION = "2023-06-01"
MAX_ATTEMPTS = 3
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def _content_blocks(blocks: list[Block]) -> list[dict[str, Any]]:
    """Render blocks, attaching a ``cache_control`` breakpoint where one is marked.

    At most four breakpoints are allowed per request and the cache key is an exact prefix over
    tools, then system, then messages, so the caller's block order is preserved exactly.
    """
    out: list[dict[str, Any]] = []
    for block in blocks:
        entry: dict[str, Any] = {"type": "text", "text": block.text}
        if block.cache is not None:
            entry["cache_control"] = {"type": "ephemeral", "ttl": block.cache}
        out.append(entry)
    return out


def build_payload(request: LLMRequest) -> dict[str, Any]:
    """The Messages API body for a request."""
    breakpoints = sum(1 for b in request.system if b.cache) + sum(
        1 for m in request.messages for b in m.blocks if b.cache
    )
    if breakpoints > 4:
        raise AdapterError(
            f"{breakpoints} cache breakpoints requested but the API allows at most 4. "
            "Put the breakpoint at the end of the static prefix instead of on every block."
        )
    payload: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": [
            {"role": m.role, "content": _content_blocks(m.blocks)} for m in request.messages
        ],
    }
    if request.system:
        payload["system"] = _content_blocks(request.system)
    if request.tools:
        payload["tools"] = request.tools
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.stop_sequences:
        payload["stop_sequences"] = request.stop_sequences
    return payload


def extract_text(raw: dict[str, Any]) -> str:
    """Concatenate the text blocks of a response, ignoring thinking and tool-use blocks."""
    content = raw.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class AnthropicAdapter:
    """Calls the Messages API over httpx.

    The official SDK is a dependency of this project and is a fine client, but the wire format
    is small and stable, and going direct keeps the recorded raw payload byte-identical to what
    the API returned — which is what the cassettes are for.
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        client: httpx.AsyncClient | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if not api_key:
            raise AdapterError("the Anthropic adapter needs an API key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._max_attempts = max_attempts

    def _headers(self) -> dict[str, str]:
        # The key goes in a header and is never logged, echoed, or written to a cassette
        # (SPEC.md non-negotiable 7).
        return {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(120.0))
        try:
            return await client.post(
                f"{self._base_url}{path}", json=payload, headers=self._headers()
            )
        finally:
            if self._client is None:
                await client.aclose()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """One call, with bounded retries.

        Every attempt is its own traced span with its own cost: a 429 that is retried has
        already been paid for in wall-clock time, and a 500 after partial generation can still
        be billed, so the caller must be able to see how many attempts a task really took
        (SPEC.md 7.1).
        """
        payload = build_payload(request)
        last_error: str | None = None
        for attempt in range(1, self._max_attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._post("/v1/messages", payload)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == self._max_attempts:
                    break
                await self._backoff(attempt)
                continue
            latency_ms = (time.perf_counter() - started) * 1000

            if response.status_code == 200:
                raw = response.json()
                usage = normalize_anthropic(raw.get("usage") or {})
                return LLMResponse(
                    text=extract_text(raw),
                    usage=usage,
                    model=raw.get("model", request.model),
                    provider=self.name,
                    latency_ms=latency_ms,
                    stop_reason=raw.get("stop_reason"),
                    raw=raw,
                    attempt=attempt,
                )

            body = response.text[:500]
            last_error = f"HTTP {response.status_code}: {body}"
            if response.status_code not in RETRYABLE_STATUS or attempt == self._max_attempts:
                break
            await self._backoff(attempt, response)

        raise AdapterError(
            f"Anthropic call failed after {self._max_attempts} attempt(s): {last_error}"
        )

    async def _backoff(self, attempt: int, response: httpx.Response | None = None) -> None:
        """Exponential backoff with jitter, honouring Retry-After when the server sends one."""
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    await asyncio.sleep(min(float(retry_after), 60.0))
                    return
                except ValueError:
                    pass
        delay = min(2.0**attempt, 30.0) * (0.5 + random.random())
        await asyncio.sleep(delay)

    async def count_tokens(self, request: LLMRequest) -> int | None:
        """Exact input token count from the count_tokens endpoint.

        https://platform.claude.com/docs/en/build-with-claude/token-counting — 2026-09-11.
        """
        payload = build_payload(request)
        payload.pop("max_tokens", None)
        response = await self._post("/v1/messages/count_tokens", payload)
        if response.status_code != 200:
            raise AdapterError(
                f"count_tokens failed with HTTP {response.status_code}: {response.text[:300]}"
            )
        data = response.json()
        tokens = data.get("input_tokens")
        return int(tokens) if isinstance(tokens, int | float) else None


def prewarm_request(request: LLMRequest) -> LLMRequest:
    """Turn a request into a cache pre-warm: ``max_tokens: 0``, same prefix.

    A cache entry only becomes available once the first response begins, so firing the whole
    split in parallel makes every first request a miss. One pre-warm call, awaited, fixes that.
    The API rejects ``max_tokens: 0`` alongside streaming, extended thinking, structured
    outputs or a forced tool choice, none of which v1 uses.

    ``step`` is cleared. A pre-warm is about a prefix, not about who is going to send it, and two
    steps of a graph sharing a prefix should share one warm call rather than pay for two — which
    is also why the runner dedups these by (model, prefix).
    """
    return request.model_copy(update={"max_tokens": 0, "prewarm": True, "step": ""})
