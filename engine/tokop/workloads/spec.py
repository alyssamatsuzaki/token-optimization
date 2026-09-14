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
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from tokop.adapters.base import Block, LLMRequest, Message

VARIABLE = re.compile(r"\{\{(\w+)\}\}")

OutputContract = Literal["final_answer_line", "json_answer"]

#: How a workload's tasks are marked right or wrong (UPGRADE_V3.md U1). ``gold`` is the mode
#: every existing workload is in and stays in by default: the dataset carries an answer key.
#: ``judged`` is for the workload that arrives with no labels at all — a cheap verifier grades
#: everything and a strong grader corrects it on a sampled subset.
GradingMode = Literal["gold", "judged"]

#: Who produces the strong labels. ``frontier`` is a model call; ``human`` is a review queue
#: Tokop writes and refuses to fill in itself.
StrongGrader = Literal["frontier", "human"]


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
    #: Whether this pipeline asks for output a verifier can *check* cheaply — the rule it
    #: applied, the numbers it used and the step from one to the other, not just the answer
    #: (UPGRADE_V3.md U4). It is a lever with two sides: it costs input tokens and output
    #: tokens, it may cost accuracy, and it buys agreement between a cheap judge and a strong
    #: grader, which is what the annotation budget is spent on.
    checkable: bool = False
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


class JudgeSpec(BaseModel):
    """The cheap verifier, and the strong grader that corrects it (UPGRADE_V3.md U1).

    One prompt serves both. The only difference between the cheap judge and the strong grader
    is which model role runs it, which is what makes the correction term mean anything: if the
    two saw different prompts, ``H - G`` would mix a prompt difference into a rater difference.
    """

    #: Which verifier kind reads the reply. Registered in ``workloads/verification.py``.
    kind: str = "judge-v1"
    model_role: str = "cheap"
    #: The model role the strong grader uses when ``annotation.strong_grader`` is ``frontier``.
    strong_model_role: str = "frontier"
    #: U4's lever. ``checkable`` asks the pipeline for output a judge can check cheaply;
    #: ``as-is`` judges whatever the pipeline already produces. Priced in M10.
    contract: Literal["checkable", "as-is"] = "as-is"
    max_tokens: int = 200
    system: list[BlockSpec] = Field(default_factory=list)
    user: list[BlockSpec] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}

    def render(self, provider: str, model: str, variables: dict[str, str]) -> LLMRequest:
        return LLMRequest(
            provider=provider,
            model=model,
            system=[b.render(variables) for b in self.system],
            messages=[Message(role="user", blocks=[b.render(variables) for b in self.user])],
            max_tokens=self.max_tokens,
        )


class AnnotationSpec(BaseModel):
    """How much strong grading to buy, and where to spend it (UPGRADE_V3.md U2)."""

    #: The cap on strong-grader spend, in dollars. `tokop annotate` stops at it.
    budget_usd: Decimal = Decimal("1.00")
    strong_grader: StrongGrader = "frontier"
    #: Which allocation policy chooses pi(x). Registered in ``optimize/annotation.py``.
    policy: str = "cost-optimal"
    #: Fixes the uniform draw u_t per task, so the same budget always samples the same items
    #: and a smaller budget samples a subset of a larger one's.
    seed: int = 20260914
    #: No item may ever be unsamplable: the inverse weight 1/pi would be infinite and one
    #: unlucky draw would dominate the estimate. ``core/stats.py`` refuses a zero rate outright.
    min_rate: float = 0.05
    notes: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}


