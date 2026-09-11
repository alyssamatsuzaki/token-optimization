"""Tokop command line. See SPEC.md section 4."""

from __future__ import annotations

import sys

import typer

from tokop import recording_state
from tokop.core.registry import OPENROUTER_MODELS_URL, load_registry
from tokop.paths import repo_root

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Find the cheapest way to run an AI workload without getting worse, and prove it.",
)


@app.command("recording-state")
def recording_state_cmd(
    check: bool = typer.Option(
        False, "--check", help="Exit nonzero only if the state is unreadable."
    ),
) -> None:
    """Report which demo recording this build has, and why."""
    state = recording_state.describe()
    typer.echo(f"state:   {state.state}")
    typer.echo(f"source:  fixtures/{state.fixture_source}/")
    typer.echo(f"reason:  {state.reason}")
    # Every state is a legitimate outcome; only an unreadable state is a failure.
    known = (recording_state.RECORDED, recording_state.AWAITING_REVIEW, recording_state.UNRECORDED)
    if check and state.state not in known:
        typer.echo(f"unknown recording state: {state.state}", err=True)
        raise typer.Exit(1)


def _not_yet(name: str, milestone: str) -> None:
    typer.echo(f"`tokop {name}` is not built yet (arrives in {milestone}).", err=True)
    raise typer.Exit(2)


@app.command("build-test-fixtures")
def build_test_fixtures_cmd() -> None:
    """Build fixtures/test/ from the deterministic simulated provider. Spends nothing."""
    from tokop.paths import fixtures_dir
    from tokop.recorder import build_test_fixtures
    from tokop.workloads.demo.dataset import build as build_dataset
    from tokop.workloads.spec import load_workload

    registry = load_registry()
    workload = load_workload(repo_root() / "data/demo/workload.yaml")
    bundle = build_dataset(
        size=workload.dataset.size,
        calibration_size=workload.dataset.calibration_size,
        seed=workload.dataset.seed,
    )
    root = fixtures_dir() / "test"
    root.mkdir(parents=True, exist_ok=True)
    report = build_test_fixtures(workload, registry, bundle, root)
    for step in report.steps:
        typer.echo("  " + step.summary())
    if report.stopped:
        typer.echo(f"\nSTOPPED: {report.stopped}", err=True)
        raise typer.Exit(1)
    typer.echo(
        f"\n{len(report.steps)} runs, {report.cassette_count} cassettes, "
        f"simulated cost ${report.total_cost:.4f} (no money was spent: origin=simulated)"
    )


