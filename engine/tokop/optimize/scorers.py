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
from typing import Any, Protocol

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler


class ScorerError(ValueError):
    """A scorer could not be fitted or applied."""


#: The scorer the cascade shipped with, and still its default. Named rather than assumed so a
#: pipeline can ask for a different one and so a stored result records which one produced it.
LOGISTIC_V1 = "logistic-v1"
SELF_CONSISTENCY_V1 = "self-consistency-v1"


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
) -> tuple[TierScorer, list[ScorerWarning]]:
    """Fit one tier's scorer on the calibration split.

    ``labels`` come from the checker, not from a scorer: fitting is the *only* moment a scorer
    is allowed anywhere near a grade, and even then it sees the label, never the gold answer.
    """
    if len(views) != len(labels):
        raise ScorerError(f"{len(views)} views but {len(labels)} labels")
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
    model.fit(scaled, y)
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
    #: Samples drawn per scored task at serving time. 1 is one call, the ordinary case.
    k: int = 1
    C: float = 1.0


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
