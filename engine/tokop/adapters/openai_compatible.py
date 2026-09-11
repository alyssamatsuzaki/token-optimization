"""OpenAI-compatible Chat Completions adapter (SPEC.md 7.1).

One adapter serves OpenAI, OpenRouter, Gemini's OpenAI-compatible endpoint and DeepSeek: they
differ in base URL, in how the key is presented, and — importantly — in how they report cache
usage. The usage mapping is selected per provider rather than assumed, because getting that
wrong is exactly the kind of silent error that makes a cost number wrong by 10x.

https://platform.openai.com/docs/api-reference/chat — checked 2026-09-11.
"""

from __future__ import annotations

import asyncio
import random
import time
from decimal import Decimal
from typing import Any

import httpx

from tokop.adapters.base import AdapterError, LLMRequest, LLMResponse
from tokop.core.usage import TokenUsage, normalize, normalize_openrouter

MAX_ATTEMPTS = 3
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def build_payload(request: LLMRequest) -> dict[str, Any]:
    """The Chat Completions body for a request.

    Chat Completions has no explicit cache breakpoints — caching is automatic on every provider
    in this family — so ``cache`` marks on blocks are informational here. They still shape the
    prompt *order*, which is what actually determines whether an automatic cache can hit.
    """
    messages: list[dict[str, Any]] = []
    system_text = request.system_text
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for message in request.messages:
        messages.append({"role": message.role, "content": message.text})
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_tokens,
    }
    if request.tools:
        payload["tools"] = request.tools
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.stop_sequences:
        payload["stop"] = request.stop_sequences
    return payload


def extract_text(raw: dict[str, Any]) -> str:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def extract_stop_reason(raw: dict[str, Any]) -> str | None:
    choices = raw.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        return str(reason) if reason is not None else None
    return None


class OpenAICompatibleAdapter:
    """Chat Completions against any compatible base URL."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        name: str = "openai",
        usage_mapping: str = "openai",
        client: httpx.AsyncClient | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if not api_key:
            raise AdapterError(f"the {name} adapter needs an API key")
        if not base_url:
            raise AdapterError(
                f"the {name} adapter needs a base URL. Providers whose base URL has not been "
                "confirmed against their own documentation stay `enabled: false`."
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.name = name
        self._usage_mapping = usage_mapping
        self._client = client
        self._max_attempts = max_attempts
        self._extra_headers = extra_headers or {}

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
            **self._extra_headers,
        }

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(120.0))
        try:
            return await client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=self._headers()
            )
        finally:
            if self._client is None:
                await client.aclose()

    def _normalize(self, raw: dict[str, Any]) -> tuple[TokenUsage, Decimal | None]:
        usage_raw = raw.get("usage") or {}
        if self._usage_mapping == "openrouter":
            return normalize_openrouter(usage_raw)
        return normalize(self._usage_mapping, usage_raw), None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload = build_payload(request)
        last_error: str | None = None
        for attempt in range(1, self._max_attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._post(payload)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == self._max_attempts:
                    break
                await self._backoff(attempt)
                continue
            latency_ms = (time.perf_counter() - started) * 1000

            if response.status_code == 200:
                raw = response.json()
                usage, provider_cost = self._normalize(raw)
                return LLMResponse(
                    text=extract_text(raw),
                    usage=usage,
                    model=raw.get("model", request.model),
                    provider=self.name,
                    latency_ms=latency_ms,
                    stop_reason=extract_stop_reason(raw),
                    raw=raw,
                    provider_cost_usd=provider_cost,
                    attempt=attempt,
                )

            last_error = f"HTTP {response.status_code}: {response.text[:500]}"
            if response.status_code not in RETRYABLE_STATUS or attempt == self._max_attempts:
                break
            await self._backoff(attempt, response)

        raise AdapterError(
            f"{self.name} call failed after {self._max_attempts} attempt(s): {last_error}"
        )

    async def _backoff(self, attempt: int, response: httpx.Response | None = None) -> None:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    await asyncio.sleep(min(float(retry_after), 60.0))
                    return
                except ValueError:
                    pass
        await asyncio.sleep(min(2.0**attempt, 30.0) * (0.5 + random.random()))

    async def count_tokens(self, request: LLMRequest) -> int | None:
        """No counting endpoint in this family.

        Returns ``None`` so the caller falls back to an estimate that names its method, rather
        than to a number that looks exact and is not (SPEC.md non-negotiable 2).
        """
        return None