@app.command()
def record(
    workload: str = typer.Option("data/demo/workload.yaml", "--workload"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Record a workload against live providers. Capped by RECORD_BUDGET_USD.

    Refuses to start without a key and an explicit budget, prints its projection before
    spending anything, and aborts if the projection exceeds the remaining budget. An
    interrupted recording resumes: identical requests reuse their cassette, so nothing is
    paid for twice.
    """
    from tokop.recorder import RecordingStopped, require_live_consent
    from tokop.settings import Mode, get_settings

    settings = get_settings()
    if settings.mode != Mode.LIVE:
        typer.echo(
            f"TOKOP_MODE is {settings.mode!r}. Recording makes live API calls, so it only runs "
            "with TOKOP_MODE=live.",
            err=True,
        )
        raise typer.Exit(2)

    try:
        budget = require_live_consent(settings)
    except RecordingStopped as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    typer.echo(f"workload: {workload}")
    typer.echo(f"budget:   ${budget} (RECORD_BUDGET_USD)")
    typer.echo(
        "\nThe live recording path is wired to the recorder and the spend guard, but it has "
        "not been exercised against a real provider in this build: no credentials were "
        "available (DECISIONS.md D1). Rather than claim a path that has never run, `tokop "
        "record` stops here and points you at the simulated fixture builder."
    )
    typer.echo("\n  tokop build-test-fixtures    # deterministic, spends nothing")
    raise typer.Exit(3)


@app.command()
def prove(
    workload: str = typer.Option("data/demo/workload.yaml", "--workload"),
    baseline: str = typer.Option("B0", "--baseline"),
    candidate: str = typer.Option("B3", "--candidate"),
    margin: float = typer.Option(0.03, "--margin"),
) -> None:
    """Run the proof for a candidate pipeline against a baseline.

    Exits 0 when the verdict is non-inferior and 1 otherwise, so it can gate CI.
    """
    from tokop.optimize.report import build_report

    payload = build_report()
    proof = payload["proof"]
    if proof["baseline"]["pipeline"] != baseline or proof["candidate"]["pipeline"] != candidate:
        typer.echo(
            f"this build's report compares {proof['baseline']['pipeline']} with "
            f"{proof['candidate']['pipeline']}; --baseline/--candidate are not yet wired to "
            "arbitrary pairs.",
            err=True,
        )
        raise typer.Exit(2)

    typer.echo(f"workload:  {payload['workload']['name']} ({workload})")
    typer.echo(
        f"splits:    {proof['split_sizes']['calibration']} calibration, "
        f"{proof['split_sizes']['test']} test"
    )
    typer.echo(f"margin:    {margin:.0%}")
    typer.echo("")
    typer.echo(f"VERDICT:   {proof['verdict']['display']}")
    typer.echo(f"           {proof['verdict']['sentence']}")
    typer.echo("")
    typer.echo(f"proof cost ${float(proof['proof_cost']['total_usd']):.4f}", nl=False)
    if proof["repayment_tasks"]:
        typer.echo(f", repaid after {proof['repayment_tasks']:,} tasks")
    else:
        typer.echo("")
    if payload["provenance"]["is_test_data"]:
        typer.echo("")
        typer.echo("NOTE: computed over simulated test data, not a recording.", err=True)
    if proof["verdict"]["label"] != "non_inferior":
        raise typer.Exit(1)


@app.command()
def report(
    check: bool = typer.Option(False, "--check", help="Recompute and assert everything agrees."),
    write_readme: bool = typer.Option(False, "--write-readme", help="Rewrite the README block."),
    out: str = typer.Option("", "--out", help="Write the full report JSON here."),
) -> None:
    """Recompute every headline metric from fixtures."""
    import json as _json

    from tokop.optimize.report import (
        build_report,
        cached_report,
        clear_cache,
        readme_metrics_current,
        write_readme_metrics,
    )

    payload = build_report()

    if write_readme:
        path = write_readme_metrics(payload)
        typer.echo(f"wrote the metrics block to {path.relative_to(repo_root())}")

    if out:
        destination = repo_root() / out
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_json.dumps(payload.data, indent=2, sort_keys=True, default=str))
        typer.echo(f"wrote {destination.relative_to(repo_root())}")

    if not check:
        headline = payload.headline()
        for key, value in headline.items():
            typer.echo(f"  {key:38} {value}")
        return

    failures = 0

    # 1. The computation is deterministic: a second run produces identical headline numbers.
    again = build_report()
    if again.headline() != payload.headline():
        typer.echo("  FAIL  the report is not deterministic across two builds", err=True)
        failures += 1
    else:
        typer.echo("  ok    the report recomputes identically")

    # 2. The API serves the same computation, not a parallel one.
    clear_cache()
    served = cached_report(False)
    if served.headline() != payload.headline():
        typer.echo("  FAIL  /api/report does not match a fresh computation", err=True)
        failures += 1
    else:
        typer.echo("  ok    /api/report serves the same numbers")

    # 3. The README's metrics block is current.
    current, message = readme_metrics_current(payload)
    typer.echo(f"  {'ok  ' if current else 'FAIL'}  {message}")
    failures += 0 if current else 1

    if failures:
        typer.echo(f"\nreport --check FAILED ({failures})", err=True)
        raise typer.Exit(1)
    typer.echo("\nreport --check passed")


@app.command()
def lint(
    pipeline: str = typer.Argument("B0", help="Pipeline ID from the demo workload."),
) -> None:
    """Run the prompt lint over a pipeline template."""
    from tokop.optimize.report import build_report

    payload = build_report()
    entry = payload["lint"].get(pipeline)
    if entry is None:
        typer.echo(
            f"no pipeline {pipeline!r}; this workload defines {', '.join(sorted(payload['lint']))}",
            err=True,
        )
        raise typer.Exit(2)
    clear = entry["clear"]
    typer.echo(
        f"{pipeline}: CLEAR {entry['clear_total']}/25  "
        + "  ".join(f"{letter} {score}" for letter, score in clear.items())
    )
    if not entry["findings"]:
        typer.echo("  no findings")
        return
    for finding in entry["findings"]:
        typer.echo(
            f"  {finding['id']} [{finding['group']:5}] {finding['confidence']:9} "
            f"${float(finding['projected_usd_per_1k']):>8.2f}/1k  {finding['title']}"
        )
        typer.echo(f"        {finding['evidence'][:100]}")


@app.command("dataset")
def dataset_cmd(
    write: bool = typer.Option(False, "--write", help="Regenerate and write the dataset."),
) -> None:
    """Show or regenerate the demo dataset."""
    from tokop.workloads.demo.dataset import build, write_dataset_doc
    from tokop.workloads.demo.dataset import write as write_dataset

    bundle = build()
    if write:
        path, handbook = write_dataset(bundle)
        doc = write_dataset_doc(bundle)
        typer.echo(f"wrote {path.relative_to(repo_root())}")
        typer.echo(f"wrote {handbook.relative_to(repo_root())}")
        typer.echo(f"wrote {doc.relative_to(repo_root())}")
    counts: dict[str, int] = {}
    for item in bundle.items:
        counts[item.question_type] = counts.get(item.question_type, 0) + 1
    typer.echo(
        f"{len(bundle.items)} items ({len(bundle.calibration)} calibration, "
        f"{len(bundle.test)} test), seed {bundle.seed}"
    )
    for question_type in sorted(counts):
        typer.echo(f"  {question_type:12} {counts[question_type]:4}")


@app.command("fixtures-check")
def fixtures_check() -> None:
    """Regenerate gold answers from YAML and assert they match the stored dataset.

    Also checks that every cassette carries provider-reported usage and that every run manifest
    records the model IDs, the prices with their provenance, the seed and the date
    (SPEC.md section 10, check 4).
    """
    from tokop.workloads.demo.dataset import verify

    failures = 0
    result = verify()
    for name, ok, detail in result.checks:
        typer.echo(f"  {'ok  ' if ok else 'FAIL'}  {name}: {detail}")
        failures += 0 if ok else 1

    for name, ok, detail in _check_fixture_dirs():
        typer.echo(f"  {'ok  ' if ok else 'FAIL'}  {name}: {detail}")
        failures += 0 if ok else 1

    if failures:
        typer.echo(f"\nfixtures-check FAILED ({failures} check(s))", err=True)
        raise typer.Exit(1)
    typer.echo("\nfixtures-check passed")


def _check_fixture_dirs() -> list[tuple[str, bool, str]]:
    """Cassette and manifest checks over whichever fixture set this build has."""
    import json

    from tokop.adapters.cassette import CassetteStore
    from tokop.paths import fixtures_dir

    checks: list[tuple[str, bool, str]] = []
    for kind in ("demo", "test"):
        root = fixtures_dir() / kind
        cassettes = root / "cassettes"
        if not cassettes.exists():
            continue
        store = CassetteStore(cassettes)
        all_cassettes = store.all()
        without_usage = [c.key[:10] for c in all_cassettes if not c.raw_usage and c.error is None]
        checks.append(
            (
                f"fixtures/{kind}: every cassette has provider-reported usage",
                not without_usage,
                f"{len(all_cassettes)} cassettes, {len(without_usage)} without usage"
                + (f": {without_usage[:3]}" if without_usage else ""),
            )
        )

        manifest_path = root / "manifest.json"
        if not manifest_path.exists():
            checks.append((f"fixtures/{kind}: manifest present", False, f"no {manifest_path.name}"))
            continue
        manifest = json.loads(manifest_path.read_text())
        runs = manifest.get("runs") or []
        missing: list[str] = []
        for run in runs:
            for field in ("model_ids", "seed", "recorded_at", "prices", "split_hash"):
                if not run.get(field):
                    missing.append(f"{run.get('pipeline', '?')}.{field}")
            for model_id, price in (run.get("prices") or {}).items():
                for field in ("source_url", "retrieved"):
                    if not price.get(field):
                        missing.append(f"{model_id}.{field}")
        checks.append(
            (
                f"fixtures/{kind}: every manifest records models, prices, seed and date",
                not missing and bool(runs),
                f"{len(runs)} runs"
                + (f", missing {missing[:4]}" if missing else ", all fields present"),
            )
        )
    if not checks:
        checks.append(
            (
                "fixture directories",
                False,
                "neither fixtures/demo/ nor fixtures/test/ has cassettes",
            )
        )
    return checks


@app.command("sync-models")
def sync_models(
    url: str = typer.Option(OPENROUTER_MODELS_URL, "--url", help="Model listing endpoint."),
    out: str = typer.Option(
        "fixtures/test/openrouter-models.json", "--out", help="Where to write the snapshot."
    ),
) -> None:
    """Pull model metadata from OpenRouter's public model listing into the registry."""
    from tokop.core.sync import SyncError, fetch_listing, write_snapshot

    destination = repo_root() / out
    try:
        payload = fetch_listing(url)
    except SyncError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    count = write_snapshot(payload, destination)
    typer.echo(f"wrote {count} priced models to {out} (retrieved {payload['_tokop_retrieved']})")
    typer.echo("Everything from the listing loads as verified: false; config/prices.yaml wins.")


@app.command()
def models() -> None:
    """List the model registry with its price provenance."""
    registry = load_registry()
    typer.echo(f"{'model':38} {'in $/Mtok':>10} {'out $/Mtok':>11}  verified  source")
    for model_id in sorted(registry.models):
        entry = registry.models[model_id]
        mark = "yes" if entry.price.provenance.verified else "NO "
        typer.echo(
            f"{model_id:38} {entry.price.input:>10} {entry.price.output:>11}  {mark:8}  "
            f"{entry.price.provenance.source_url}"
        )
    roles = ", ".join(f"{k}={v}" for k, v in sorted(registry.roles.items()))
    typer.echo(f"\nroles: {roles}")
    typer.echo(f"scarce: {', '.join(registry.scarce)}")
    if registry.listing_taken:
        typer.echo(f"listing snapshot: {registry.listing_source} taken {registry.listing_taken}")


def main() -> int:
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