class DatasetProvenanceSpec(BaseModel):
    """Where the workload claims its tasks came from (UPGRADE_V3.md U8).

    A claim, not a measurement. Tokop cannot look at a question and tell whether a person typed
    it, a program templated it or a model wrote it, so the origins are declared and everything
    the *items* reveal — template coverage, how much of the set sits in rarely-seen templates,
    how concentrated it is — is computed beside them. Certification is refused on the pair.
    """

    #: Items taken from real recorded traffic. The only kind that is evidence about production.
    real_traffic_items: int = 0
    #: Items a model wrote. The ones the collapse literature is about.
    model_generated_items: int = 0
    #: Items a program templated. Synthetic, but nothing sampled them from a model, so they do
    #: not compound the way a self-consuming loop does. Kept separate for that reason.
    program_generated_items: int = 0
    generators: list[str] = Field(default_factory=list)
    #: Sampling budget the generators ran at. Hu et al. find a larger decoding budget mitigates
    #: collapse; ``None`` when no model was involved and there is nothing to record.
    decoding_budget: str | None = None
    relabelled_by_frozen_reference: bool = False
    real_traffic_accumulating: bool = False
    notes: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}

    #: The upgrade brief writes this as a share; it is stored as counts because a share cannot
    #: be checked against the set and a count can.
    @property
    def declared_total(self) -> int:
        return self.real_traffic_items + self.model_generated_items + self.program_generated_items


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
    #: ``gold`` unless the workload says otherwise, so every workload written before the
    #: judged path existed keeps grading exactly as it did.
    grading: GradingMode = "gold"
    judge: JudgeSpec | None = None
    annotation: AnnotationSpec | None = None
    dataset_provenance: DatasetProvenanceSpec | None = None
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

    provenance_raw = raw.get("dataset_provenance") or workload.get("dataset_provenance")
    dataset_provenance = (
        DatasetProvenanceSpec(**provenance_raw) if isinstance(provenance_raw, dict) else None
    )
    judge_raw = raw.get("judge") or workload.get("judge")
    judge = JudgeSpec(**judge_raw) if isinstance(judge_raw, dict) else None
    annotation_raw = raw.get("annotation") or workload.get("annotation")
    annotation = AnnotationSpec(**annotation_raw) if isinstance(annotation_raw, dict) else None
    grading = str(workload.get("grading", "gold"))
    if grading not in ("gold", "judged"):
        raise WorkloadError(
            f"grading must be `gold` or `judged`, not {grading!r}. `gold` compares against the "
            "dataset's answer key; `judged` has a cheap verifier grade everything and corrects "
            "it from a sampled subset of strong labels."
        )
    _validate_judging(path, grading, judge, annotation, dataset_provenance)

    return WorkloadSpec(
        id=str(workload["id"]),
        name=str(workload["name"]),
        description=str(workload["description"]),
        dataset=DatasetSpec(**workload["dataset"]),
        pipelines=pipelines,
        cascades=cascades,
        scorer_features=list(workload.get("scorer_features") or []),
        allowed_providers=list(workload.get("allowed_providers") or []),
        grading=grading,  # type: ignore[arg-type]
        judge=judge,
        annotation=annotation,
        dataset_provenance=dataset_provenance,
        margin=float(workload.get("margin", 0.03)),
        latency_sensitive=bool(workload.get("latency_sensitive", False)),
    )


def _validate_judging(
    path: Path,
    grading: str,
    judge: JudgeSpec | None,
    annotation: AnnotationSpec | None,
    dataset_provenance: DatasetProvenanceSpec | None = None,
) -> None:
    """Refuse a judged workload that cannot actually be judged.

    Every failure here is one a user would otherwise meet halfway through an annotation run,
    after money had been spent on the cheap judge.
    """
    if grading == "judged" and dataset_provenance is None:
        raise WorkloadError(
            f"{path} declares `grading: judged` but no `dataset_provenance:` block. A judged "
            "workload arrives without labels, so where its tasks came from is the only thing "
            "left that says whether a certificate over it means anything (UPGRADE_V3.md U8)."
        )
    if grading == "judged" and judge is None:
        raise WorkloadError(
            f"{path} declares `grading: judged` but defines no `judge:` block. Judged grading "
            "needs a verifier prompt and a model role; there is nothing to grade with."
        )
    if judge is None:
        if annotation is not None:
            raise WorkloadError(
                f"{path} defines an `annotation:` budget but no `judge:` block. The annotation "
                "budget buys strong labels that correct a cheap judge; without one there is "
                "nothing to correct."
            )
        return

    # Imported here, not at module scope: `optimize` reads workloads, so a top-level import
    # would tie the two together in both directions — the same reason the scorer check below
    # is deferred.
    from tokop.workloads.verification import VerificationError, verifier_kind

    try:
        verifier_kind(judge.kind)
    except VerificationError as exc:
        raise WorkloadError(f"{path}: {exc}") from None
    if not judge.user:
        raise WorkloadError(
            f"{path}: the judge block has no `user:` content, so every judge call would send "
            "an empty message."
        )
    rendered = "".join(block.text for block in [*judge.system, *judge.user])
    for required in ("{{answer}}", "{{question}}"):
        if required not in rendered:
            raise WorkloadError(
                f"{path}: the judge prompt never references {required}. A judge that cannot see "
                "the question and the answer is not verifying anything."
            )

    if annotation is None:
        return
    from tokop.optimize.annotation import AnnotationError, allocation_policy

    try:
        allocation_policy(annotation.policy)
    except AnnotationError as exc:
        raise WorkloadError(f"{path}: {exc}") from None
    if annotation.budget_usd < 0:
        raise WorkloadError(
            f"{path}: the annotation budget is ${annotation.budget_usd}. A negative budget is "
            "not zero annotations, it is a typo."
        )
    if not 0 < annotation.min_rate <= 1:
        raise WorkloadError(
            f"{path}: annotation.min_rate is {annotation.min_rate}; it must be in (0, 1]. An "
            "item that can never be sampled makes its inverse weight infinite."
        )


def demo_timestamp(now: datetime | None = None) -> str:
    """The volatile line B0 opens with.

    Fixed for a recording so the cassette key is stable; in production this would be
    ``datetime.now()``, which is exactly the bug PL01 exists to catch.
    """
    moment = now or datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
    return moment.strftime("Current date and time: %Y-%m-%d %H:%M UTC")
