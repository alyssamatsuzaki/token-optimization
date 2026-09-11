"""Adapter contract, cassettes, and the two M2 acceptance checks (SPEC.md section 11).

The acceptance checks are:
  * a record-and-replay round trip reproduces identical metrics;
  * a recording against a fake provider that is killed halfway and restarted sends no request
    twice.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tokop.adapters.anthropic import build_payload, extract_text, prewarm_request
from tokop.adapters.base import (
    AdapterError,
    Block,
    CassetteMiss,
    LLMRequest,
    blocks_of,
    user_message,
)
from tokop.adapters.cassette import CassetteStore, ReplayAdapter
from tokop.adapters.openai_compatible import build_payload as oai_payload
from tokop.adapters.openai_compatible import extract_stop_reason
from tokop.adapters.openai_compatible import extract_text as oai_text
from tokop.adapters.recording import RecordingAdapter
from tokop.adapters.simulated import ModelProfile, SimulatedAdapter
from tokop.core.pricing import ModelPrice, PriceProvenance, PriceSnapshot

HANDBOOK = "Returns policy handbook. " * 400  # comfortably over every minimum
PROFILES = {
    "sim-frontier": ModelProfile("sim-frontier", min_cacheable_tokens=512, ms_per_output_token=9),
    "sim-cheap": ModelProfile(
        "sim-cheap", min_cacheable_tokens=4096, tokenizer_generation="previous"
    ),
}


def make_request(question: str, model: str = "sim-frontier", cache: bool = True) -> LLMRequest:
    return LLMRequest(
        provider="simulated",
        model=model,
        system=blocks_of(
            "You answer from the handbook.", HANDBOOK, cache_last="5m" if cache else None
        ),
        messages=[user_message(question)],
        max_tokens=200,
    )


def echo_responder(request: LLMRequest) -> str:
    return f"Final answer: {request.messages[-1].text.upper()}"


PRICE = ModelPrice(
    model_id="sim-frontier",
    provider="simulated",
    input=Decimal("5"),
    output=Decimal("25"),
    cache_write_5m=Decimal("6.25"),
    cache_read=Decimal("0.50"),
    provenance=PriceProvenance(source_url="https://example.invalid", retrieved=date(2026, 9, 11)),
)


class TestRequestCanonicalization:
    def test_the_key_is_stable_across_identical_requests(self) -> None:
        assert make_request("a").cassette_key() == make_request("a").cassette_key()

    def test_the_key_changes_with_any_part_of_the_request(self) -> None:
        base = make_request("a").cassette_key()
        assert make_request("b").cassette_key() != base
        assert make_request("a", model="sim-cheap").cassette_key() != base
        assert make_request("a").model_copy(update={"max_tokens": 201}).cassette_key() != base

    def test_moving_the_breakpoint_changes_the_key(self) -> None:
        """An unstable key order would break provider caches too, so this matters twice."""
        assert (
            make_request("a", cache=True).cassette_key()
            != make_request("a", cache=False).cassette_key()
        )

    def test_static_prefix_stops_at_the_last_breakpoint(self) -> None:
        request = LLMRequest(
            provider="simulated",
            model="sim-frontier",
            system=[
                Block(text="static one"),
                Block(text="static two", cache="5m"),
                Block(text="volatile"),
            ],
            messages=[user_message("q")],
        )
        assert request.static_prefix_text == "static one\n\nstatic two"
        assert "volatile" not in request.static_prefix_text

    def test_no_breakpoint_means_no_cacheable_prefix(self) -> None:
        assert make_request("a", cache=False).static_prefix_text == ""

    def test_prompt_hash_ignores_max_tokens(self) -> None:
        a = make_request("a")
        b = a.model_copy(update={"max_tokens": 999})
        assert a.prompt_hash() == b.prompt_hash()
        assert a.cassette_key() != b.cassette_key()


class TestAnthropicPayload:
    def test_breakpoints_render_as_cache_control(self) -> None:
        payload = build_payload(make_request("q"))
        assert payload["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
        assert "cache_control" not in payload["system"][0]

    def test_more_than_four_breakpoints_is_refused_with_advice(self) -> None:
        request = LLMRequest(
            provider="anthropic",
            model="claude-opus-5",
            system=[Block(text=f"b{i}", cache="5m") for i in range(5)],
            messages=[user_message("q")],
        )
        with pytest.raises(AdapterError, match="at most 4"):
            build_payload(request)

    def test_text_extraction_ignores_thinking_and_tool_blocks(self) -> None:
        raw = {
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "visible"},
                {"type": "tool_use", "name": "x"},
            ]
        }
        assert extract_text(raw) == "visible"

    def test_prewarm_sets_max_tokens_zero_and_is_flagged(self) -> None:
        warm = prewarm_request(make_request("q"))
        assert warm.max_tokens == 0
        assert warm.prewarm is True
        assert warm.static_prefix_text == make_request("q").static_prefix_text


class TestOpenAIPayload:
    def test_system_blocks_are_flattened_into_one_system_message(self) -> None:
        payload = oai_payload(make_request("q"))
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"

    def test_text_and_finish_reason_extraction(self) -> None:
        raw = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
        assert oai_text(raw) == "hello"
        assert extract_stop_reason(raw) == "stop"

    def test_empty_choices_yield_empty_text(self) -> None:
        assert oai_text({"choices": []}) == ""
        assert extract_stop_reason({"choices": []}) is None


class TestSimulatedProvider:
    async def test_first_call_writes_the_cache_and_the_second_reads_it(self) -> None:
        adapter = SimulatedAdapter(PROFILES, echo_responder)
        first = await adapter.complete(make_request("q1"))
        second = await adapter.complete(make_request("q2"))
        assert first.usage.cache_write_5m > 0
        assert first.usage.cache_read == 0
        assert second.usage.cache_read == first.usage.cache_write_5m
        assert second.usage.cache_write_5m == 0

    async def test_a_prefix_below_the_minimum_is_silently_uncached(self) -> None:
        """Appendix B: no error, and both cache fields read 0."""
        short = LLMRequest(
            provider="simulated",
            model="sim-cheap",  # 4,096-token minimum
            system=blocks_of("A short system prompt.", cache_last="5m"),
            messages=[user_message("q")],
            max_tokens=50,
        )
        adapter = SimulatedAdapter(PROFILES, echo_responder)
        response = await adapter.complete(short)
        assert response.usage.cache_write_5m == 0
        assert response.usage.cache_read == 0
        assert response.usage.input_uncached > 0

    async def test_the_newer_tokenizer_counts_about_thirty_percent_higher(self) -> None:
        adapter = SimulatedAdapter(PROFILES, echo_responder)
        newer = await adapter.complete(make_request("q", model="sim-frontier", cache=False))
        adapter.reset_cache()
        cheaper = await adapter.complete(make_request("q", model="sim-cheap", cache=False))
        ratio = newer.usage.total_input / cheaper.usage.total_input
        assert 1.25 <= ratio <= 1.35

    async def test_a_prewarm_bills_no_output(self) -> None:
        adapter = SimulatedAdapter(PROFILES, echo_responder)
        response = await adapter.complete(prewarm_request(make_request("q")))
        assert response.usage.output_visible == 0
        assert response.usage.cache_write_5m > 0
        assert response.stop_reason == "max_tokens"

    async def test_usage_is_anthropic_shaped_so_the_real_normalizer_runs(self) -> None:
        adapter = SimulatedAdapter(PROFILES, echo_responder)
        response = await adapter.complete(make_request("q"))
        assert set(response.usage.raw) >= {
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        }
        assert response.raw["_tokop_origin"] == "simulated"

    async def test_latency_is_deterministic_for_the_same_request(self) -> None:
        a = SimulatedAdapter(PROFILES, echo_responder)
        b = SimulatedAdapter(PROFILES, echo_responder)
        assert (await a.complete(make_request("q"))).latency_ms == (
            await b.complete(make_request("q"))
        ).latency_ms

    async def test_an_unknown_model_is_refused_by_name(self) -> None:
        adapter = SimulatedAdapter(PROFILES, echo_responder)
        with pytest.raises(AdapterError, match="no simulated profile"):
            await adapter.complete(make_request("q", model="not-a-model"))

    async def test_count_tokens_reports_the_whole_input(self) -> None:
        adapter = SimulatedAdapter(PROFILES, echo_responder)
        request = make_request("q")
        counted = await adapter.count_tokens(request)
        response = await adapter.complete(request)
        assert counted == response.usage.total_input


class TestCassetteStore:
    def test_a_recorded_call_round_trips(self, tmp_path: Path) -> None:
        store = CassetteStore(tmp_path)
        request = make_request("q")
        import asyncio

        response = asyncio.run(SimulatedAdapter(PROFILES, echo_responder).complete(request))
        store.record(request, response, origin="simulated")
        loaded = store.get(request.cassette_key())
        assert loaded is not None
        assert loaded.response_text == response.text
        assert loaded.usage.total == response.usage.total
        assert loaded.origin == "simulated"

    def test_a_miss_in_replay_names_the_request_and_never_calls_out(self, tmp_path: Path) -> None:
        import asyncio

        replay = ReplayAdapter(CassetteStore(tmp_path))
        with pytest.raises(CassetteMiss) as exc:
            asyncio.run(replay.complete(make_request("never recorded")))
        assert "never falls back to a live call" in str(exc.value)
        assert "simulated/sim-frontier" in str(exc.value)

    def test_replayed_calls_are_marked_reused(self, tmp_path: Path) -> None:
        import asyncio

        store = CassetteStore(tmp_path)
        request = make_request("q")
        response = asyncio.run(SimulatedAdapter(PROFILES, echo_responder).complete(request))
        store.record(request, response, origin="simulated")
        replayed = asyncio.run(ReplayAdapter(store).complete(request))
        assert replayed.reused is True
        assert response.reused is False

    def test_stats_report_origin_and_recording_window(self, tmp_path: Path) -> None:
        import asyncio

        store = CassetteStore(tmp_path)
        adapter = SimulatedAdapter(PROFILES, echo_responder)
        for q in ("a", "b", "c"):
            request = make_request(q)
            store.record(request, asyncio.run(adapter.complete(request)), origin="simulated")
        stats = store.stats()
        assert stats["count"] == 3
        assert stats["origins"] == {"simulated": 3}
        assert stats["recorded_from"] is not None

    def test_a_partial_write_never_replaces_a_good_cassette(self, tmp_path: Path) -> None:
        """Writes go through a temp file and an atomic rename."""
        import asyncio

        store = CassetteStore(tmp_path)
        request = make_request("q")
        response = asyncio.run(SimulatedAdapter(PROFILES, echo_responder).complete(request))
        store.record(request, response, origin="simulated")
        path = store.path_for(request.cassette_key())
        original = path.read_text()
        assert json.loads(original)["key"] == request.cassette_key()
        assert not list(path.parent.glob("*.tmp"))


class TestRoundTripMetrics:
    """M2 acceptance: record and replay reproduce identical metrics."""

    async def test_metrics_are_bit_identical_across_a_round_trip(self, tmp_path: Path) -> None:
        store = CassetteStore(tmp_path)
        recorder = RecordingAdapter(
            SimulatedAdapter(PROFILES, echo_responder), store, origin="simulated"
        )
        requests = [make_request(f"question {i}") for i in range(12)]
        snapshot = PriceSnapshot.of([PRICE], date(2026, 9, 11))

        recorded = [await recorder.complete(r) for r in requests]
        recorded_costs = [snapshot.cost("sim-frontier", r.usage).total for r in recorded]
        recorded_tokens = [r.usage.total for r in recorded]

        replay = ReplayAdapter(store)
        replayed = [await replay.complete(r) for r in requests]
        replayed_costs = [snapshot.cost("sim-frontier", r.usage).total for r in replayed]
        replayed_tokens = [r.usage.total for r in replayed]

        assert recorded_tokens == replayed_tokens
        assert recorded_costs == replayed_costs
        assert sum(recorded_costs) == sum(replayed_costs)
        # Bucket by bucket, not just the totals.
        for rec, rep in zip(recorded, replayed, strict=True):
            assert rec.usage.model_dump(exclude={"raw"}) == rep.usage.model_dump(exclude={"raw"})

    async def test_reuse_returns_the_cassette_without_calling_the_provider(
        self, tmp_path: Path
    ) -> None:
        inner = SimulatedAdapter(PROFILES, echo_responder)
        recorder = RecordingAdapter(inner, CassetteStore(tmp_path), origin="simulated")
        request = make_request("q")
        await recorder.complete(request)
        assert inner.calls == 1
        again = await recorder.complete(request)
        assert inner.calls == 1  # not called a second time
        assert again.reused is True
        assert recorder.reused_count == 1

    async def test_reuse_can_be_turned_off_for_the_live_confirmation_run(
        self, tmp_path: Path
    ) -> None:
        """SPEC.md section 6 step 5 needs every call fresh so the run checks the simulator."""
        inner = SimulatedAdapter(PROFILES, echo_responder)
        recorder = RecordingAdapter(inner, CassetteStore(tmp_path), origin="simulated", reuse=False)
        request = make_request("q")
        await recorder.complete(request)
        await recorder.complete(request)
        assert inner.calls == 2
        assert recorder.reused_count == 0


RESUME_SCRIPT = r"""
import asyncio, json, sys
from pathlib import Path
from tokop.adapters.base import LLMRequest, blocks_of, user_message
from tokop.adapters.cassette import CassetteStore
from tokop.adapters.recording import RecordingAdapter
from tokop.adapters.simulated import ModelProfile, SimulatedAdapter

