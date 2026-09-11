"""The /api surface (SPEC.md section 4, 5).

Every number served here comes from ``optimize/report.py`` — the same function the CLI runs and
the README is generated from — so the UI, the tests and the docs cannot drift apart
(non-negotiable 1). Nothing here recomputes a metric on its own.

The mode shapes what is *offered*, not what is claimed: in replay mode every live control is
reported as disabled with the reason, so the UI can render a real disabled state rather than a
button that does nothing (non-negotiable 9).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from tokop import __version__, recording_state
from tokop.adapters.base import Block, LLMRequest, Message
from tokop.core.ratios import fitted_estimator
from tokop.core.registry import Registry, load_registry
from tokop.core.sync import snapshot_age_days
from tokop.core.tokenize import base_counter_name
from tokop.extras import load_extras
from tokop.optimize.lint import LintContext, clear_score, lint
from tokop.optimize.report import ReportError, cached_report, trace_for
from tokop.optimize.transforms import apply_safe_fixes
from tokop.paths import fixtures_dir, repo_root
from tokop.settings import Mode, get_settings
from tokop.workloads.spec import load_workload

router = APIRouter(prefix="/api")


#: Live actions whose endpoint does not exist yet. They are disabled in **every** mode with the
#: reason that is actually true, because a control that is enabled and does nothing is worse
#: than one that is honestly unavailable (SPEC.md non-negotiable 9).
UNIMPLEMENTED_LIVE_ACTIONS: dict[str, str] = {
    "new_experiment": (
        "Running a new experiment is not built. The engine can do it — `tokop record` drives "
        "the recorder — but there is no endpoint, no run-status record and no progress view "
        "yet, so this button would do nothing. See DECISIONS.md D24."
    ),
    "compare": (
        "Running a live comparison is not built: there is no endpoint behind this button. The "
        "recorded comparisons below are real engine output. See DECISIONS.md D24."
    ),
    "rewrite": (
        "Rewriting with a model is not built. Apply safe fixes does the deterministic half and "
        "is wired; the model-assisted rewrite has no endpoint. See DECISIONS.md D24."
    ),
    "brief": (
        "Making a new brief is not built: there is no endpoint behind this button. The recorded "
        "brief is real engine output. See DECISIONS.md D24."
    ),
    "sync_models": (
        "Syncing the model listing needs openrouter.ai, which this environment's egress policy "
        "blocks. `tokop sync-models` is implemented and will work where the host is reachable; "
        "the committed snapshot is a hand-built fixture and says so. See DECISIONS.md D2."
    ),
    "delete_content": "",  # filled in below: this one really is wired
}


def _live_controls() -> dict[str, Any]:
    """Which actions are available, and — when they are not — exactly why.

    A disabled control with a one-line reason is the contract in SPEC.md non-negotiable 9. The
    reason is computed here rather than written into the UI so it cannot go stale, and an action
    with no endpoint is reported as unavailable **whatever the mode**, because enabling a button
    that does nothing is the failure this rule exists to prevent.
    """
    settings = get_settings()
    configured = settings.configured_providers()
    reasons: dict[str, str] = {
        name: reason for name, reason in UNIMPLEMENTED_LIVE_ACTIONS.items() if reason
    }

    mode_reason: str | None = None
    if settings.mode == Mode.REPLAY:
        mode_reason = (
            "Tokop is in replay mode, so it runs entirely from recorded fixtures and makes no "
            "API calls. Set TOKOP_MODE=live and provide a key to enable this."
        )
    elif not any(configured.values()):
        mode_reason = (
            "No provider key is configured. Keys stay on the server and are never sent here."
        )
    elif settings.daily_budget_usd is None:
        mode_reason = "DAILY_BUDGET_USD is not set, so no live action may spend."

    # The mode reason comes first where it applies: it is the one the reader can act on.
    if mode_reason:
        for name in ("new_experiment", "compare", "rewrite", "brief"):
            reasons[name] = mode_reason

    return {
        "enabled": not reasons,
        "disabled_reasons": reasons,
        "implemented": sorted(
            k
            for k in ("delete_content",)
            if k not in UNIMPLEMENTED_LIVE_ACTIONS or not UNIMPLEMENTED_LIVE_ACTIONS[k]
        ),
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


class InspectRequest(BaseModel):
    """What the Inspect screen sends. Variables are `{{name}}` and are left unrendered: the
    lint has to see the template, because a rendered timestamp and a `{{timestamp}}` variable
    are different findings."""

    system: str = ""
    user: str = ""
    tools: str = ""
    max_tokens: int = 1024
    models: list[str] = Field(default_factory=list)
    cache_after_system: bool = False
    apply_fixes: bool = False


def _inspect_request(payload: InspectRequest) -> LLMRequest:
    system_blocks: list[Block] = []
    for index, chunk in enumerate([c for c in payload.system.split("\n\n---\n\n") if c.strip()]):
        last = index == len(payload.system.split("\n\n---\n\n")) - 1
        system_blocks.append(
            Block(text=chunk, cache="5m" if payload.cache_after_system and last else None)
        )
    tools: list[dict[str, Any]] = []
    if payload.tools.strip():
        try:
            parsed = json.loads(payload.tools)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"the tool definitions are not valid JSON: {exc}. Paste the `tools` array "
                "exactly as you send it to the API.",
            ) from exc
        tools = parsed if isinstance(parsed, list) else [parsed]
    return LLMRequest(
        provider="anthropic",
        model="claude-opus-5",
        system=system_blocks,
        messages=[Message(role="user", blocks=[Block(text=payload.user)])] if payload.user else [],
        tools=tools,
        max_tokens=payload.max_tokens,
    )


def _model_costs(request: LLMRequest, registry: Registry) -> list[dict[str, Any]]:
    """Token counts and cost per call and per 1,000 calls, for every priced model.

    Counts are estimates and say so: an exact count needs a provider counting endpoint, and
    replay mode makes no calls (SPEC.md non-negotiable 2).
    """
    estimator = fitted_estimator()
    text = request.system_text + "\n" + "\n".join(m.text for m in request.messages)
    tool_text = json.dumps(request.tools) if request.tools else ""
    rows: list[dict[str, Any]] = []
    for entry in registry.models.values():
        counted = estimator.count(text, entry.model_id)
        tool_tokens = estimator.count(tool_text, entry.model_id).tokens if tool_text else 0
        overhead = entry.tool_use_system_prompt_tokens or 0 if request.tools else 0
        input_tokens = counted.tokens + tool_tokens + overhead
        prefix = request.static_prefix_text
        prefix_tokens = estimator.count(prefix, entry.model_id).tokens if prefix else 0
        price = entry.price
        per_call = (
            Decimal(input_tokens) * price.input + Decimal(request.max_tokens) * price.output
        ) / Decimal(1_000_000)
        minimum = entry.min_cacheable_tokens or 0
        rows.append(
            {
                "model_id": entry.model_id,
                "display_name": entry.display_name,
                "provider": entry.provider,
                "scarce": registry.is_scarce(entry.model_id),
                "input_tokens": input_tokens,
                "token_source": counted.source,
                "token_method": counted.method,
                "tool_definition_tokens": tool_tokens,
                "tool_use_system_prompt_tokens": overhead,
                "fixed_overhead_tokens": estimator.count(request.system_text, entry.model_id).tokens
                + tool_tokens
                + overhead,
                "cacheable_prefix_tokens": prefix_tokens,
                "min_cacheable_tokens": minimum,
                "clears_minimum": bool(prefix_tokens and minimum and prefix_tokens >= minimum),
                "cost_per_call_usd": str(per_call),
                "cost_per_1k_usd": str(per_call * 1000),
                "price_verified": price.provenance.verified,
                "price_source": price.provenance.source_url,
                "price_retrieved": price.provenance.retrieved.isoformat(),
            }
        )
    rows.sort(key=lambda r: Decimal(r["cost_per_1k_usd"]))
    return rows


@router.post("/inspect")
def inspect(payload: InspectRequest) -> dict[str, Any]:
    """What will this prompt cost, and what in it is waste? (SPEC.md 5.2)"""
    registry = load_registry()
    request = _inspect_request(payload)
    frontier = registry.model(registry.roles["frontier"])
    context = LintContext(
        price=frontier.price,
        min_cacheable_tokens=frontier.min_cacheable_tokens or 512,
        tool_use_system_prompt_tokens=frontier.tool_use_system_prompt_tokens or 0,
    )
    findings = lint(request, context)
    clear = clear_score(findings)

    result: dict[str, Any] = {
        "models": _model_costs(request, registry),
        "findings": [f.as_dict() for f in findings],
        "clear": clear,
        "clear_total": sum(clear.values()),
        "cacheability": {
            "static_prefix_chars": len(request.static_prefix_text),
            "has_breakpoint": any(b.cache for b in request.system),
            "note": (
                "The provider matches an exact prefix over tools, then system, then messages, "
                "and stops at the first difference."
            ),
        },
        "token_counter": base_counter_name(),
        "live_controls": _live_controls(),
    }

    if payload.apply_fixes:
        report_ = apply_safe_fixes(request)
        savings = report_.savings_per_1k(frontier.price)
        after_findings = lint(report_.after, context)
        after_clear = clear_score(after_findings)
        result["fixes"] = {
            "applied": [
                {
                    "transform": step.transform,
                    "title": step.title,
                    "input_delta": step.input_delta,
                    "changes": [
                        {
                            "kind": c.kind,
                            "section": c.section,
                            "block": c.block,
                            "before": c.before,
                            "after": c.after,
                            "note": c.note,
                        }
                        for c in step.changes
                    ],
                }
                for step in report_.applied
            ],
            "skipped": [
                {"transform": s.transform, "title": s.title, "reason": s.note}
                for s in report_.steps
                if not s.applied
            ],
            "input_tokens_before": report_.input_tokens_before,
            "input_tokens_after": report_.input_tokens_after,
            "input_delta": report_.input_delta,
            "cacheable_before": report_.cacheable_before,
            "cacheable_after": report_.cacheable_after,
            "max_tokens_before": report_.max_tokens_before,
            "max_tokens_after": report_.max_tokens_after,
            "savings_per_1k": {k: str(v) for k, v in savings.items()},
            "system_after": report_.after.system_text,
            "user_after": (report_.after.messages[0].text if report_.after.messages else ""),
            "clear_after": after_clear,
            "clear_total_after": sum(after_clear.values()),
            "findings_after": [f["id"] for f in (f.as_dict() for f in after_findings)],
            "note": (
                "Deterministic rewrites only. Adding an output contract and a cache breakpoint "
                "both make the prompt longer; the net input delta is shown next to what each "
                "buys back."
            ),
        }
    return result


@router.get("/inspect/pipeline/{pipeline_id}")
def inspect_pipeline(pipeline_id: str) -> dict[str, Any]:
    """The demo pipeline's prompt, ready to paste into Inspect."""
    spec = load_workload(repo_root() / "data/demo/workload.yaml")
    try:
        pipeline = spec.pipeline(pipeline_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "id": pipeline_id,
        "name": pipeline.name,
        "system": "\n\n---\n\n".join(b.text for b in pipeline.system),
        "user": "\n\n".join(b.text for b in pipeline.user),
        "max_tokens": pipeline.max_tokens,
        "cache_after_system": any(b.cache for b in pipeline.system),
        "variables": ["handbook", "question", "timestamp"],
    }


