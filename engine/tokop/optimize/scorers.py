"""Scorers: estimate whether an answer is right, without ever seeing whether it is.

This is the load-bearing distinction in the cascade (SPEC.md 7.5). A **checker**
(``workloads/grading.py``) sees the gold answer and decides whether a task succeeded. A
**scorer** lives here, never sees gold, and guesses — because at run time, on a task nobody has
graded, a guess is all there is. A scorer that could reach a gold answer would make every
measurement in the product circular, so they are separate modules with separate tests and
``tests/test_scorer_isolation.py`` fails if a scorer can reach one.

The model is an L2-regularized logistic regression per tier, fitted on the **calibration split
only**, over deterministic features a workload's YAML selects from a registry. Logistic
regression rather than something stronger for three reasons: it needs about a hundred labelled
examples rather than thousands, its coefficients can be read and argued with, and its output is
a calibrated-ish probability, which is what a threshold needs to mean anything.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from tokop.optimize.entailment import (
    ClusterResult,
    EntailmentFn,
    cluster_by_entailment,
    semantic_entropy,
)


class ScorerError(ValueError):
    """A scorer could not be fitted or applied."""


#: The scorer the cascade shipped with, and still its default. Named rather than assumed so a
#: pipeline can ask for a different one and so a stored result records which one produced it.
LOGISTIC_V1 = "logistic-v1"
SELF_CONSISTENCY_V1 = "self-consistency-v1"
SEMANTIC_ENTROPY_V1 = "semantic-entropy-v1"
SEP_V1 = "sep-v1"


#: Whether two candidate answers mean the same thing. Supplied by the workload, never imported
#: here: the grader's comparisons take a candidate and a reference, and a scorer module that
#: could import one would be one edit away from reading the answer key. Injection keeps the
#: isolation structural. Both arguments are model outputs; neither is a reference answer.
EquivalenceFn = Callable[[str, str], bool]


@dataclass(frozen=True)
class TaskView:
    """Everything a scorer is allowed to see about a task.

    Deliberately not the ``DemoItem``: there is no ``gold``, no ``answer_type``, no
    ``question_type`` and no ``sections`` field to reach for, so scorer code cannot accidentally
    depend on one. The isolation is structural, not a convention.
    """

    task_id: str
    question: str
    output: str
    #: The grounding document, for features that check a quote against it. Never gold.
    context: str = ""
    tier: str = ""
    latency_ms: float = 0.0
    output_tokens: int = 0
    #: Repeated generations for this task at this tier, when the cascade drew any. ``samples[0]``
    #: is ``output``. Empty when the tier was called once, which is the ordinary case: a scorer
    #: that needs repeats must say so rather than assume it got them.
    samples: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for forbidden in ("gold", "correct", "answer_type", "question_type", "sections"):
            if forbidden in self.__dict__:  # pragma: no cover - defensive
                raise ScorerError(f"a TaskView must never carry {forbidden!r}")


FeatureFn = Callable[[TaskView], float]


@dataclass(frozen=True)
class Feature:
    """A named deterministic feature over a task view."""

    name: str
    description: str
    fn: FeatureFn
    #: Demo-specific features are not offered to arbitrary workloads.
    workload: str | None = None


# --------------------------------------------------------------------------- generic features


def _parsed(view: TaskView) -> dict[str, Any] | None:
    from tokop.workloads.grading import extract_json_answer

    return extract_json_answer(view.output)


def f_output_parses(view: TaskView) -> float:
    """Whether an answer could be extracted at all.

    The single most informative generic feature: a model that could not follow the output
    contract usually could not follow the reasoning either.
    """
    from tokop.workloads.grading import extract_answer

    return 1.0 if extract_answer(view.output) is not None else 0.0


_SHAPES = (
    ("number", re.compile(r"^-?\$?\d[\d,]*\.?\d*%?$")),
    ("yes_no", re.compile(r"^(?:yes|no)$", re.I)),
    ("enum", re.compile(r"^[a-z]+_\d+$", re.I)),
)


def f_answer_shape_is_clean(view: TaskView) -> float:
    """Whether the extracted answer is a bare value rather than a sentence.

    Note what this does *not* do: it does not know what shape the question wanted. It only
    rewards an answer that is a value rather than prose, which a model that understood the
    contract produces and a model that waffled does not.
    """
    from tokop.workloads.grading import extract_answer

    answer = extract_answer(view.output)
    if answer is None:
        return 0.0
    answer = answer.strip()
    if any(pattern.match(answer) for _, pattern in _SHAPES):
        return 1.0
    return 1.0 if len(answer.split()) <= 3 else 0.0


def f_output_length(view: TaskView) -> float:
    return float(len(view.output))


def f_question_length(view: TaskView) -> float:
    return float(len(view.question))


def f_question_numerals(view: TaskView) -> float:
    """How many numbers the question contains. A proxy for arithmetic, which is where cheap
    models fall away."""
    return float(len(re.findall(r"\d+", view.question)))


def f_output_tokens(view: TaskView) -> float:
    return float(view.output_tokens)


def f_has_evidence_field(view: TaskView) -> float:
    payload = _parsed(view)
    return 1.0 if payload and str(payload.get("evidence", "")).strip() else 0.0


# ---------------------------------------------------------------- demo-specific feature


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def f_evidence_quote_found(view: TaskView) -> float:
    """Whether the evidence quote appears verbatim inside the section the answer itself cites.

    This compares the quote with the handbook and with nothing else. It never looks at gold, and
    it cannot: it does not know what the right answer is, only whether the model's own citation
    holds up. A model that invented a quote, or cited a section that does not contain it, is
    telling you it did not find the rule — which turns out to predict being wrong.
    """
    payload = _parsed(view)
    if not payload or not view.context:
        return 0.0
    quote = str(payload.get("evidence", "")).strip()
    section = str(payload.get("section", "")).strip()
    if not quote or not section:
        return 0.0

    from tokop.workloads.demo.handbook import section_text

    body = section_text(view.context, section)
    if not body:
        return 0.0
    return 1.0 if _normalize_ws(quote) in _normalize_ws(body) else 0.0


def f_cites_a_real_section(view: TaskView) -> float:
    payload = _parsed(view)
    if not payload or not view.context:
        return 0.0
    section = str(payload.get("section", "")).strip()
    if not section:
        return 0.0
    from tokop.workloads.demo.handbook import section_text

    return 1.0 if section_text(view.context, section) else 0.0


FEATURE_REGISTRY: dict[str, Feature] = {
    f.name: f
    for f in (
        Feature("output_parses", "An answer could be extracted from the output", f_output_parses),
        Feature(
            "answer_type_matches",
            "The answer is a bare value rather than prose",
            f_answer_shape_is_clean,
        ),
        Feature("output_length", "Characters of output", f_output_length),
        Feature("question_length", "Characters of question", f_question_length),
        Feature("question_numerals", "Count of numerals in the question", f_question_numerals),
        Feature("output_tokens", "Output tokens reported by the provider", f_output_tokens),
        Feature(
            "has_evidence_field",
            "The output carried a non-empty evidence field",
            f_has_evidence_field,
        ),
        Feature(
            "evidence_quote_found",
            "The evidence quote appears verbatim in the section the answer cites",
            f_evidence_quote_found,
            workload="returns-support",
        ),
        Feature(
            "cites_a_real_section",
            "The cited section exists in the handbook",
            f_cites_a_real_section,
            workload="returns-support",
        ),
    )
}


def resolve_features(names: Sequence[str]) -> list[Feature]:
    missing = [n for n in names if n not in FEATURE_REGISTRY]
    if missing:
        raise ScorerError(
            f"unknown scorer feature(s) {missing}; the registry offers {sorted(FEATURE_REGISTRY)}"
        )
    return [FEATURE_REGISTRY[n] for n in names]


def extract(views: Sequence[TaskView], features: Sequence[Feature]) -> np.ndarray:
    return np.array([[f.fn(v) for f in features] for v in views], dtype=float)


@dataclass
class TierScorer:
    """A fitted scorer for one cascade tier."""

    tier: str
    feature_names: tuple[str, ...]
    model: LogisticRegression
    scaler: StandardScaler
    fitted_on: int
    train_positive_rate: float
    #: Set when the calibration labels were all one class, so no discrimination is possible.
    degenerate: bool = False
    auroc: float | None = None
    kind: str = LOGISTIC_V1

    def score(self, views: Sequence[TaskView]) -> np.ndarray:
        """P(correct) for each view, in [0, 1]."""
        if not views:
            return np.array([], dtype=float)
        if self.degenerate:
            # Every calibration example had the same label. Returning the base rate is the only
            # honest answer; pretending to discriminate would be worse than useless.
            return np.full(len(views), self.train_positive_rate, dtype=float)
        features = resolve_features(self.feature_names)
        matrix = self.scaler.transform(extract(views, features))
        probabilities: np.ndarray = self.model.predict_proba(matrix)[:, 1]
        return probabilities

    def extra_cost(self, view: TaskView) -> Decimal:
        """Nothing beyond the tier's own call: every feature is computed locally."""
        return Decimal(0)

    def cluster_sizes(self, views: Sequence[TaskView]) -> list[list[int]] | None:
        """None: this scorer reads one generation, so there is nothing to agree with."""
        return None

    def answer_index(self, view: TaskView) -> int:
        """Sample 0: this scorer reads one call and returns what it said."""
        return 0

    def coefficients(self) -> dict[str, float]:
        """What the scorer learned, so a reader can argue with it."""
        if self.degenerate:
            return dict.fromkeys(self.feature_names, 0.0)
        return {
            name: float(coefficient)
            for name, coefficient in zip(self.feature_names, self.model.coef_[0], strict=True)
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tier": self.tier,
            "features": list(self.feature_names),
            "fitted_on": self.fitted_on,
            "auroc": self.auroc,
            "degenerate": self.degenerate,
            "coefficients": self.coefficients(),
        }


