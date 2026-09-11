"""Which adapter a recording actually runs on.

The recorder is the only thing in the build that can spend money, so what it wraps matters more
than what the docstrings say. These tests build the adapter stack and look at it. None of them
makes a call: constructing an :class:`AnthropicAdapter` opens no socket.

DECISIONS.md D24 records what is *not* covered here: this path has never been exercised against
a real provider, because this build had no credentials and no record budget.
"""

from __future__ import annotations

from datetime import date

import pytest

from tokop.adapters.anthropic import AnthropicAdapter
from tokop.adapters.base import AdapterError
from tokop.adapters.recording import RecordingAdapter
from tokop.adapters.simulated import SimulatedAdapter
from tokop.core.registry import load_registry
from tokop.paths import repo_root
from tokop.recorder import Recorder
from tokop.workloads.demo.dataset import build as build_dataset
from tokop.workloads.spec import load_workload


@pytest.fixture(scope="module")
def registry():
    return load_registry()


@pytest.fixture(scope="module")
def workload():
    return load_workload(repo_root() / "data/demo/workload.yaml")


@pytest.fixture(scope="module")
def bundle():
    return build_dataset()


def make_recorder(workload, registry, bundle, tmp_path, provider: str, origin: str) -> Recorder:
    return Recorder(
        workload,
        registry,
        bundle,
        registry.snapshot(list(registry.roles.values()), date(2026, 9, 11)),
        tmp_path / "cassettes",
        provider=provider,
        origin=origin,
    )


class TestAdapterSelection:
    def test_the_simulated_provider_builds_the_in_process_responder(
        self, workload, registry, bundle, tmp_path
    ) -> None:
        recorder = make_recorder(workload, registry, bundle, tmp_path, "simulated", "simulated")
        adapter = recorder._adapter("B2")
        assert isinstance(adapter, RecordingAdapter)
        assert isinstance(adapter.inner, SimulatedAdapter)
        assert adapter.origin == "simulated"

    def test_a_real_provider_builds_its_http_adapter(
        self, workload, registry, bundle, tmp_path, monkeypatch
    ) -> None:
        """The live path is wired: with a key present the recorder wraps the real adapter.

        No call is made here. What this pins down is that a live recording would not silently
        run on the simulator and report spend that never happened.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
        recorder = make_recorder(workload, registry, bundle, tmp_path, "anthropic", "live")
        adapter = recorder._adapter("B2")
        assert isinstance(adapter, RecordingAdapter)
        assert isinstance(adapter.inner, AnthropicAdapter)
        assert adapter.origin == "live"

    def test_a_missing_key_is_refused_by_name(
        self, workload, registry, bundle, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        recorder = make_recorder(workload, registry, bundle, tmp_path, "anthropic", "live")
        with pytest.raises(AdapterError) as exc:
            recorder._adapter("B2")
        assert "ANTHROPIC_API_KEY" in str(exc.value)
