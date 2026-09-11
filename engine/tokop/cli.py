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


@app.command()
def record(workload: str = typer.Option(..., "--workload")) -> None:
    """Record a workload against live providers. Capped by RECORD_BUDGET_USD."""
    _not_yet("record", "M4")


@app.command()
def prove(
    workload: str = typer.Option(..., "--workload"),
    baseline: str = typer.Option(..., "--baseline"),
    candidate: str = typer.Option(..., "--candidate"),
    margin: float = typer.Option(0.03, "--margin"),
) -> None:
    """Run the proof for a candidate pipeline against a baseline."""
    _not_yet("prove", "M4")


@app.command()
def report(
    check: bool = typer.Option(False, "--check"),
    write_readme: bool = typer.Option(False, "--write-readme"),
) -> None:
    """Recompute every headline metric from fixtures."""
    _not_yet("report", "M4")


@app.command()
def lint(path: str = typer.Argument(...)) -> None:
    """Run the prompt lint over a pipeline template."""
    _not_yet("lint", "M4")


@app.command("fixtures-check")
def fixtures_check() -> None:
    """Regenerate gold answers from YAML and assert they match the stored dataset."""
    _not_yet("fixtures-check", "M3")


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
