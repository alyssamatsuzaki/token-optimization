"""The runner, the ledger, and the M3 acceptance check.

The acceptance check is: **B0 metrics on test fixtures match a hand-computed fixture.** The
hand computation here is deliberately independent — it reads the token counts out of the
recorded cassettes and multiplies them by Anthropic's published rates with plain arithmetic,
without calling ``compute_cost``. If the engine and the arithmetic ever disagree, one of them
is wrong and the test says so.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from tokop.adapters.cassette import CassetteStore
from tokop.adapters.factory import simulated_profiles
from tokop.adapters.recording import RecordingAdapter
from tokop.adapters.simulated import SimulatedAdapter
from tokop.core.registry import load_registry
from tokop.db import Call, GradeRow, Run, delete_run_content, make_engine, session_scope
from tokop.paths import fixtures_dir, repo_root
from tokop.workloads.demo.dataset import build as build_dataset
from tokop.workloads.demo.responder import ACCURACY, DemoResponder, TierProfile
from tokop.workloads.runner import Runner, persist
from tokop.workloads.spec import WorkloadError, load_workload

OPUS = "claude-opus-5"
# Anthropic's published rates, verified 2026-09-11. Repeated here on purpose: a hand
# computation that imported the registry would not be an independent check.
RATE_INPUT = Decimal("5")
RATE_OUTPUT = Decimal("25")
RATE_CACHE_WRITE_5M = Decimal("6.25")
RATE_CACHE_READ = Decimal("0.50")
MILLION = Decimal(1_000_000)


@pytest.fixture(scope="module")
def registry():
    return load_registry()


@pytest.fixture(scope="module")
def workload():
    return load_workload(repo_root() / "data/demo/workload.yaml")


@pytest.fixture(scope="module")
def bundle():
    return build_dataset()


@pytest.fixture(scope="module")
def fixture_manifest() -> dict:
    path = fixtures_dir() / "test" / "manifest.json"
    if not path.exists():
        pytest.skip("fixtures/test/ has not been built; run `tokop build-test-fixtures`")
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def fixture_store() -> CassetteStore:
    root = fixtures_dir() / "test" / "cassettes"
    if not root.exists():
        pytest.skip("fixtures/test/ has not been built")
    return CassetteStore(root)


def run_pipeline(workload, registry, bundle, pipeline_id, items, tmp_path, split="test"):
    pipeline = workload.pipeline(pipeline_id)
    tiers = {r: TierProfile(registry.roles[r], r) for r in ("cheap", "mid", "frontier")}
    responder = DemoResponder(
        list(bundle.items), bundle.handbook, tiers, pipeline_id, pipeline.output_contract
    )
    adapter = RecordingAdapter(
        SimulatedAdapter(simulated_profiles(registry, list(registry.models)), responder),
        CassetteStore(Path(tmp_path) / "cassettes"),
        origin="simulated",
    )
    snapshot = registry.snapshot(list(registry.roles.values()), date(2026, 9, 11))
    runner = Runner(
        workload,
        registry,
        snapshot,
        adapter,
        provider="simulated",
        handbook=bundle.handbook,
        origin="simulated",
    )
    result = asyncio.run(runner.run_pipeline(pipeline_id, items, split=split))
    return result, snapshot


class TestHandComputedB0:
    """M3 acceptance: B0's metrics reconcile with arithmetic done outside the engine."""

    def test_b0_cost_matches_hand_arithmetic(self, workload, registry, bundle, tmp_path) -> None:
        items = list(bundle.test)[:40]
        result, _ = run_pipeline(workload, registry, bundle, "B0", items, tmp_path)

        by_hand = Decimal(0)
        for task in result.tasks:
            for _, response, _, _ in task.calls:
                usage = response.usage
                by_hand += (
                    Decimal(usage.input_uncached) * RATE_INPUT
                    + Decimal(usage.cache_write_5m) * RATE_CACHE_WRITE_5M
                    + Decimal(usage.cache_read) * RATE_CACHE_READ
                    + Decimal(usage.total_output) * RATE_OUTPUT
                ) / MILLION
        assert result.total_cost == by_hand

    def test_b0_has_no_cacheable_prefix_at_all(self, workload, registry, bundle, tmp_path) -> None:
        """The point of B0: the handbook sits after the question, so nothing can be cached."""
        items = list(bundle.test)[:20]
        result, _ = run_pipeline(workload, registry, bundle, "B0", items, tmp_path)
        assert result.prewarm_calls == 0
        for task in result.tasks:
            for request, response, _, _ in task.calls:
                assert request.static_prefix_text == ""
                assert response.usage.cache_read == 0
                assert response.usage.cache_write_5m == 0
                assert response.usage.input_uncached == response.usage.total_input

    def test_b1_caches_what_b0_cannot(self, workload, registry, bundle, tmp_path) -> None:
        items = list(bundle.test)[:20]
        b0, _ = run_pipeline(workload, registry, bundle, "B0", items, tmp_path / "a")
        b1, _ = run_pipeline(workload, registry, bundle, "B1", items, tmp_path / "b")
        b1_reads = sum(r.usage.cache_read for t in b1.tasks for _, r, _, _ in t.calls)
        assert b1_reads > 0
        assert b1.prewarm_calls == 1
        # Same words, same model, same cap: only the order changed, and it is much cheaper.
        assert b1.total_cost < b0.total_cost

    def test_b2_output_contract_cuts_output_tokens(
        self, workload, registry, bundle, tmp_path
    ) -> None:
        items = list(bundle.test)[:20]
        b1, _ = run_pipeline(workload, registry, bundle, "B1", items, tmp_path / "a")
        b2, _ = run_pipeline(workload, registry, bundle, "B2", items, tmp_path / "b")
        b1_out = sum(r.usage.total_output for t in b1.tasks for _, r, _, _ in t.calls)
        b2_out = sum(r.usage.total_output for t in b2.tasks for _, r, _, _ in t.calls)
        assert b2_out < b1_out / 2