MIN_CALIBRATION_TASKS = 30


@dataclass
class ScorerWarning:
    """Something the caller must be told about a fit."""

    tier: str
    message: str


def fit_tier_scorer(
    tier: str,
    views: Sequence[TaskView],
    labels: Sequence[int],
    feature_names: Sequence[str],
    *,
    C: float = 1.0,
    seed: int = 20260911,
    sample_weights: Sequence[float] | None = None,
) -> tuple[TierScorer, list[ScorerWarning]]:
    """Fit one tier's scorer on the calibration split.

    ``labels`` come from the checker, not from a scorer: fitting is the *only* moment a scorer
    is allowed anywhere near a grade, and even then it sees the label, never the gold answer.

    ``sample_weights`` lets a caller say how much each label is worth. A gold label is worth one;
    a pseudo-label standing in for one is worth what the consensus behind it is worth
    (UPGRADE_V3.md U3, ``optimize/pseudolabels.py``).
    """
    if len(views) != len(labels):
        raise ScorerError(f"{len(views)} views but {len(labels)} labels")
    if sample_weights is not None and len(sample_weights) != len(views):
        raise ScorerError(f"{len(views)} views but {len(sample_weights)} weights")
    if not views:
        raise ScorerError("a scorer needs at least one calibration example")

    warnings: list[ScorerWarning] = []
    if len(views) < MIN_CALIBRATION_TASKS:
        warnings.append(
            ScorerWarning(
                tier,
                f"only {len(views)} calibration tasks. A cascade needs labelled examples from "
                f"the same distribution it will serve; below about {MIN_CALIBRATION_TASKS} the "
                "thresholds are fitted to noise.",
            )
        )

    features = resolve_features(feature_names)
    matrix = extract(views, features)
    y = np.asarray(labels, dtype=int)
    positive_rate = float(y.mean())

    scaler = StandardScaler()
    if len(set(y.tolist())) < 2:
        warnings.append(
            ScorerWarning(
                tier,
                f"every calibration example at this tier had the same outcome "
                f"({'all correct' if positive_rate else 'all wrong'}), so the scorer cannot "
                "discriminate and returns the base rate.",
            )
        )
        scaler.fit(matrix if len(matrix) else np.zeros((1, len(features))))
        return (
            TierScorer(
                tier=tier,
                feature_names=tuple(feature_names),
                model=LogisticRegression(),
                scaler=scaler,
                fitted_on=len(views),
                train_positive_rate=positive_rate,
                degenerate=True,
            ),
            warnings,
        )

    scaled = scaler.fit_transform(matrix)
    # L2 is scikit-learn's default penalty; passing penalty="l2" explicitly is deprecated from
    # 1.8 and removed in 1.10, so the regularization is L2 by default rather than by argument.
    model = LogisticRegression(C=C, max_iter=1000, random_state=seed)
    model.fit(scaled, y, sample_weight=None if sample_weights is None else list(sample_weights))
    return (
        TierScorer(
            tier=tier,
            feature_names=tuple(feature_names),
            model=model,
            scaler=scaler,
            fitted_on=len(views),
            train_positive_rate=positive_rate,
        ),
        warnings,
    )