@router.get("/compare")
def compare() -> dict[str, Any]:
    """The recorded Compare examples (SPEC.md 5.3)."""
    state = recording_state.describe()
    extras = load_extras(fixtures_dir() / state.fixture_source / "extras.json")
    if extras is None:
        raise HTTPException(
            status_code=503,
            detail="no recorded Compare examples. Run `tokop build-test-fixtures`.",
        )
    return {
        "examples": extras.compares,
        "is_test_data": state.is_test_data,
        "live_controls": _live_controls(),
        "note": (
            "Replay mode shows recorded comparisons. A live comparison sends one prompt to the "
            "selected models in parallel after a preflight that shows the projected total cost."
        ),
    }


@router.get("/brief")
def brief() -> dict[str, Any]:
    """The recorded Brief example (SPEC.md 5.2)."""
    state = recording_state.describe()
    extras = load_extras(fixtures_dir() / state.fixture_source / "extras.json")
    if extras is None or not extras.brief:
        raise HTTPException(
            status_code=503, detail="no recorded Brief example. Run `tokop build-test-fixtures`."
        )
    return {**extras.brief, "is_test_data": state.is_test_data, "live_controls": _live_controls()}


class DeleteContentRequest(BaseModel):
    run_id: str


@router.post("/runs/{run_id}/content")
def delete_content(run_id: str) -> dict[str, Any]:
    """Delete stored prompts and outputs for one run, keeping every metric.

    SPEC.md non-negotiable 10. Costs, token counts, grades and scores are untouched, so
    honouring a deletion request does not cost the user the result they paid for.
    """
    from sqlalchemy import select

    from tokop.db import Call, Run, delete_run_content, make_engine, session_scope
    from tokop.paths import fixtures_dir

    state = recording_state.describe()
    ledger = fixtures_dir() / state.fixture_source / "ledger.db"
    if not ledger.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"no ledger at {ledger.name}. It is rebuilt by `tokop build-test-fixtures`; "
                "there is nothing stored to delete until then."
            ),
        )
    engine = make_engine(ledger)
    with session_scope(engine) as session:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r} in the ledger")
        before = session.scalars(select(Call).where(Call.run_id == run_id)).all()
        cost_before = sum(c.cost for c in before)
        token_before = sum(c.usage.total for c in before)
        deleted = delete_run_content(session, run_id)
    with session_scope(engine) as session:
        after = session.scalars(select(Call).where(Call.run_id == run_id)).all()
        return {
            "run_id": run_id,
            "calls_cleared": deleted,
            "prompts_remaining": sum(1 for c in after if c.request_json is not None),
            "responses_remaining": sum(1 for c in after if c.response_text is not None),
            "cost_usd_unchanged": str(sum(c.cost for c in after)) == str(cost_before),
            "tokens_unchanged": sum(c.usage.total for c in after) == token_before,
            "note": (
                "Stored prompts and outputs are gone. Every metric — cost, token counts, grades "
                "and scores — is unchanged."
            ),
        }


