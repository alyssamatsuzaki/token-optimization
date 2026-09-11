"""Builds the right adapter for a provider, given the mode and the configured keys.

The mode decides the shape of the stack:

* **replay** — every call is served from cassettes. A missing cassette raises; nothing is ever
  sent. This is what the public demo and the test suite run on.
* **live** — the provider adapter, wrapped in a recorder so identical requests reuse what has
  already been paid for.

The simulated provider is refused in live mode. It exists to build fixtures and to test the
recorder, and a run that thinks it called a provider but did not would poison every number
downstream of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from tokop.adapters.anthropic import AnthropicAdapter
from tokop.adapters.base import AdapterError, LLMRequest, LLMResponse
from tokop.adapters.cassette import CassetteStore, ReplayAdapter
from tokop.adapters.openai_compatible import OpenAICompatibleAdapter
from tokop.adapters.recording import RecordingAdapter
from tokop.adapters.simulated import ModelProfile, SimulatedAdapter
from tokop.core.registry import Registry
from tokop.settings import Mode, Settings


class AnyAdapter(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def count_tokens(self, request: LLMRequest) -> int | None: ...


def simulated_profiles(registry: Registry, model_ids: list[str]) -> dict[str, ModelProfile]:
    """Simulated profiles built from the real registry, so cache minimums and tokenizer
    generations match the models being stood in for."""
    profiles: dict[str, ModelProfile] = {}
    for model_id in model_ids:
        entry = registry.model(model_id)
        profiles[model_id] = ModelProfile(
            model_id=model_id,
            min_cacheable_tokens=entry.min_cacheable_tokens or 1024,
            tokenizer_generation=entry.tokenizer_generation or "newer",
            ms_per_output_token={"claude-opus-5": 11.0, "claude-sonnet-5": 6.5}.get(model_id, 3.5),
            base_latency_ms={"claude-opus-5": 780.0, "claude-sonnet-5": 520.0}.get(model_id, 310.0),
        )
    return profiles


def build_live_adapter(provider_name: str, registry: Registry, settings: Settings) -> AnyAdapter:
    """The provider adapter for a live call. Raises with a fixable message when it cannot."""
    provider = registry.provider(provider_name)
    if provider.test_only:
        raise AdapterError(
            f"{provider_name!r} is a test-only provider and cannot be used in live mode. "
            "It makes no network call, so a live run against it would report spend that never "
            "happened."
        )
    if not provider.enabled:
        reason = provider.unconfirmed_reason or "it is disabled in config/providers.yaml"
        raise AdapterError(f"provider {provider_name!r} is not enabled: {reason}")
    if provider.base_url is None:
        raise AdapterError(f"provider {provider_name!r} has no base URL")

    api_key = settings.api_key_for(provider.api_key_env)
    if not api_key:
        raise AdapterError(
            f"provider {provider_name!r} needs {provider.api_key_env} in the environment. "
            "Keys stay on the server and are never sent to the browser."
        )

    if provider.adapter == "anthropic":
        return AnthropicAdapter(api_key=api_key, base_url=provider.base_url)
    if provider.adapter == "openai_compatible":
        return OpenAICompatibleAdapter(
            api_key=api_key,
            base_url=provider.base_url,
            name=provider_name,
            usage_mapping=provider.usage_mapping,
        )
    raise AdapterError(f"unknown adapter kind {provider.adapter!r} for provider {provider_name!r}")


def build_adapter(
    provider_name: str,
    registry: Registry,
    settings: Settings,
    cassette_dir: Path,
    *,
    reuse: bool = True,
    simulated_responder: object | None = None,
) -> AnyAdapter:
    """The adapter stack for the current mode."""
    store = CassetteStore(cassette_dir)
    if settings.mode == Mode.REPLAY:
        return ReplayAdapter(store, name=provider_name)

    if provider_name == "simulated":
        if simulated_responder is None:
            raise AdapterError("the simulated provider needs a responder")
        profiles = simulated_profiles(registry, list(registry.models))
        inner: AnyAdapter = SimulatedAdapter(profiles, simulated_responder)  # type: ignore[arg-type]
        return RecordingAdapter(inner, store, origin="simulated", reuse=reuse)

    return RecordingAdapter(
        build_live_adapter(provider_name, registry, settings), store, origin="live", reuse=reuse
    )
