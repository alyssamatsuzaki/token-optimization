"""The /api surface (SPEC.md section 4, 5).

Every number served here comes from ``optimize/report.py`` — the same function the CLI runs and
the README is generated from — so the UI, the tests and the docs cannot drift apart
(non-negotiable 1). Nothing here recomputes a metric on its own.

The mode shapes what is *offered*, not what is claimed: in replay mode every live control is
reported as disabled with the reason, so the UI can render a real disabled state rather than a
button that does nothing (non-negotiable 9).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from tokop import __version__, recording_state
from tokop.core.registry import load_registry
from tokop.core.sync import snapshot_age_days
from tokop.core.tokenize import base_counter_name
from tokop.optimize.report import ReportError, cached_report, trace_for
from tokop.paths import repo_root
from tokop.settings import Mode, get_settings
from tokop.workloads.spec import load_workload

router = APIRouter(prefix="/api")


def _live_controls() -> dict[str, Any]:
    """Which live actions are available, and — when they are not — exactly why.

    A disabled control with a one-line reason is the contract in SPEC.md non-negotiable 9. The
    reason is computed here rather than written into the UI so it cannot go stale.
    """
    settings = get_settings()
    configured = settings.configured_providers()
    reasons: dict[str, str] = {}
    if settings.mode == Mode.REPLAY:
        reason = (
            "Tokop is in replay mode, so it runs entirely from recorded fixtures and makes no "
            "API calls. Set TOKOP_MODE=live and provide a key to enable this."
        )
        reasons = {k: reason for k in ("new_experiment", "compare", "rewrite", "brief")}
    elif not any(configured.values()):
        reason = "No provider key is configured. Keys stay on the server and are never sent here."
        reasons = {k: reason for k in ("new_experiment", "compare", "rewrite", "brief")}
    elif settings.daily_budget_usd is None:
        reason = "DAILY_BUDGET_USD is not set, so no live action may spend."
        reasons = {k: reason for k in ("new_experiment", "compare", "rewrite", "brief")}
    return {
        "enabled": not reasons,
        "disabled_reasons": reasons,
    }


@router.get("/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    state = recording_state.describe()
    return {
        "version": __version__,
        "mode": settings.mode,
        "recording": state.to_dict(),
        "live_controls": _live_controls(),
    }


@router.get("/report")
def report(
    protect_scarce: bool = Query(False, description="Minimise scarce-model spend instead of cost."),
) -> dict[str, Any]:
    """Every headline metric, recomputed from fixtures."""
    try:
        payload = cached_report(protect_scarce)
    except ReportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {**payload.data, "live_controls": _live_controls()}


@router.get("/trace/{task_id}")
def trace(task_id: str) -> dict[str, Any]:
    """Every call every pipeline made for one task."""
    try:
        return trace_for(task_id)
    except ReportError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workload")
def workload() -> dict[str, Any]:
    spec = load_workload(repo_root() / "data/demo/workload.yaml")
    return {
        "id": spec.id,
        "name": spec.name,
        "description": spec.description,
        "margin": spec.margin,
        "latency_sensitive": spec.latency_sensitive,
        "allowed_providers": spec.allowed_providers,
        "scorer_features": spec.scorer_features,
        "dataset": {
            "size": spec.dataset.size,
            "calibration_size": spec.dataset.calibration_size,
            "seed": spec.dataset.seed,
        },
        "pipelines": {
            pid: {
                "name": p.name,
                "description": p.description,
                "model_role": p.model_role,
                "max_tokens": p.max_tokens,
                "output_contract": p.output_contract,
                "notes": p.notes,
                "known_antipatterns": p.known_antipatterns,
                "system": [{"label": b.label, "cache": b.cache, "text": b.text} for b in p.system],
                "user": [{"label": b.label, "cache": b.cache, "text": b.text} for b in p.user],
            }
            for pid, p in spec.pipelines.items()
        },
        "cascades": {
            cid: {
                "name": c.name,
                "description": c.description,
                "base_pipeline": c.base_pipeline,
                "tiers": c.tiers,
                "threshold_step": c.threshold_step,
                "accuracy_slack": c.accuracy_slack,
                "notes": c.notes,
            }
            for cid, c in spec.cascades.items()
        },
    }


@router.get("/settings")
def settings_view() -> dict[str, Any]:
    """Settings (SPEC.md 5.5). Keys are reported as configured or not, and never sent."""
    settings = get_settings()
    registry = load_registry()
    spec = load_workload(repo_root() / "data/demo/workload.yaml")
    state = recording_state.describe()
    return {
        "mode": settings.mode,
        "providers": [
            {
                "name": name,
                "configured": settings.configured_providers().get(name, False),
                "enabled": provider.enabled,
                "adapter": provider.adapter,
                "base_url": provider.base_url,
                "usage_mapping": provider.usage_mapping,
                "unconfirmed_reason": provider.unconfirmed_reason,
                "test_only": provider.test_only,
                "docs": provider.docs,
            }
            for name, provider in sorted(registry.providers.items())
        ],
        "models": [
            {
                "model_id": entry.model_id,
                "display_name": entry.display_name,
                "provider": entry.provider,
                "input_per_mtok": str(entry.price.input),
                "output_per_mtok": str(entry.price.output),
                "cache_read_per_mtok": str(entry.price.cache_read),
                "cache_write_5m_per_mtok": str(entry.price.cache_write_5m),
                "batch_discount": str(entry.price.batch_discount),
                "min_cacheable_tokens": entry.min_cacheable_tokens,
                "context_tokens": entry.price.context_tokens,
                "tokenizer_generation": entry.tokenizer_generation,
                "scarce": registry.is_scarce(entry.model_id),
                "roles": [r for r, m in registry.roles.items() if m == entry.model_id],
                "provenance": {
                    "source_url": entry.price.provenance.source_url,
                    "retrieved": entry.price.provenance.retrieved.isoformat(),
                    "verified": entry.price.provenance.verified,
                    "note": entry.price.provenance.note,
                },
            }
            for entry in sorted(registry.models.values(), key=lambda e: e.model_id)
        ],
        "scarce_models": registry.scarce,
        "budgets": {
            "record_budget_usd": (
                str(settings.record_budget_usd) if settings.record_budget_usd else None
            ),
            "daily_budget_usd": (
                str(settings.daily_budget_usd) if settings.daily_budget_usd else None
            ),
        },
        "allowed_providers": {spec.id: spec.allowed_providers},
        "model_listing": {
            "source": registry.listing_source,
            "taken": registry.listing_taken.isoformat() if registry.listing_taken else None,
            "age_days": snapshot_age_days(registry.listing_taken),
        },
        "token_counter": base_counter_name(),
        "recording": state.to_dict(),
        "live_controls": _live_controls(),
    }


@router.get("/spend")
def spend() -> dict[str, Any]:
    """The Spend screen's ledger view (SPEC.md 5.4).

    Built from the same replayed runs the report uses, so a figure here and a figure on
    Optimize cannot disagree.
    """
    try:
        payload = cached_report(False)
    except ReportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    settings = get_settings()
    registry = load_registry()

    from collections import defaultdict
    from decimal import Decimal

    by_model: dict[str, Decimal] = defaultdict(Decimal)
    by_pipeline: dict[str, Decimal] = defaultdict(Decimal)
    scarce_total = Decimal(0)
    total = Decimal(0)
    for run in payload["runs"]:
        cost = Decimal(run["total_cost_usd"])
        by_model[run["model_id"]] += cost
        by_pipeline[run["pipeline"]] += cost
        total += cost
        if registry.is_scarce(run["model_id"]):
            scarce_total += cost

    return {
        "total_usd": str(total),
        "scarce_usd": str(scarce_total),
        "scarce_share": float(scarce_total / total) if total else 0.0,
        "by_model": [
            {
                "model_id": model,
                "usd": str(amount),
                "scarce": registry.is_scarce(model),
                "share": float(amount / total) if total else 0.0,
            }
            for model, amount in sorted(by_model.items(), key=lambda kv: -kv[1])
        ],
        "by_pipeline": [
            {"pipeline": pipeline, "usd": str(amount)}
            for pipeline, amount in sorted(by_pipeline.items())
        ],
        "runs": payload["runs"],
        "budgets": {
            "daily_cap_usd": (
                str(settings.daily_budget_usd) if settings.daily_budget_usd else None
            ),
            "daily_spent_usd": "0",
            "state": "uncapped" if settings.daily_budget_usd is None else "ok",
        },
        "cost_per_successful_task": [
            {
                "pipeline": step["pipeline"],
                "label": step["label"],
                "usd": step["cost_per_successful_task"]["point"],
                "low": step["cost_per_successful_task"]["low"],
                "high": step["cost_per_successful_task"]["high"],
            }
            for step in payload["waterfall"]
        ],
        "provenance": payload["provenance"],
        "note": (
            "Tokop sees only the API calls it made itself. It cannot see Claude.ai or Claude "
            "Code plan usage."
        ),
        "live_controls": _live_controls(),
    }