@router.get("/runs")
def runs() -> dict[str, Any]:
    """Runs in the ledger, and whether their stored content has been deleted."""
    from sqlalchemy import select

    from tokop.db import Call, Run, make_engine, session_scope
    from tokop.paths import fixtures_dir

    state = recording_state.describe()
    ledger = fixtures_dir() / state.fixture_source / "ledger.db"
    if not ledger.exists():
        return {"runs": [], "ledger": None}
    engine = make_engine(ledger)
    with session_scope(engine) as session:
        rows = session.scalars(select(Run).order_by(Run.id)).all()
        out = []
        for run in rows:
            calls = session.scalars(select(Call).where(Call.run_id == run.id)).all()
            out.append(
                {
                    "run_id": run.id,
                    "pipeline": run.pipeline_id,
                    "split": run.split,
                    "model_ids": run.model_ids,
                    "calls": len(calls),
                    "total_cost_usd": run.total_cost_usd,
                    "content_deleted": run.content_deleted,
                    "stored_prompts": sum(1 for c in calls if c.request_json is not None),
                    "stored_responses": sum(1 for c in calls if c.response_text is not None),
                }
            )
    return {"runs": out, "ledger": ledger.name}


class PreferenceRequest(BaseModel):
    compare_session_id: str
    model: str


