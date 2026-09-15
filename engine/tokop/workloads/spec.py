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


class StepSpec(BaseModel):
    """One step of a multi-call pipeline (UPGRADE_V4.md M16).

    A single-call pipeline declares none of these and keeps working exactly as it did: the
    compiler turns it into a graph of one `generate` step that renders the identical request.
    Declaring steps is how a workload says it is an agent rather than a prompt — fourteen calls
    and two retries, where the useful proposal is usually to delete one of them rather than to
    run a cheaper model.

    **Two kinds of step, and the difference is who produces the output.** A `generate`, `verify`
    or `retry` step is a model call: its `system` and `user` blocks are the prompt and its output
    is the reply. A `tool` or `retrieve` step is *local*: Tokop has no tool runtime and does not
    pretend to one, so such a step's `user` blocks are the arguments it was called with and its
    `emits` blocks are the result the **workload declares** it returns. That makes a graph a
    model of an agent's shape rather than an agent, which is the honest description and the one
    the workload's own notes have to carry.
    """

    id: str
    #: What this step does. `generate` is a model call; the others exist so a graph can say what
    #: a step *is* before Tokop can price it — a finding about a tool called twice with the same
    #: arguments has to know which calls were tool calls.
    kind: Literal["generate", "tool", "retrieve", "verify", "retry", "loop"] = "generate"
    #: Step ids whose output this one consumes. Empty means it runs first.
    inputs: list[str] = Field(default_factory=list)
    model_role: str | None = None
    max_tokens: int | None = None
    system: list[BlockSpec] = Field(default_factory=list)
    user: list[BlockSpec] = Field(default_factory=list)
    #: What a *local* step puts back into the graph. Declared rather than executed: nothing here
    #: calls a tool, so a step that claims to retrieve a document returns the text the workload
    #: says it returns. A generation step must not declare it — its output is the model's reply.
    emits: list[BlockSpec] = Field(default_factory=list)
    #: For a `tool` step, the tool it calls. Recorded so two calls to the same tool with the
    #: same arguments are recognisable as the same call.
    tool: str = ""
    notes: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}

    def references(self) -> set[str]:
        """Every ``{{variable}}`` this step's prompt and arguments mention."""
        return {
            match.group(1)
            for block in [*self.system, *self.user]
            for match in VARIABLE.finditer(block.text)
        }

    def prefix_references(self) -> set[str]:
        """Variables inside the cacheable part of this step's system prompt.

        A step whose *prefix* mentions no upstream step is a step whose cache entry is the same
        on every task, which is what decides whether it can be warmed before the fan-out.
        """
        cut = -1
        for index, block in enumerate(self.system):
            if block.cache is not None:
                cut = index
        if cut < 0:
            return set()
        return {
            match.group(1)
            for block in self.system[: cut + 1]
            for match in VARIABLE.finditer(block.text)
        }

    def render(
        self, provider: str, model: str, variables: dict[str, str], max_tokens: int
    ) -> LLMRequest:
        """This step's model call. ``max_tokens`` is the pipeline's unless the step overrides."""
        return LLMRequest(
            provider=provider,
            model=model,
            system=[b.render(variables) for b in self.system],
            messages=[Message(role="user", blocks=[b.render(variables) for b in self.user])],
            max_tokens=self.max_tokens or max_tokens,
        )

    def arguments(self, variables: dict[str, str]) -> str:
        """What a local step was called with. Two calls with the same text are the same call."""
        return "\n\n".join(b.render(variables).text for b in self.user)

    def emitted(self, variables: dict[str, str]) -> str:
        """What a local step returns into the graph, as the workload declared it."""
        return "\n\n".join(b.render(variables).text for b in self.emits)


class PipelineSpec(BaseModel):
    """One pipeline: the prompt, the model, the cap and the contract.

    Or, since M16, a graph of steps. An empty ``steps`` is the single-call shape every workload
    had before, and it stays byte-identical: same request, same cassette key, same numbers.
    """

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
    #: The steps this pipeline runs, if it is a graph. Empty means one model call, which is
    #: what every workload was before M16 and what the demo still is.
    steps: list[StepSpec] = Field(default_factory=list)
    #: Which step's output is the pipeline's answer. Empty means the last step in run order,
    #: which is right for a graph that ends in the thing it is answering with and wrong for one
    #: that ends in a verifier — so a graph that ends in a verifier has to say so, rather than
    #: having the answer quietly become a verdict.
    output_step: str = ""

    model_config = {"frozen": True}

    @property
    def is_graph(self) -> bool:
        return bool(self.steps)

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


class EntailmentSpec(BaseModel):
    """The model call that decides whether two answers mean the same thing (U6).

    Tiny by design: the question and the two answers, no grounding document. Whether "60" and
    "sixty days" mean the same thing does not depend on the handbook, and sending it would make
    every clustering call cost what a judge call costs.
    """

    model_role: str = "cheap"
    max_tokens: int = 60
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