def evaluate_auroc(
    scorer: Scorer, views: Sequence[TaskView], labels: Sequence[int]
) -> float | None:
    """AUROC on the **test** split, reported alongside every cascade result (SPEC.md 7.5).

    ``None`` when the labels are all one class, where AUROC is undefined — reporting 0.5 there
    would imply the scorer was measured and found useless, which is not what happened.
    """
    y = np.asarray(labels, dtype=int)
    if len(set(y.tolist())) < 2:
        return None
    return float(roc_auc_score(y, scorer.score(views)))


@dataclass
class ScorerBundle:
    """One scorer per tier, plus whatever the caller must be told."""

    scorers: dict[str, Scorer]
    warnings: list[ScorerWarning] = field(default_factory=list)

    def score(self, tier: str, views: Sequence[TaskView]) -> np.ndarray:
        try:
            return self.scorers[tier].score(views)
        except KeyError:
            raise ScorerError(
                f"no scorer fitted for tier {tier!r}; fitted tiers: "
                f"{', '.join(sorted(self.scorers))}"
            ) from None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tiers": {tier: scorer.as_dict() for tier, scorer in self.scorers.items()},
            "warnings": [{"tier": w.tier, "message": w.message} for w in self.warnings],
        }


# --------------------------------------------------------------------------- the interface