store_dir, log_path, stop_after = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
HANDBOOK = "Returns policy handbook. " * 400
PROFILES = {"sim-frontier": ModelProfile("sim-frontier", min_cacheable_tokens=512)}

def responder(request):
    # Every provider call appends to the log. This is what proves no request is sent twice.
    with log_path.open("a") as fh:
        fh.write(request.messages[-1].text + "\n")
    return "Final answer: ok"

def make(i):
    return LLMRequest(
        provider="simulated", model="sim-frontier",
        system=blocks_of("You answer from the handbook.", HANDBOOK, cache_last="5m"),
        messages=[user_message(f"question {i}")], max_tokens=200,
    )

async def main():
    recorder = RecordingAdapter(SimulatedAdapter(PROFILES, responder), CassetteStore(store_dir),
                                origin="simulated")
    for i in range(10):
        if i == stop_after:
            # Simulate being killed: hard-exit without unwinding, mid-recording.
            import os; os._exit(137)
        await recorder.complete(make(i))

asyncio.run(main())
"""


class TestResume:
    """M2 acceptance: killed halfway and restarted, no request is sent twice."""

    def test_an_interrupted_recording_resumes_without_paying_twice(self, tmp_path: Path) -> None:
        store_dir = tmp_path / "cassettes"
        log = tmp_path / "provider-calls.log"
        script = tmp_path / "resume_run.py"
        script.write_text(RESUME_SCRIPT)

        # First run: dies hard after 4 calls, the way a Ctrl-C or an OOM kill would.
        first = subprocess.run(
            [sys.executable, str(script), str(store_dir), str(log), "4"],
            capture_output=True,
            text=True,
        )
        assert first.returncode == 137, first.stderr
        assert len(log.read_text().splitlines()) == 4
        assert len(CassetteStore(store_dir)) == 4

        # Second run: completes all 10. Only the 6 it had not recorded reach the provider.
        second = subprocess.run(
            [sys.executable, str(script), str(store_dir), str(log), "99"],
            capture_output=True,
            text=True,
        )
        assert second.returncode == 0, second.stderr

        calls = log.read_text().splitlines()
        assert len(calls) == 10, f"expected 10 provider calls in total, got {len(calls)}"
        assert len(set(calls)) == 10, "a request was sent to the provider twice"
        assert len(CassetteStore(store_dir)) == 10