class SimulationSpec(BaseModel):
    """How a workload's simulated provider behaves, declared by the workload itself.

    These are invented numbers. They live here, in the workload's own file beside the
    provenance block that says the set is program-generated, because a reader auditing a result
    should find the invented parts in the workload rather than by reading the engine
    (UPGRADE_V4.md M15).
    """

    #: What each tier role gets right, as a probability.
    accuracy: dict[str, float] = Field(default_factory=dict)
    #: Per task type, where a type is harder or easier than the tier's baseline. Missing types
    #: use the tier's own rate rather than a guess.
    accuracy_by_type: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: How often an answer arrives in a form the parser cannot read.
    parse_failure_rate: float = 0.01
    notes: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}

    def accuracy_for(self, role: str, question_type: str) -> float:
        by_type = self.accuracy_by_type.get(role, {})
        if question_type in by_type:
            return by_type[question_type]
        try:
            return self.accuracy[role]
        except KeyError:
            raise WorkloadError(
                f"the workload declares no simulated accuracy for the {role!r} tier. A "
                "simulated fixture set cannot be built without saying what it is simulating."
            ) from None


class DatasetSpec(BaseModel):
    """Where a workload's tasks come from.

    ``generator`` names the code that produced them, or ``"ingested"`` when a file did and
    nothing generated anything. ``source`` is the file the engine actually reads: every workload
    is loaded from its committed dataset rather than re-generated, so the demo and an ingested
    workload travel the same path (UPGRADE_V4.md M15).
    """

    generator: str
    seed: int
    size: int
    calibration_size: int
    #: The dataset file, relative to ``data/<workload id>/``.
    source: str = "dataset.jsonl"
    #: The document every pipeline answers from, relative to the same directory, rendered into
    #: prompts as ``{{grounding}}``. Empty when a workload's tasks need no shared document.
    grounding: str = ""

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
    entailment: EntailmentSpec | None = None
    dataset_provenance: DatasetProvenanceSpec | None = None
    #: How this workload's simulated provider behaves, when it has one. Absent for a workload
    #: whose fixtures are a real recording, which needs no simulator at all.
    simulation: SimulationSpec | None = None
    #: Which directory under `fixtures/` holds this workload's recording. Empty for the demo,
    #: whose fixtures are chosen by the recording state — `demo/` when a real recording exists
    #: and `test/` otherwise. Any other workload names its own (UPGRADE_V4.md M15).
    fixtures: str = ""
    #: Where this workload was loaded from. Its dataset and grounding document sit beside it,
    #: because a workload is its definition *and* its tasks, and splitting them across two
    #: directories is how a spec starts describing a dataset that is not there.
    directory: Path | None = None
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
    pipelines = {
        pid: PipelineSpec(
            id=pid,
            **{
                **spec,
                "steps": [
                    StepSpec(**step) if isinstance(step, dict) else step
                    for step in spec.get("steps") or []
                ],
            },
        )
        for pid, spec in pipelines_raw.items()
    }
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
    entailment_raw = raw.get("entailment") or workload.get("entailment")
    entailment = EntailmentSpec(**entailment_raw) if isinstance(entailment_raw, dict) else None
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
    simulation_raw = raw.get("simulation") or workload.get("simulation")
    simulation = SimulationSpec(**simulation_raw) if isinstance(simulation_raw, dict) else None
    _validate_judging(path, grading, judge, annotation, dataset_provenance)
    _validate_entailment(path, entailment)

    return WorkloadSpec(
        id=str(workload["id"]),
        name=str(workload["name"]),
        description=str(workload["description"]),
        dataset=DatasetSpec(**workload["dataset"]),
        directory=path.parent,
        fixtures=str(workload.get("fixtures", "")),
        simulation=simulation,
        pipelines=pipelines,
        cascades=cascades,
        scorer_features=list(workload.get("scorer_features") or []),
        allowed_providers=list(workload.get("allowed_providers") or []),
        grading=grading,  # type: ignore[arg-type]
        judge=judge,
        annotation=annotation,
        entailment=entailment,
        dataset_provenance=dataset_provenance,
        margin=float(workload.get("margin", 0.03)),
        latency_sensitive=bool(workload.get("latency_sensitive", False)),
    )


def _validate_entailment(path: Path, entailment: EntailmentSpec | None) -> None:
    """An entailment prompt that cannot see both answers is not judging entailment."""
    if entailment is None:
        return
    rendered = "".join(block.text for block in [*entailment.system, *entailment.user])
    for required in ("{{question}}", "{{answer_a}}", "{{answer_b}}"):
        if required not in rendered:
            raise WorkloadError(
                f"{path}: the entailment prompt never references {required}. Deciding whether "
                "two answers mean the same thing needs both of them and the question they "
                "answer."
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
