"""Workload and pipeline definitions, loaded from YAML (SPEC.md section 4, 6).

A pipeline is a list of content blocks for the system prompt and for the user message, plus a
model role, a token cap and an output contract. Blocks carry an optional cache breakpoint. That
is the whole vocabulary, and it is deliberately small: the product's argument is that *where*
text sits and *what order* it is in changes the bill, so the definition format has to make
position and order the explicit, editable thing.

Variables are ``{{name}}``. The demo supplies ``handbook``, ``question`` and ``timestamp``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from tokop.adapters.base import Block, LLMRequest, Message

VARIABLE = re.compile(r"\{\{(\w+)\}\}")

OutputContract = Literal["final_answer_line", "json_answer"]


class WorkloadError(ValueError):
    """A workload or pipeline definition could not be loaded."""


class BlockSpec(BaseModel):
    """One content block, with an optional cache breakpoint after it."""

    text: str
    cache: Literal["5m", "1h"] | None = None
    #: Free-text label used by the UI and by the lint to name what a block is.
    label: str | None = None

    model_config = {"frozen": True}

    def render(self, variables: dict[str, str]) -> Block:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                raise WorkloadError(
                    f"block references {{{{{name}}}}} but the workload supplies {sorted(variables)}"
                )
            return variables[name]

        return Block(text=VARIABLE.sub(replace, self.text), cache=self.cache)


class PipelineSpec(BaseModel):
    """One pipeline: the prompt, the model, the cap and the contract."""

    id: str
    name: str
    description: str
    model_role: str
    max_tokens: int
    system: list[BlockSpec] = Field(default_factory=list)
    user: list[BlockSpec] = Field(default_factory=list)
    output_contract: OutputContract = "final_answer_line"
    #: What this pipeline is meant to demonstrate. Shown in the UI next to the graph.
    notes: list[str] = Field(default_factory=list)
    #: Anti-patterns present on purpose, by lint rule ID, with an explanation each.
    known_antipatterns: dict[str, str] = Field(default_factory=dict)

    model_config = {"frozen": True}

    def render(self, provider: str, model: str, variables: dict[str, str]) -> LLMRequest:
        system = [b.render(variables) for b in self.system]
        user_blocks = [b.render(variables) for b in self.user]
        return LLMRequest(
            provider=provider,
            model=model,
            system=system,
            messages=[Message(role="user", blocks=user_blocks)],
            max_tokens=self.max_tokens,
        )


class CascadeSpec(BaseModel):
    """A cascade over an existing pipeline's prompt."""

    id: str
    name: str
    description: str
    base_pipeline: str
    tiers: list[str]
    #: Which scorer implementation routes this cascade, by registry name. Named here rather
    #: than hardcoded in the report so a cascade records the scorer that produced its numbers.
    scorer: str = "logistic-v1"
    #: Samples drawn per scored task. 1 is one call per tier, which is what a deterministic
    #: scorer needs. A sampling scorer charges every one of these.
    scorer_samples: int = 1
    #: Scorer kinds to evaluate and report side by side. Empty means "only `scorer`".
    scorer_grid: list[str] = Field(default_factory=list)
    #: Which of those the calibration search may actually adopt as the operating point. Empty
    #: means all of them, which is the right default when the response matrix is a recording.
    #: Naming a subset is how a workload says "measure this, but do not ship it yet" — and the
    #: reason has to be written down next to it, because a search that is not allowed to pick
    #: the cheapest option is a claim that the data cannot support the choice.
    scorer_search_grid: list[str] = Field(default_factory=list)
    #: Sample counts the calibration search may choose between. Empty means "only
    #: `scorer_samples`". The response matrix has to hold the largest of these.
    scorer_samples_grid: list[int] = Field(default_factory=list)

    def scorer_choices(self) -> list[str]:
        """Every scorer to evaluate, whether or not the search may adopt it."""
        return list(dict.fromkeys(self.scorer_grid or [self.scorer]))

    def searchable_scorers(self) -> list[str]:
        """The subset the calibration search may choose the operating point from."""
        allowed = self.scorer_search_grid or self.scorer_choices()
        return [name for name in self.scorer_choices() if name in set(allowed)]

    def sample_choices(self) -> list[int]:
        return sorted(set(self.scorer_samples_grid or [self.scorer_samples]))

    @property
    def max_samples(self) -> int:
        """The deepest the response matrix has to go for any setting the search may pick."""
        return max([self.scorer_samples, *self.scorer_samples_grid])

    threshold_step: float = 0.02
    #: Minimum calibration accuracy, expressed as "the frontier's accuracy minus this".
    accuracy_slack: float = 0.01
    notes: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}


class DatasetSpec(BaseModel):
    generator: str
    seed: int
    size: int
    calibration_size: int

    model_config = {"frozen": True}