class TestFixtureRuns:
    """The committed fixture set, read back the way the app reads it."""

    def test_manifest_records_the_expected_runs(self, fixture_manifest) -> None:
        keys = {(r["pipeline"], r["split"], r["tier"]) for r in fixture_manifest["runs"]}
        assert ("B0", "test", "frontier") in keys
        assert ("B1", "test", "frontier") in keys
        for tier in ("cheap", "mid", "frontier"):
            assert ("B2", "calibration", tier) in keys
            assert ("B2", "test", tier) in keys

    def test_manifest_is_marked_simulated_not_recorded(self, fixture_manifest) -> None:
        """Nothing may present simulated fixtures as a recording (DECISIONS.md D1)."""
        assert fixture_manifest["origin"] == "simulated"
        assert "not a recording" in fixture_manifest["note"]
        assert all(r["origin"] == "simulated" for r in fixture_manifest["runs"])

    def test_the_sanity_and_difficulty_gate_would_have_passed(self, fixture_manifest) -> None:
        by_key = {(r["pipeline"], r["split"], r["tier"]): r for r in fixture_manifest["runs"]}
        frontier = by_key[("B2", "calibration", "frontier")]["accuracy"]
        cheap = by_key[("B2", "calibration", "cheap")]["accuracy"]
        assert frontier >= 0.90, "every answer is in the handbook; below 90% means a bug"
        assert frontier - cheap >= 0.03, "the set is too easy to show escalation"

    def test_every_cassette_carries_raw_provider_usage(self, fixture_store) -> None:
        cassettes = fixture_store.all()
        assert len(cassettes) > 1000
        for cassette in cassettes[:200]:
            assert cassette.raw_usage
            assert "input_tokens" in cassette.raw_usage

    def test_blobs_deduplicate_the_handbook(self, fixture_store) -> None:
        """The handbook appears in every request; it is stored once.

        Asserted as the dedup property rather than as a blob count. A blob is any block longer
        than the threshold, and the judge prompts added by UPGRADE_V3.md U1 carry the answer
        under review — B0's uncapped answers are essays, and a long *unique* string correctly
        gets a blob of its own. A cap on the total would have been a cap on how many long
        strings the fixtures may contain, which is not what this test is about.
        """
        from tokop.paths import repo_root

        handbook = (repo_root() / "data/demo/handbook.md").read_text()
        blobs = [blob.read_text() for blob in fixture_store.blob_dir.glob("*.txt")]
        carrying = [text for text in blobs if handbook in text]
        # One per distinct wrapper: B0 opens "Here is the handbook material…" and the others
        # open "Handbook:". Anything more means the same text is being stored per call.
        assert 1 <= len(carrying) <= 3, (
            f"the handbook is stored in {len(carrying)} distinct blobs across "
            f"{len(fixture_store)} calls"
        )
        assert len(fixture_store) > 1000
        # Still far fewer blobs than calls: the big shared prefix really is shared.
        assert fixture_store.blob_count() < len(fixture_store) / 10

    def test_a_cassette_rehydrates_to_its_full_request(self, fixture_store) -> None:
        """B0 carries the handbook in the user message and B1/B2 in the system prompt; both
        must come back whole, not as a blob reference."""
        cassette = fixture_store.all()[0]
        texts = [b["text"] for b in cassette.request["system"]]
        texts += [b["text"] for m in cassette.request["messages"] for b in m["blocks"]]
        assert all(isinstance(t, str) for t in texts), "a blob reference was not rehydrated"
        assert any(len(t) > 5000 for t in texts), "the handbook must come back in full"