#: Everything a cascade needs from a scorer, whatever is behind it. Deliberately small: fit on
#: calibration, score views, describe yourself. A kind that needs more than a ``TaskView`` —
#: repeated samples, say — asks for it through ``ScorerContext`` and refuses if it is missing,
#: rather than quietly scoring something weaker than advertised.
class Scorer(Protocol):
    tier: str
    kind: str
    auroc: float | None

    def score(self, views: Sequence[TaskView]) -> np.ndarray:
        """P(correct) for each view, in [0, 1]."""
        ...

    def answer_index(self, view: TaskView) -> int:
        """Which recorded generation this scorer returns as the tier's answer.

        0 for any scorer that reads a single call. A scorer that samples may return a different
        one, and the checker then grades *that* answer rather than the first — otherwise the
        cascade would be charged for sampling and graded as though it had not sampled.
        """
        ...

    def extra_cost(self, view: TaskView) -> Decimal:
        """Money this scorer spends per scored task **beyond** the tier's own generations.

        On the protocol rather than bolted on at the call site, because a scorer that spends
        money the cascade is not charged for is exactly how a comparison stops meaning
        anything (DECISIONS.md D27). Zero for a scorer that reads what is already there.
        """
        ...

    def cluster_sizes(self, views: Sequence[TaskView]) -> list[list[int]] | None:
        """How this scorer's k samples grouped, per task, or ``None`` if it does not sample.

        Read by the effective-k measurement (UPGRADE_V3.md U6): how much k repeated samples
        agree with each other is what says whether they are worth k draws or one.
        """
        ...

    def as_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ScorerContext:
    """What a scorer kind may need beyond the calibration data itself.

    One object rather than a widening parameter list, because each kind reads a different
    subset of it: ``logistic-v1`` uses ``feature_names`` and ``seed`` and ignores the rest;
    ``self-consistency-v1`` uses ``k`` and ``equivalence`` and ignores the features.
    """

    feature_names: tuple[str, ...] = ()
    #: Supplied by the workload. ``None`` means the workload defined no answer equivalence, and
    #: a kind that needs one must refuse rather than fall back to string equality.
    equivalence: EquivalenceFn | None = None
    seed: int = 20260911
    #: Whether two answers mean the same thing, judged by a model (UPGRADE_V3.md U6). Supplied
    #: by the caller from the workload's `entailment:` block, never imported: the same isolation
    #: the answer-equivalence relation has, for the same reason.
    entailment: EntailmentFn | None = None
    #: The mean recorded price of one entailment call, charged per call a clustering makes.
    entailment_price: Decimal = Decimal(0)
    #: Models in this registry that expose hidden states. Empty behind an API, which is what
    #: makes `sep-v1` refuse rather than approximate.
    hidden_state_models: tuple[str, ...] = ()
    #: Samples drawn per scored task at serving time. 1 is one call, the ordinary case.
    k: int = 1
    C: float = 1.0
    #: Per-example fitting weights, one per calibration view. ``None`` means every example
    #: counts equally, which is what a gold label deserves. Pseudo-labels do not: a label
    #: standing in for an answer key is only as good as the consensus behind it, so
    #: ``optimize/pseudolabels.py`` supplies a weight per example and the fit honours it
    #: (UPGRADE_V3.md U3).
    sample_weights: tuple[float, ...] | None = None


FitFn = Callable[
    [str, Sequence[TaskView], Sequence[int], ScorerContext],
    tuple[Scorer, list[ScorerWarning]],
]


@dataclass(frozen=True)
class ScorerKind:
    """One registered scorer implementation."""

    name: str
    description: str
    fit: FitFn
    #: Calls this kind makes per scored task at serving time, as a function of ``k``. The
    #: cascade charges this, so a kind that samples cannot hide what it costs.
    calls_per_task: Callable[[int], int]

    def method_note(self, k: int) -> str:
        """How this kind estimates, named for the caller (non-negotiable 2)."""
        calls = self.calls_per_task(k)
        suffix = "1 call per scored task" if calls == 1 else f"{calls} calls per scored task"
        return f"{self.description} ({suffix})"


SCORER_REGISTRY: dict[str, ScorerKind] = {}


def register_scorer(kind: ScorerKind) -> None:
    if kind.name in SCORER_REGISTRY:
        raise ScorerError(f"scorer {kind.name!r} is already registered")
    SCORER_REGISTRY[kind.name] = kind


def scorer_kind(name: str) -> ScorerKind:
    try:
        return SCORER_REGISTRY[name]
    except KeyError:
        raise ScorerError(
            f"unknown scorer {name!r}; the registry offers {', '.join(sorted(SCORER_REGISTRY))}"
        ) from None


def fit_scorer(
    name: str,
    tier: str,
    views: Sequence[TaskView],
    labels: Sequence[int],
    context: ScorerContext,
) -> tuple[Scorer, list[ScorerWarning]]:
    """Fit whichever scorer the pipeline named, on the calibration split."""
    return scorer_kind(name).fit(tier, views, labels, context)


def _fit_logistic(
    tier: str,
    views: Sequence[TaskView],
    labels: Sequence[int],
    context: ScorerContext,
) -> tuple[Scorer, list[ScorerWarning]]:
    scorer, warnings = fit_tier_scorer(
        tier,
        views,
        labels,
        context.feature_names,
        C=context.C,
        seed=context.seed,
        sample_weights=context.sample_weights,
    )
    return scorer, warnings