class WorkloadSpec(BaseModel):
    """Everything one workload needs to be run, graded and optimized."""

    id: str
    name: str
    description: str
    dataset: DatasetSpec
    pipelines: dict[str, PipelineSpec]
    cascades: dict[str, CascadeSpec] = Field(default_factory=dict)
    scorer_features: list[str] = Field(default_factory=list)
    allowed_providers: list[str] = Field(default_factory=list)
    margin: float = 0.03
    #: Whether the workload has a latency requirement; drives the Batch API finding (W05).
    latency_sensitive: bool = False

    model_config = {"frozen": True}

    def pipeline(self, pipeline_id: str) -> PipelineSpec:
        try:
            return self.pipelines[pipeline_id]
        except KeyError:
            raise WorkloadError(
                f"no pipeline {pipeline_id!r}; this workload defines "
                f"{', '.join(sorted(self.pipelines))}"
            ) from None

    def cascade(self, cascade_id: str) -> CascadeSpec:
        try:
            return self.cascades[cascade_id]
        except KeyError:
            raise WorkloadError(
                f"no cascade {cascade_id!r}; this workload defines "
                f"{', '.join(sorted(self.cascades)) or 'none'}"
            ) from None

    def max_samples(self) -> int:
        """Samples per task the response matrix must hold to serve every cascade defined here.

        The recording captures this many once; a setting that wants fewer reads a prefix of
        them, so every k is compared over the same draws rather than over fresh ones.
        """
        return max([1, *(c.max_samples for c in self.cascades.values())])

    def provider_allowed(self, provider: str) -> bool:
        """Enforced by the router (SPEC.md 7.7). An empty list means no restriction."""
        return not self.allowed_providers or provider in self.allowed_providers


def load_workload(path: Path) -> WorkloadSpec:
    if not path.exists():
        raise WorkloadError(f"no workload at {path}")
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise WorkloadError(f"{path} must contain a mapping")

    pipelines_raw = raw.get("pipelines") or {}
    pipelines = {pid: PipelineSpec(id=pid, **spec) for pid, spec in pipelines_raw.items()}
    cascades_raw = raw.get("cascades") or {}
    cascades = {cid: CascadeSpec(id=cid, **spec) for cid, spec in cascades_raw.items()}

    workload = raw.get("workload")
    if not isinstance(workload, dict):
        raise WorkloadError(f"{path} needs a top-level `workload:` mapping")

    # Imported here rather than at module scope: the scorer registry lives in `optimize`, which
    # reads workloads, and a top-level import would tie the two together in both directions.
    from tokop.optimize.scorers import ScorerError, scorer_kind

    for cascade in cascades.values():
        if cascade.base_pipeline not in pipelines:
            raise WorkloadError(
                f"cascade {cascade.id!r} is built on pipeline {cascade.base_pipeline!r}, "
                f"which is not defined"
            )
        for name in {cascade.scorer, *cascade.scorer_grid, *cascade.scorer_search_grid}:
            try:
                scorer_kind(name)
            except ScorerError as exc:
                raise WorkloadError(f"cascade {cascade.id!r}: {exc}") from None
        if any(k < 1 for k in cascade.scorer_samples_grid):
            raise WorkloadError(
                f"cascade {cascade.id!r} has a sample count below 1 in its grid: "
                f"{cascade.scorer_samples_grid}"
            )
        unknown = set(cascade.scorer_search_grid) - set(cascade.scorer_choices())
        if unknown:
            raise WorkloadError(
                f"cascade {cascade.id!r} lets the search adopt {sorted(unknown)}, which is not "
                "among the scorers it evaluates. Add them to `scorer_grid` or remove them."
            )
        if cascade.scorer_samples < 1:
            raise WorkloadError(
                f"cascade {cascade.id!r} asks for {cascade.scorer_samples} samples per task; "
                "a tier has to be called at least once"
            )

    return WorkloadSpec(
        id=str(workload["id"]),
        name=str(workload["name"]),
        description=str(workload["description"]),
        dataset=DatasetSpec(**workload["dataset"]),
        pipelines=pipelines,
        cascades=cascades,
        scorer_features=list(workload.get("scorer_features") or []),
        allowed_providers=list(workload.get("allowed_providers") or []),
        margin=float(workload.get("margin", 0.03)),
        latency_sensitive=bool(workload.get("latency_sensitive", False)),
    )


def demo_timestamp(now: datetime | None = None) -> str:
    """The volatile line B0 opens with.

    Fixed for a recording so the cassette key is stable; in production this would be
    ``datetime.now()``, which is exactly the bug PL01 exists to catch.
    """
    moment = now or datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
    return moment.strftime("Current date and time: %Y-%m-%d %H:%M UTC")