class TestPersistence:
    def test_calls_grades_and_run_are_written(self, workload, registry, bundle, tmp_path) -> None:
        items = list(bundle.test)[:12]
        result, snapshot = run_pipeline(workload, registry, bundle, "B2", items, tmp_path)
        engine = make_engine(tmp_path / "ledger.db")
        persist(engine, workload, result, items, snapshot)

        with session_scope(engine) as session:
            run = session.get(Run, result.run_id)
            assert run is not None
            assert run.origin == "simulated"
            assert run.status == "complete"
            calls = session.scalars(select(Call).where(Call.run_id == run.id)).all()
            grades = session.scalars(select(GradeRow).where(GradeRow.run_id == run.id)).all()
            assert len(grades) == len(items)
            # Call rows must sum to the headline, pre-warm included, or nothing reconciles.
            assert sum(c.cost for c in calls) == result.total_cost
            assert any(c.prewarm for c in calls)
            assert all(c.raw_usage for c in calls)
            assert all(c.price_snapshot_id == snapshot.snapshot_id for c in calls)

    def test_rerunning_replaces_rather_than_duplicates(
        self, workload, registry, bundle, tmp_path
    ) -> None:
        items = list(bundle.test)[:8]
        result, snapshot = run_pipeline(workload, registry, bundle, "B2", items, tmp_path)
        engine = make_engine(tmp_path / "ledger.db")
        persist(engine, workload, result, items, snapshot)
        persist(engine, workload, result, items, snapshot)
        with session_scope(engine) as session:
            grades = session.scalars(select(GradeRow).where(GradeRow.run_id == result.run_id)).all()
            assert len(grades) == len(items)

    def test_content_deletion_keeps_every_metric(
        self, workload, registry, bundle, tmp_path
    ) -> None:
        """SPEC.md non-negotiable 10: deleting content must not cost the user their results."""
        items = list(bundle.test)[:8]
        result, snapshot = run_pipeline(workload, registry, bundle, "B2", items, tmp_path)
        engine = make_engine(tmp_path / "ledger.db")
        persist(engine, workload, result, items, snapshot)

        with session_scope(engine) as session:
            before = [
                (c.cost, c.input_uncached, c.cache_read, c.output_visible)
                for c in session.scalars(
                    select(Call).where(Call.run_id == result.run_id).order_by(Call.id)
                ).all()
            ]
            deleted = delete_run_content(session, result.run_id)
        assert deleted > 0

        with session_scope(engine) as session:
            calls = session.scalars(
                select(Call).where(Call.run_id == result.run_id).order_by(Call.id)
            ).all()
            after = [(c.cost, c.input_uncached, c.cache_read, c.output_visible) for c in calls]
            assert after == before
            assert all(c.request_json is None for c in calls)
            assert all(c.response_text is None for c in calls)
            assert session.get(Run, result.run_id).content_deleted is True


class TestFailuresStayVisible:
    def test_a_failing_call_counts_as_an_unsuccessful_task(
        self, workload, registry, bundle, tmp_path
    ) -> None:
        """SPEC.md non-negotiable 8: a failure is not removed from the denominator."""

        class Exploding:
            name = "simulated"

            async def complete(self, request):
                raise RuntimeError("provider exploded")

            async def count_tokens(self, request):
                return None

        items = list(bundle.test)[:5]
        snapshot = registry.snapshot(list(registry.roles.values()), date(2026, 9, 11))
        runner = Runner(
            workload,
            registry,
            snapshot,
            Exploding(),
            provider="simulated",
            handbook=bundle.handbook,
            origin="simulated",
        )
        result = asyncio.run(runner.run_pipeline("B0", items, split="test", prewarm=False))
        assert len(result.tasks) == len(items)
        assert all(t.failed for t in result.tasks)

        engine = make_engine(tmp_path / "ledger.db")
        persist(engine, workload, result, items, snapshot)
        with session_scope(engine) as session:
            grades = session.scalars(select(GradeRow).where(GradeRow.run_id == result.run_id)).all()
            assert len(grades) == len(items)
            assert not any(g.correct for g in grades)
            calls = session.scalars(select(Call).where(Call.run_id == result.run_id)).all()
            assert all("provider exploded" in (c.error or "") for c in calls)