register_scorer(
    ScorerKind(
        name=LOGISTIC_V1,
        description=(
            "L2-regularized logistic regression over deterministic features, fitted per tier "
            "on the calibration split"
        ),
        fit=_fit_logistic,
        calls_per_task=lambda k: 1,
    )
)


# ------------------------------------------------------------------- self-consistency-v1


def group_samples(samples: Sequence[str], equivalence: EquivalenceFn) -> list[int]:
    """Group repeated generations by meaning and return the group sizes, largest first.

    Each sample is compared against one representative per group. That is exact here rather
    than an approximation, because the supplied relation is a genuine equivalence — reflexive,
    symmetric and transitive — so the grouping is the same whatever order the samples arrive
    in. A relation that only approximates meaning, such as model-judged entailment, is *not*
    transitive, and clustering under one needs more care than this function takes. That is part
    of why this scorer does not claim to implement one (DECISIONS.md D27).
    """
    return sorted((len(g) for g in group_indices(samples, equivalence)), reverse=True)


def group_indices(samples: Sequence[str], equivalence: EquivalenceFn) -> list[list[int]]:
    """The same grouping, as sample positions, in first-appearance order."""
    groups: list[list[int]] = []
    for index, sample in enumerate(samples):
        for group in groups:
            if equivalence(samples[group[0]], sample):
                group.append(index)
                break
        else:
            groups.append([index])
    return groups


def majority_index(samples: Sequence[str], equivalence: EquivalenceFn) -> int:
    """Which sample a self-consistency scorer returns: a member of the largest group.

    Ties go to the group that appeared first, which is the earliest sample drawn. Any
    tie-break is arbitrary; this one is at least deterministic and does not need a random
    seed to reproduce.
    """
    groups = group_indices(samples, equivalence)
    if not groups:
        return 0
    return max(groups, key=len)[0]


def discrete_entropy(sizes: Sequence[int]) -> float:
    """Shannon entropy over group proportions, normalized to [0, 1].

    Probabilities come from **generation counts** rather than from token likelihoods, which is
    what makes this computable behind a provider API at all: the log-probabilities a
    length-normalized sequence entropy would need are not something every provider returns.

    Normalized by ``log(k)``, the most entropy k samples can carry, so that the number means
    the same thing at different sample counts and can be thresholded on one grid. 0 is total
    agreement; 1 is k samples that all disagree.
    """
    total = sum(sizes)
    if total <= 1 or len(sizes) <= 1:
        return 0.0
    proportions = [size / total for size in sizes if size]
    entropy = -sum(p * np.log(p) for p in proportions)
    return float(entropy / np.log(total))


@dataclass
class SelfConsistencyScorer:
    """Routes on how much a tier agrees with itself across k repeated generations.

    The estimate is the normalized entropy of the sample grouping, mapped to a probability by a
    one-feature logistic fit on the calibration split — the same split, and the same moment,
    that every other scorer here is fitted on. The mapping is monotone, so it changes what the
    threshold *means* without changing the ranking or the AUROC; it exists so that a score is a
    probability, as the rest of the cascade assumes.

    This is **self-consistency over an exact-match equivalence relation, a deterministic
    approximation of semantic entropy** — not semantic entropy. The method it approximates
    clusters by model-judged meaning, which catches two differently-worded answers that say the
    same thing; an exact-match relation over extracted answers does not, and will read a
    paraphrase as disagreement. On a workload whose answers are numbers, enums and yes/no that
    gap is narrow. On free-form text it is not. DECISIONS.md D27 records the departure.
    """

    tier: str
    k: int
    equivalence: EquivalenceFn
    model: LogisticRegression
    scaler: StandardScaler
    fitted_on: int
    train_positive_rate: float
    degenerate: bool = False
    auroc: float | None = None
    kind: str = SELF_CONSISTENCY_V1

    def entropies(self, views: Sequence[TaskView]) -> np.ndarray:
        values = []
        for view in views:
            samples = require_samples(view, self.k)
            values.append(discrete_entropy(group_samples(samples, self.equivalence)))
        return np.array(values, dtype=float).reshape(-1, 1)

    def score(self, views: Sequence[TaskView]) -> np.ndarray:
        """P(correct) for each view, in [0, 1]."""
        if not views:
            return np.array([], dtype=float)
        if self.degenerate:
            return np.full(len(views), self.train_positive_rate, dtype=float)
        scaled = self.scaler.transform(self.entropies(views))
        probabilities: np.ndarray = self.model.predict_proba(scaled)[:, 1]
        return probabilities

    def extra_cost(self, view: TaskView) -> Decimal:
        """Nothing beyond the k generations, which the cascade already charges for."""
        return Decimal(0)

    def cluster_sizes(self, views: Sequence[TaskView]) -> list[list[int]] | None:
        return [group_samples(require_samples(view, self.k), self.equivalence) for view in views]

    def answer_index(self, view: TaskView) -> int:
        """The majority answer, which is the point: k samples buy a vote, not just a score."""
        return majority_index(require_samples(view, self.k), self.equivalence)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tier": self.tier,
            "k": self.k,
            "features": ["sample_entropy"],
            "fitted_on": self.fitted_on,
            "auroc": self.auroc,
            "degenerate": self.degenerate,
            "coefficients": {
                "sample_entropy": (0.0 if self.degenerate else float(self.model.coef_[0][0]))
            },
        }


