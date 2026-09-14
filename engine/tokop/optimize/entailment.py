"""Clustering answers by meaning, and counting how many draws they are really worth (U6).

``self-consistency-v1`` groups repeated generations by the workload's exact-match relation, and
says everywhere it appears that it is an approximation of semantic entropy rather than the thing
itself (DECISIONS.md D27). This is the thing itself, as far as an API allows.

Farquhar et al. compute uncertainty at the level of **meaning** rather than word sequence: sample
the model several times, cluster the answers by *bidirectional entailment* — A entails B and B
entails A — and take the entropy of the cluster proportions. The discrete variant needs no token
probabilities, which is what makes it computable behind a provider API at all: cluster
probabilities come from generation counts, and no provider has to return a log-probability.

**Bidirectional entailment is not an equivalence relation, and this module does not pretend it
is.** Exact-match equality is reflexive, symmetric and transitive, so grouping by one
representative per cluster is exact and order-independent. Entailment judged by a model is
symmetric only by construction here (both directions are asked) and is *not* transitive: A can
match B and B match C while A and C do not. Clustering therefore depends on the order the samples
arrive in, which is fixed by the recording, and the module says so rather than leaving a reader to
assume the grouping is canonical. ``group_indices`` in ``scorers.py`` makes the same first-fit
choice for a relation that genuinely is transitive, where it costs nothing.

**Every comparison is a model call, and they are charged.** Two directed calls per candidate
pair. Identical answers are short-circuited without a call, because paying a model to confirm
that "60" means what "60" means is money for nothing — and on a workload whose answers are
numbers and yes/no that short-circuit removes most of the bill.

**Effective k.** D27 wrote a paragraph explaining that repeated samples from a real model are
correlated and that the simulator's independent draws flatter any scorer that reads them. This
module turns that paragraph into a number: the intraclass correlation of within-task agreement,
and the design effect it implies. ``effective_k`` close to ``k`` means the draws really are
carrying k draws' worth of information — which on these fixtures is true, and is a fact about
the simulator rather than about sampling.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

#: ``(question, answer_a, answer_b) -> bool``: does A entail B? Asked in both directions by the
#: clustering below. Injected rather than imported, for the same reason the answer-equivalence
#: relation is: this module must not be able to reach a grader.
EntailmentFn = Callable[[str, str, str], bool]

SEMANTIC_ENTROPY_V1 = "semantic-entropy-v1"
SEP_V1 = "sep-v1"


class EntailmentError(ValueError):
    """An entailment judgement could not be made or read."""


_YES = {"yes", "true", "entails", "same", "equivalent", "1"}
_NO = {"no", "false", "different", "0"}
_LINE = re.compile(r"(?:entails|answer|verdict)\s*[:\-]\s*(\w+)", re.IGNORECASE)


def parse_entailment(reply: str) -> bool | None:
    """Read an entailment model's reply. ``None`` when it did not answer the question asked.

    ``None`` rather than ``False``: a reply nobody can read is not a judgement that the two
    answers differ, and treating it as one would silently split clusters and inflate the
    entropy — which would make every task look uncertain and route everything to the frontier.
    The caller decides what to do with a refusal; this function does not decide for it.
    """
    from tokop.workloads.grading import extract_json_answer

    payload = extract_json_answer(reply or "")
    if payload is not None and "entails" in payload:
        value = payload["entails"]
        if isinstance(value, bool):
            return value
        text = str(value).strip().casefold()
        if text in _YES:
            return True
        if text in _NO:
            return False
    match = _LINE.search(reply or "")
    if match:
        text = match.group(1).strip().casefold()
        if text in _YES:
            return True
        if text in _NO:
            return False
    return None


def render_entailment_prompt(question: str, premise: str, hypothesis: str) -> str:
    """The user message of a directed entailment call.

    One direction per call. Asking "do these mean the same thing?" in one call would be cheaper
    and would not be entailment: a model asked a symmetric question answers a symmetric question,
    and the asymmetry is where "42" and "about 42 dollars" come apart.
    """
    return (
        f"Question: {question}\n\n"
        f"Answer A: {premise}\n\n"
        f"Answer B: {hypothesis}\n\n"
        "Does Answer A entail Answer B — that is, if A is correct, must B also be correct?"
    )


@dataclass(frozen=True)
class ClusterResult:
    """Samples grouped by meaning, with what the grouping cost."""

    groups: tuple[tuple[int, ...], ...]
    #: Directed entailment calls made. Identical answers are free and are not counted.
    calls: int
    #: Pairs the entailment model would not judge, kept apart rather than merged.
    unreadable: int = 0

    @property
    def sizes(self) -> list[int]:
        return sorted((len(group) for group in self.groups), reverse=True)

    def representative(self, index: int) -> int:
        return self.groups[index][0]


def cluster_by_entailment(
    question: str, answers: Sequence[str | None], entails: EntailmentFn
) -> ClusterResult:
    """Group answers into meaning clusters by bidirectional entailment.

    First fit: each answer joins the first cluster whose representative it bidirectionally
    entails, and starts a new one otherwise. That is exact for a transitive relation and an
    approximation for this one — see the module docstring. The order is the recording's order,
    so the grouping is reproducible even though it is not canonical.

    ``None`` is an output nothing could be read out of. It forms its own cluster and is never
    compared, because a generation that produced no answer means nothing — and two of them do
    not agree with each other. That is the rule ``workloads/grading.py`` applies to an
    unparseable answer, one level up.
    """
    groups: list[list[int]] = []
    calls = 0
    unreadable = 0
    for index, answer in enumerate(answers):
        if answer is None:
            groups.append([index])
            continue
        for group in groups:
            other = answers[group[0]]
            if other is None:
                continue
            if answer == other:
                # Identical strings mean the same thing. Paying a model to confirm that is
                # money for nothing, and on a workload of numbers and yes/no it is most of
                # the bill.
                group.append(index)
                break
            try:
                forward = entails(question, other, answer)
                calls += 1
                backward = entails(question, answer, other) if forward else False
                calls += 1 if forward else 0
            except EntailmentError:
                unreadable += 1
                continue
            if forward and backward:
                group.append(index)
                break
        else:
            groups.append([index])
    return ClusterResult(
        groups=tuple(tuple(group) for group in groups), calls=calls, unreadable=unreadable
    )


def semantic_entropy(sizes: Sequence[int]) -> float:
    """Normalized entropy over meaning-cluster proportions.

    The same arithmetic ``self-consistency-v1`` applies to exact-match groups; the difference is
    entirely in what counts as a group, which is the whole point of the method.
    """
    from tokop.optimize.scorers import discrete_entropy

    return discrete_entropy(list(sizes))


# --------------------------------------------------------------------------- effective k


def intraclass_correlation(indicators: Sequence[Sequence[int]]) -> float:
    """How much more alike samples are *within* a task than across tasks.

    One-way ANOVA estimator on a binary indicator, ``rho = (MSB - MSW) / (MSB + (k-1) MSW)``,
    clipped to [0, 1]: a negative estimate means the draws are *less* alike within a task than
    across them, which is not a thing k correlated samples can be and is an artefact of the
    estimator at small k.

    The indicator is membership of the task's largest meaning cluster. Self-referential in a
    benign way — the modal cluster is defined by the samples it is measuring — and it is what
    "these k draws agreed" means when there is no reference answer to agree *with*.
    """
    rows = [list(row) for row in indicators if row]
    if len(rows) < 2:
        raise EntailmentError("an intraclass correlation needs at least two tasks")
    k = len(rows[0])
    if k < 2:
        raise EntailmentError("an intraclass correlation needs at least two samples per task")
    if any(len(row) != k for row in rows):
        raise EntailmentError("every task must carry the same number of samples")

    values = np.asarray(rows, dtype=float)
    task_means = values.mean(axis=1)
    grand = float(values.mean())
    tasks = len(rows)
    between = k * float(((task_means - grand) ** 2).sum()) / (tasks - 1)
    within = float(((values - task_means[:, None]) ** 2).sum()) / (tasks * (k - 1))
    if between + (k - 1) * within == 0:
        # Every sample everywhere agreed: there is no variance to partition, and the honest
        # reading is total within-task agreement rather than an undefined ratio.
        return 1.0
    rho = (between - within) / (between + (k - 1) * within)
    return float(min(1.0, max(0.0, rho)))


def effective_k(k: int, rho: float) -> float:
    """How many independent draws k correlated ones are worth.

    ``k / (1 + (k - 1) rho)`` — the cluster-sampling design effect. At rho = 0 the draws are
    independent and k samples are worth k; at rho = 1 they are the same draw repeated and worth
    one, whatever k says on the invoice.
    """
    if k < 1:
        raise EntailmentError("k must be at least 1")
    if not 0 <= rho <= 1:
        raise EntailmentError("an intraclass correlation must be in [0, 1]")
    return float(k / (1 + (k - 1) * rho))


@dataclass(frozen=True)
class SampleCorrelation:
    """What k repeated samples were actually worth, measured."""

    k: int
    rho: float
    effective_k: float
    tasks: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "intraclass_correlation": self.rho,
            "effective_k": self.effective_k,
            "tasks": self.tasks,
            "method": (
                "one-way ANOVA intraclass correlation of membership of each task's largest "
                "meaning cluster, and the cluster-sampling design effect k / (1 + (k-1) rho)"
            ),
        }


def measure_correlation(
    cluster_sizes_by_task: Sequence[Sequence[int]], k: int
) -> SampleCorrelation:
    """Effective k across a split, from each task's meaning-cluster sizes."""
    indicators: list[list[int]] = []
    for sizes in cluster_sizes_by_task:
        if sum(sizes) != k:
            raise EntailmentError(
                f"a task's clusters hold {sum(sizes)} samples but k is {k}; the measurement "
                "would be comparing tasks sampled to different depths."
            )
        largest = max(sizes)
        indicators.append([1] * largest + [0] * (k - largest))
    rho = intraclass_correlation(indicators)
    return SampleCorrelation(k=k, rho=rho, effective_k=effective_k(k, rho), tasks=len(indicators))


def entailment_reply(entails: bool, why: str = "") -> str:
    """The reply shape the parser reads. Shared by the simulator and the prompt's instructions
    so the two cannot drift apart."""
    return json.dumps({"entails": bool(entails), "why": why[:120]})