class TestProviderPolicy:
    def test_a_disallowed_provider_is_refused_by_the_runner(
        self, workload, registry, bundle
    ) -> None:
        """SPEC.md 7.7: allowed providers per workload, enforced by the router."""
        from tokop.adapters.base import AdapterError

        snapshot = registry.snapshot(list(registry.roles.values()), date(2026, 9, 11))
        with pytest.raises(AdapterError, match="not among them"):
            Runner(
                workload,
                registry,
                snapshot,
                object(),
                provider="deepseek",
                handbook=bundle.handbook,
            )

    def test_the_demo_workload_allows_only_anthropic_and_the_simulator(self, workload) -> None:
        assert workload.provider_allowed("anthropic")
        assert not workload.provider_allowed("openrouter")


class TestWorkloadSpec:
    def test_pipelines_and_cascade_load(self, workload) -> None:
        assert sorted(workload.pipelines) == ["B0", "B1", "B2"]
        assert workload.cascade("B3").tiers == ["cheap", "mid", "frontier"]
        assert workload.cascade("B3").base_pipeline == "B2"

    def test_b0_declares_its_antipatterns(self, workload) -> None:
        b0 = workload.pipeline("B0")
        assert set(b0.known_antipatterns) >= {"PL01", "PL02", "PL06", "PL10", "PL11"}
        for rule, explanation in b0.known_antipatterns.items():
            assert len(explanation) > 40, rule

    def test_b0_documents_the_duplicate_rule_the_lint_cannot_catch(self, workload) -> None:
        """The citation rule is stated twice in different words, as SPEC.md section 6 asks.
        PL07 is a lexical rule and cannot see a paraphrase, so B0 records that in its notes
        rather than claiming a catch it does not make (DECISIONS.md D18)."""
        b0 = workload.pipeline("B0")
        assert "PL07" not in b0.known_antipatterns
        notes = " ".join(b0.notes)
        assert "PL07" in notes and "paraphrase" in notes
        instructions = next(b for b in b0.system if b.label == "instructions").text
        assert "Always cite the section" in instructions
        assert "mention which part of the handbook you used" in instructions

    def test_b0_and_b1_have_identical_instructions(self, workload) -> None:
        """B1 is B0 reordered, not B0 rewritten. The comparison is only fair if that holds."""
        b0 = next(b for b in workload.pipeline("B0").system if b.label == "instructions")
        b1 = next(b for b in workload.pipeline("B1").system if b.label == "instructions")
        assert b0.text == b1.text

    def test_only_b1_and_b2_carry_a_cache_breakpoint(self, workload) -> None:
        assert not any(b.cache for b in workload.pipeline("B0").system)
        assert any(b.cache for b in workload.pipeline("B1").system)
        assert any(b.cache for b in workload.pipeline("B2").system)

    def test_b2_caps_output_far_below_b0(self, workload) -> None:
        assert workload.pipeline("B0").max_tokens == 2000
        assert workload.pipeline("B2").max_tokens == 200

    def test_an_unknown_pipeline_lists_the_known_ones(self, workload) -> None:
        with pytest.raises(WorkloadError, match="B0, B1, B2"):
            workload.pipeline("B9")

    def test_a_block_referencing_an_unknown_variable_raises(self, workload) -> None:
        with pytest.raises(WorkloadError, match="supplies"):
            workload.pipeline("B0").render("simulated", OPUS, {"question": "q"})


class TestResponderModel:
    def test_the_accuracy_profile_can_show_escalation(self) -> None:
        """If the tiers were this close, no cascade could help and the demo would be dishonest."""
        for question_type in ("lookup", "two_hop", "computation", "exception"):
            frontier = ACCURACY["frontier"][question_type]
            cheap = ACCURACY["cheap"][question_type]
            assert frontier >= cheap
        # And on the hard types the gap must be worth routing for.
        assert ACCURACY["frontier"]["computation"] - ACCURACY["cheap"]["computation"] > 0.15

    def test_correctness_is_deterministic_per_model_and_task(self, bundle, registry) -> None:
        tiers = {r: TierProfile(registry.roles[r], r) for r in ("cheap", "mid", "frontier")}
        a = DemoResponder(list(bundle.items), bundle.handbook, tiers, "B2", "json_answer")
        b = DemoResponder(list(bundle.items), bundle.handbook, tiers, "B2", "json_answer")
        item = bundle.items[0]
        assert a.is_correct(OPUS, item) == b.is_correct(OPUS, item)

    def test_an_unknown_model_raises_rather_than_guessing(self, bundle, registry) -> None:
        tiers = {r: TierProfile(registry.roles[r], r) for r in ("cheap", "mid", "frontier")}
        responder = DemoResponder(list(bundle.items), bundle.handbook, tiers, "B2", "json_answer")
        with pytest.raises(KeyError, match="no simulated accuracy profile"):
            responder.is_correct("gpt-9", bundle.items[0])