def require_samples(view: TaskView, k: int) -> tuple[str, ...]:
    """The k repeats this scorer was promised, or a refusal that says what is missing."""
    if len(view.samples) < k:
        raise ScorerError(
            f"task {view.task_id!r} carries {len(view.samples)} sample(s) but "
            f"{SELF_CONSISTENCY_V1} was asked for {k}. A sampling scorer cannot be run over a "
            "response matrix that was recorded with fewer repeats than it needs; re-record the "
            "matrix at the depth the cascade asks for."
        )
    return view.samples[:k]


def fit_self_consistency(
    tier: str,
    views: Sequence[TaskView],
    labels: Sequence[int],
    context: ScorerContext,
) -> tuple[Scorer, list[ScorerWarning]]:
    """Fit the entropy-to-probability mapping on the calibration split."""
    if context.equivalence is None:
        raise ScorerError(
            f"{SELF_CONSISTENCY_V1} needs the workload's answer-equivalence function and this "
            "workload supplies none. It groups repeated generations by whether they mean the "
            "same thing; without that relation there is nothing to group by. Falling back to "
            "string equality would silently score a different quantity — two answers that a "
            "grader would mark identical would be counted as disagreement — so this refuses "
            "instead. Give the workload an equivalence function, or name a different scorer."
        )
    if context.k < 2:
        raise ScorerError(
            f"{SELF_CONSISTENCY_V1} needs at least 2 samples per task to measure disagreement; "
            f"the cascade asks for {context.k}. One sample always agrees with itself."
        )
    if len(views) != len(labels):
        raise ScorerError(f"{len(views)} views but {len(labels)} labels")
    if not views:
        raise ScorerError("a scorer needs at least one calibration example")

    equivalence = context.equivalence
    warnings: list[ScorerWarning] = []
    if len(views) < MIN_CALIBRATION_TASKS:
        warnings.append(
            ScorerWarning(
                tier,
                f"only {len(views)} calibration tasks. A cascade needs labelled examples from "
                f"the same distribution it will serve; below about {MIN_CALIBRATION_TASKS} the "
                "thresholds are fitted to noise.",
            )
        )

    matrix = np.array(
        [
            [discrete_entropy(group_samples(require_samples(view, context.k), equivalence))]
            for view in views
        ],
        dtype=float,
    )
    y = np.asarray(labels, dtype=int)
    positive_rate = float(y.mean())
    scaler = StandardScaler()

    spread = float(matrix.std())
    if len(set(y.tolist())) < 2 or spread == 0.0:
        if spread == 0.0 and len(set(y.tolist())) >= 2:
            warnings.append(
                ScorerWarning(
                    tier,
                    f"every calibration task produced the same sample entropy at k="
                    f"{context.k}, so agreement cannot discriminate here and the scorer returns "
                    "the base rate. Either the tier is perfectly consistent on this split or k "
                    "is too small to separate anything.",
                )
            )
        else:
            warnings.append(
                ScorerWarning(
                    tier,
                    f"every calibration example at this tier had the same outcome "
                    f"({'all correct' if positive_rate else 'all wrong'}), so the scorer cannot "
                    "discriminate and returns the base rate.",
                )
            )
        scaler.fit(matrix if len(matrix) else np.zeros((1, 1)))
        return (
            SelfConsistencyScorer(
                tier=tier,
                k=context.k,
                equivalence=equivalence,
                model=LogisticRegression(),
                scaler=scaler,
                fitted_on=len(views),
                train_positive_rate=positive_rate,
                degenerate=True,
            ),
            warnings,
        )

    scaled = scaler.fit_transform(matrix)
    model = LogisticRegression(C=context.C, max_iter=1000, random_state=context.seed)
    model.fit(
        scaled,
        y,
        sample_weight=None if context.sample_weights is None else list(context.sample_weights),
    )
    return (
        SelfConsistencyScorer(
            tier=tier,
            k=context.k,
            equivalence=equivalence,
            model=model,
            scaler=scaler,
            fitted_on=len(views),
            train_positive_rate=positive_rate,
        ),
        warnings,
    )


register_scorer(
    ScorerKind(
        name=SELF_CONSISTENCY_V1,
        description=(
            "self-consistency over an exact-match equivalence relation, a deterministic "
            "approximation of semantic entropy: k samples per task, grouped by answer "
            "equivalence, scored by the normalized entropy of the group proportions"
        ),
        fit=fit_self_consistency,
        calls_per_task=lambda k: k,
    )
)