@router.post("/preferences")
def store_preference(payload: PreferenceRequest) -> dict[str, Any]:
    """ "Prefer this answer" on Compare (SPEC.md 5.3).

    Written to the ledger. **Nothing reads it in v1** — it is there for a future learned
    router — but it really is stored, because a button that says "stored" and stores nothing is
    a fake button.
    """
    from tokop.db import Preference, make_engine, session_scope
    from tokop.paths import state_dir

    engine = make_engine(state_dir() / "preferences.db")
    with session_scope(engine) as session:
        session.add(Preference(compare_session_id=payload.compare_session_id, model=payload.model))
    return {
        "stored": True,
        "model": payload.model,
        "note": "Stored for a future learned router. Nothing reads it in v1.",
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
    from collections import defaultdict
    from datetime import date as _date
    from decimal import Decimal

    from tokop.core.budget import SpendGuard

    try:
        payload = cached_report(False)
    except ReportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    settings = get_settings()
    registry = load_registry()
    workload_spec = load_workload(repo_root() / "data/demo/workload.yaml")

    by_model: dict[str, Decimal] = defaultdict(Decimal)
    by_pipeline: dict[str, Decimal] = defaultdict(Decimal)
    by_provider: dict[str, Decimal] = defaultdict(Decimal)
    by_workload: dict[str, Decimal] = defaultdict(Decimal)
    by_day: dict[str, Decimal] = defaultdict(Decimal)
    scarce_total = Decimal(0)
    total = Decimal(0)

    recorded_at = payload["provenance"].get("recorded_at") or ""
    day = recorded_at[:10] or _date.today().isoformat()

    for run in payload["runs"]:
        cost = Decimal(run["total_cost_usd"])
        by_model[run["model_id"]] += cost
        by_pipeline[run["pipeline"]] += cost
        by_provider[registry.model(run["model_id"]).provider] += cost
        by_workload[workload_spec.id] += cost
        # Every run in a fixture set was recorded in one session, so the ledger has one day.
        # The dimension is real and the shape is right; a live ledger fills it out.
        by_day[day] += cost
        total += cost
        if registry.is_scarce(run["model_id"]):
            scarce_total += cost

    # The daily figure is read from the guard rather than asserted. In replay it is genuinely
    # zero because no live call was made — and the payload says which of those two it is.
    guard = SpendGuard(daily_cap=settings.daily_budget_usd)
    daily = next(s for s in guard.status() if s.name == "daily")

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
        "by_provider": [
            {
                "provider": provider,
                "usd": str(amount),
                "share": float(amount / total) if total else 0.0,
            }
            for provider, amount in sorted(by_provider.items(), key=lambda kv: -kv[1])
        ],
        "by_pipeline": [
            {"pipeline": pipeline, "usd": str(amount)}
            for pipeline, amount in sorted(by_pipeline.items())
        ],
        "by_workload": [
            {"workload": name, "usd": str(amount), "runs": len(payload["runs"])}
            for name, amount in sorted(by_workload.items())
        ],
        "by_day": [{"day": d, "usd": str(amount)} for d, amount in sorted(by_day.items())],
        "runs": payload["runs"],
        "budgets": {
            "daily_cap_usd": daily.as_dict()["cap_usd"],
            "daily_spent_usd": daily.as_dict()["spent_usd"],
            "daily_remaining_usd": daily.as_dict()["remaining_usd"],
            "fraction": daily.fraction,
            "state": daily.state,
            "warning_at": 0.8,
            "note": (
                "Replay mode makes no API calls, so today's live spend is genuinely zero. The "
                "totals above are what the recorded runs cost when they were made."
            ),
        },
        "cost_per_successful_task": [
            {
                "pipeline": step["pipeline"],
                "label": step["label"],
                "usd": step["cost_per_successful_task"]["point"],
                "low": step["cost_per_successful_task"]["low"],
                "high": step["cost_per_successful_task"]["high"],
                "day": day,
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