# ------------------------------------------------------------------- semantic-entropy-v1


def answers_of(view: TaskView, k: int) -> tuple[str | None, ...]:
    """The k generations as *answers*, not as raw outputs.

    Clustering the raw JSON would compare the evidence quote and the section id as well as the
    answer, and two correct answers citing different sentences would land in different meaning
    clusters. The extraction is the output *contract*'s parser, which a scorer may use — reading
    JSON out of a fenced block is not knowledge of the answer key. ``None`` is an output nothing
    could be read out of, and the clustering keeps those apart rather than merging them.
    """
    from tokop.workloads.grading import extract_answer

    return tuple(extract_answer(sample) for sample in require_samples(view, k))


@dataclass
class SemanticEntropyScorer:
    """Routes on entropy over **meanings** rather than over strings (UPGRADE_V3.md U6).

    The same shape as ``self-consistency-v1`` with the grouping replaced: k samples per task,
    clustered by bidirectional entailment judged by a cheap model, and the normalized entropy of
    the cluster proportions mapped to a probability by a one-feature logistic fit on the
    calibration split.

    The difference is the one D27 named and could not close: an exact-match relation reads a
    paraphrase as disagreement, and entailment does not. What it costs is calls — two directed
    ones per candidate pair, charged to this scorer like the samples are — and what it gives up
    is transitivity, so the clustering is first-fit and order-dependent. Both are stated in
    ``optimize/entailment.py`` rather than discovered.
    """

    tier: str
    k: int
    entailment: EntailmentFn
    model: LogisticRegression
    scaler: StandardScaler
    fitted_on: int
    train_positive_rate: float
    price_per_call: Decimal = Decimal(0)
    degenerate: bool = False
    auroc: float | None = None
    kind: str = SEMANTIC_ENTROPY_V1

    def _clusters(self, view: TaskView) -> ClusterResult:
        return cluster_by_entailment(view.question, answers_of(view, self.k), self.entailment)

    def entropies(self, views: Sequence[TaskView]) -> np.ndarray:
        values = [semantic_entropy(self._clusters(view).sizes) for view in views]
        return np.array(values, dtype=float).reshape(-1, 1)

    def cluster_sizes(self, views: Sequence[TaskView]) -> list[list[int]] | None:
        """Per-task meaning-cluster sizes, for the effective-k measurement."""
        return [self._clusters(view).sizes for view in views]

    def score(self, views: Sequence[TaskView]) -> np.ndarray:
        if not views:
            return np.array([], dtype=float)
        if self.degenerate:
            return np.full(len(views), self.train_positive_rate, dtype=float)
        scaled = self.scaler.transform(self.entropies(views))
        probabilities: np.ndarray = self.model.predict_proba(scaled)[:, 1]
        return probabilities

    def extra_cost(self, view: TaskView) -> Decimal:
        """The entailment calls this task's clustering made, at the recorded mean price.

        Mean rather than per-call because entailment prompts are near-identical in size — the
        question and two short answers — so the spread is small and the alternative is carrying
        a price for every pair through three layers. The method note says which it is.
        """
        return Decimal(self._clusters(view).calls) * self.price_per_call

    def answer_index(self, view: TaskView) -> int:
        """A member of the largest meaning cluster: k samples buy a vote, not just a score."""
        clusters = self._clusters(view)
        if not clusters.groups:  # pragma: no cover - a view always has at least one sample
            return 0
        largest = max(clusters.groups, key=len)
        return largest[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tier": self.tier,
            "k": self.k,
            "features": ["semantic_entropy"],
            "fitted_on": self.fitted_on,
            "auroc": self.auroc,
            "degenerate": self.degenerate,
            "coefficients": {
                "semantic_entropy": (0.0 if self.degenerate else float(self.model.coef_[0][0]))
            },
        }


def fit_semantic_entropy(
    tier: str,
    views: Sequence[TaskView],
    labels: Sequence[int],
    context: ScorerContext,
) -> tuple[Scorer, list[ScorerWarning]]:
    if context.entailment is None:
        raise ScorerError(
            f"{SEMANTIC_ENTROPY_V1} needs an entailment function and none was supplied. It "
            "clusters repeated generations by whether they mean the same thing, which is a "
            "model's judgement and not a string comparison; falling back to exact match would "
            "silently make this `self-consistency-v1` under a different name, which is the one "
            "thing D27 was careful to keep apart. Define an `entailment:` block on the workload, "
            "or name a different scorer."
        )
    if context.k < 2:
        raise ScorerError(
            f"{SEMANTIC_ENTROPY_V1} needs at least 2 samples per task to measure disagreement; "
            f"the cascade asks for {context.k}. One sample always agrees with itself."
        )
    if len(views) != len(labels):
        raise ScorerError(f"{len(views)} views but {len(labels)} labels")
    if not views:
        raise ScorerError("a scorer needs at least one calibration example")

    entailment = context.entailment
    warnings: list[ScorerWarning] = []
    if len(views) < MIN_CALIBRATION_TASKS:
        warnings.append(
            ScorerWarning(
                tier,
                f"only {len(views)} calibration tasks. A cascade needs labelled examples from "
                f"the same distribution it will serve; below about {MIN_CALIBRATION_TASKS} the "
                "thresholds are fitted to noise.",
            )
        )

    matrix = np.array(
        [
            [
                semantic_entropy(
                    cluster_by_entailment(
                        view.question, answers_of(view, context.k), entailment
                    ).sizes
                )
            ]
            for view in views
        ],
        dtype=float,
    )
    y = np.asarray(labels, dtype=int)
    positive_rate = float(y.mean())
    scaler = StandardScaler()
    spread = float(matrix.std())

    if len(set(y.tolist())) < 2 or spread == 0.0:
        warnings.append(
            ScorerWarning(
                tier,
                (
                    f"every calibration task produced the same semantic entropy at k="
                    f"{context.k}, so meaning-level agreement cannot discriminate here and the "
                    "scorer returns the base rate."
                    if spread == 0.0
                    else f"every calibration example at this tier had the same outcome "
                    f"({'all correct' if positive_rate else 'all wrong'}), so the scorer cannot "
                    "discriminate and returns the base rate."
                ),
            )
        )
        scaler.fit(matrix if len(matrix) else np.zeros((1, 1)))
        return (
            SemanticEntropyScorer(
                tier=tier,
                k=context.k,
                entailment=entailment,
                model=LogisticRegression(),
                scaler=scaler,
                fitted_on=len(views),
                train_positive_rate=positive_rate,
                price_per_call=context.entailment_price,
                degenerate=True,
            ),
            warnings,
        )

    scaled = scaler.fit_transform(matrix)
    model = LogisticRegression(C=context.C, max_iter=1000, random_state=context.seed)
    model.fit(
        scaled,
        y,
        sample_weight=None if context.sample_weights is None else list(context.sample_weights),
    )
    return (
        SemanticEntropyScorer(
            tier=tier,
            k=context.k,
            entailment=entailment,
            model=model,
            scaler=scaler,
            fitted_on=len(views),
            train_positive_rate=positive_rate,
            price_per_call=context.entailment_price,
        ),
        warnings,
    )


register_scorer(
    ScorerKind(
        name=SEMANTIC_ENTROPY_V1,
        description=(
            "discrete semantic entropy: k samples per task, clustered by bidirectional "
            "entailment judged by a cheap model, scored by the normalized entropy of the "
            "meaning-cluster proportions"
        ),
        fit=fit_semantic_entropy,
        # k generations plus, in the worst case, two directed entailment calls for every pair.
        # Identical answers are compared for free, so the realised count is far lower and is
        # charged from the recording rather than from this bound.
        calls_per_task=lambda k: k + k * (k - 1),
    )
)


# ----------------------------------------------------------------------------- sep-v1


def fit_semantic_entropy_probe(
    tier: str,
    views: Sequence[TaskView],
    labels: Sequence[int],
    context: ScorerContext,
) -> tuple[Scorer, list[ScorerWarning]]:
    """Semantic Entropy Probes: a linear probe on hidden states (UPGRADE_V3.md U6).

    Registered so that asking for it gets an answer, and it always refuses here. Kossen et al.
    show semantic entropy is recoverable from the hidden states of a *single* generation, which
    removes the five-to-tenfold sampling cost this file's other sampling scorer pays — and
    hidden states are exactly what no provider API returns. There is nothing to approximate: a
    probe without hidden states is not a worse probe, it is a different method wearing the name.

    D27.4 settled this once: Tokop is official-APIs-only (non-negotiable 6), so this is not a
    "not yet". It becomes runnable the day a model in the registry declares hidden-state access,
    and refuses with that sentence until then.
    """
    available = sorted(context.hidden_state_models)
    raise ScorerError(
        f"{SEP_V1} reads a model's hidden states to predict semantic entropy from one "
        "generation, and no model in this registry exposes them"
        + (f" (models that do: {', '.join(available)})" if available else "")
        + ". Tokop uses official APIs only (SPEC.md non-negotiable 6) and no provider API "
        "returns hidden states, so this refuses rather than approximating: a probe fitted on "
        "something other than hidden states is a different method under the same name. Use "
        f"{SEMANTIC_ENTROPY_V1}, which needs only generation counts, or self-host a model and "
        "declare `hidden_states: true` for it."
    )


register_scorer(
    ScorerKind(
        name=SEP_V1,
        description=(
            "semantic entropy probe: a linear probe on hidden states predicting semantic "
            "entropy from a single generation. Self-hosted models only; refuses behind an API"
        ),
        fit=fit_semantic_entropy_probe,
        calls_per_task=lambda k: 1,
    )
)
