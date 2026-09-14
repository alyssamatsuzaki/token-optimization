"""Where the task set came from, and whether it can certify anything (UPGRADE_V3.md U8).

A certificate is a claim about production behaviour, and it is only as good as the set it was
measured on. Two questions decide that, and neither is answerable from the numbers a proof
produces:

**Who wrote these tasks?** Shumailov et al. show that indiscriminate training on model-generated
content causes irreversible defects in which the *tails of the distribution disappear*, and
Alemohammad et al. show the same loop degrades quality or diversity without enough fresh real
data each generation. The danger here is narrower than the training-time one and worth stating
plainly: **a cascade earns its savings on easy tasks and its risk lives in the tail.** An eval
set whose tail has thinned will certify a router that fails in production, and every number on
the certificate will look fine while it does.

**Is the tail still there?** Measured rather than assumed. The generator's template space is
known, so the share of it that appears at least once, the share of items sitting in
rarely-seen templates, and the concentration of items across templates are all computable from
the set itself. A set that has collapsed onto its common cases says so in those three numbers
before it says so in production.

**What this module refuses to fake.** Hu et al. find that relabeling generated items with a
frozen reference model mitigates collapse, and Drayson et al. train a machine-generated-text
detector and importance-resample towards likely human content. Both need a model. This build has
no credentials (DECISIONS.md D1), so the detector registry is **empty** and asking for one
raises: a detector that guessed would importance-resample towards its own guess, which is the
self-consuming loop wearing a lab coat. The resampling mathematics is here and tested, because a
caller with a real detector can use it; the detector is not.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

#: A set with no real recorded traffic at all cannot certify production behaviour, whatever its
#: other numbers say. It can still be measured, and the measurement is still useful — it just is
#: not evidence about a distribution nobody has observed.
MIN_REAL_TRAFFIC_ITEMS = 1

#: Above this share of *model-generated* items, with no real traffic accumulating, the set is in
#: the regime the collapse papers describe and certification is refused.
MAX_MODEL_GENERATED_SHARE = 0.5

#: Share of the generator's template space that must appear at least once. Below this the set
#: has thinned onto its common cases, which is exactly where a cascade's risk is not.
MIN_TAIL_COVERAGE = 0.60

#: A template seen this many times or fewer is in the tail for the purpose of the tail-share
#: figure. Not a threshold anything is refused on; a description of the shape.
TAIL_OCCURRENCES = 2

#: Below this many distinct generators, model-generated content is a warning rather than a
#: refusal: Hu et al. find generator diversity mitigates collapse, and one generator has none.
MIN_GENERATORS = 2


class ProvenanceError(ValueError):
    """A dataset's provenance could not be established."""


@dataclass(frozen=True)
class DeclaredProvenance:
    """What the workload *claims* about where its items came from.

    A claim, not a measurement: Tokop cannot look at a question and tell whether a person typed
    it, a program templated it or a model wrote it. What it can do is take the claim, compute
    everything the items themselves reveal, and refuse to certify when the two together are not
    good enough.
    """

    real_traffic_items: int = 0
    model_generated_items: int = 0
    program_generated_items: int = 0
    generators: tuple[str, ...] = ()
    #: Sampling budget the generators ran at, when a model wrote any of it. Hu et al. find a
    #: larger decoding budget mitigates collapse; ``None`` when no model was involved.
    decoding_budget: str | None = None
    #: Whether generated items were relabeled by a model held fixed across generations.
    relabelled_by_frozen_reference: bool = False
    #: Whether real traffic is being collected and folded in over time.
    real_traffic_accumulating: bool = False
    notes: tuple[str, ...] = ()

    @property
    def declared_total(self) -> int:
        return self.real_traffic_items + self.model_generated_items + self.program_generated_items


@dataclass(frozen=True)
class TailShape:
    """What the items themselves say about coverage, computed from their template ids."""

    template_space: int
    templates_present: int
    tail_coverage: float
    #: Share of items sitting in templates seen ``TAIL_OCCURRENCES`` times or fewer.
    tail_share: float
    #: Gini coefficient over per-template counts. 0 is perfectly even, 1 is everything in one.
    concentration: float
    missing_templates: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_space": self.template_space,
            "templates_present": self.templates_present,
            "tail_coverage": self.tail_coverage,
            "tail_share": self.tail_share,
            "concentration": self.concentration,
            "missing_templates": list(self.missing_templates[:12]),
            "missing_template_count": len(self.missing_templates),
        }


def _gini(counts: Sequence[int]) -> float:
    """Concentration of items across templates, 0 (even) to 1 (all in one).

    Reported rather than refused on: a set can be uneven for good reasons — real traffic is
    uneven — and what matters for a certificate is whether the rare cases are *present*, which
    ``tail_coverage`` answers directly.
    """
    values = sorted(int(c) for c in counts if c > 0)
    if len(values) <= 1:
        return 0.0
    total = sum(values)
    if total == 0:
        return 0.0
    weighted = sum((index + 1) * value for index, value in enumerate(values))
    n = len(values)
    return float((2 * weighted) / (n * total) - (n + 1) / n)


def tail_shape(template_ids: Sequence[str], template_space: Sequence[str]) -> TailShape:
    """Coverage and concentration of a set over the space it was drawn from.

    ``template_space`` is every template the generator can produce. Without it there is nothing
    to be missing *from*, and a collapsed set looks identical to a small one — which is the
    whole difficulty with detecting collapse after the fact.
    """
    if not template_space:
        raise ProvenanceError(
            "the generator's template space is empty, so coverage cannot be measured. A set "
            "with nothing to be missing from cannot be checked for a thinned tail."
        )
    counts = Counter(template_ids)
    present = {t for t in counts if counts[t] > 0}
    space = list(dict.fromkeys(template_space))
    missing = tuple(sorted(t for t in space if t not in present))
    total = sum(counts.values())
    tail_items = sum(count for count in counts.values() if count <= TAIL_OCCURRENCES)
    return TailShape(
        template_space=len(space),
        templates_present=len(present & set(space)),
        tail_coverage=len(present & set(space)) / len(space),
        tail_share=tail_items / total if total else 0.0,
        concentration=_gini(list(counts.values())),
        missing_templates=missing,
    )


@dataclass(frozen=True)
class Certifiability:
    """Whether a set may back a certificate, and every reason it may not."""

    certifiable: bool
    refusals: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "certifiable": self.certifiable,
            "refusals": list(self.refusals),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DatasetProvenance:
    """The declared origins, the measured shape, and the verdict."""

    n: int
    declared: DeclaredProvenance
    shape: TailShape
    assessment: Certifiability

    @property
    def model_generated_share(self) -> float:
        return self.declared.model_generated_items / self.n if self.n else 0.0

    @property
    def synthetic_share(self) -> float:
        generated = self.declared.model_generated_items + self.declared.program_generated_items
        return generated / self.n if self.n else 0.0

    @property
    def real_traffic_share(self) -> float:
        return self.declared.real_traffic_items / self.n if self.n else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "real_traffic_items": self.declared.real_traffic_items,
            "model_generated_items": self.declared.model_generated_items,
            "program_generated_items": self.declared.program_generated_items,
            "real_traffic_share": self.real_traffic_share,
            "model_generated_share": self.model_generated_share,
            "synthetic_share": self.synthetic_share,
            "generators": list(self.declared.generators),
            "decoding_budget": self.declared.decoding_budget,
            "relabelled_by_frozen_reference": self.declared.relabelled_by_frozen_reference,
            "real_traffic_accumulating": self.declared.real_traffic_accumulating,
            "notes": list(self.declared.notes),
            "shape": self.shape.as_dict(),
            **self.assessment.as_dict(),
        }


def assess(n: int, declared: DeclaredProvenance, shape: TailShape) -> Certifiability:
    """Whether this set may back a certificate.

    Every refusal names the rule it failed and the number that failed it, because a refusal a
    user cannot act on is a refusal they will route around.
    """
    refusals: list[str] = []
    warnings: list[str] = []

    if declared.declared_total != n:
        refusals.append(
            f"the declared origins account for {declared.declared_total} items and the set holds "
            f"{n}. A provenance block that does not add up describes a different dataset."
        )

    model_share = declared.model_generated_items / n if n else 0.0
    if model_share > MAX_MODEL_GENERATED_SHARE and not declared.real_traffic_accumulating:
        refusals.append(
            f"{model_share:.0%} of the items were written by a model and no real traffic is "
            "accumulating. That is the self-consuming loop the collapse literature describes, "
            "and the failure mode for a cascade is specific: savings are earned on easy tasks "
            "and risk lives in the tail, so a thinned tail certifies a router that fails in "
            "production."
        )

    if declared.real_traffic_items < MIN_REAL_TRAFFIC_ITEMS:
        refusals.append(
            "the set contains no real recorded traffic, so it cannot certify behaviour on a "
            "distribution nobody has observed. Everything measured on it is still true about "
            "the set; none of it is yet evidence about production."
        )

    if shape.tail_coverage < MIN_TAIL_COVERAGE:
        refusals.append(
            f"only {shape.tail_coverage:.0%} of the generator's {shape.template_space} question "
            f"templates appear in the set, below the {MIN_TAIL_COVERAGE:.0%} floor. The tail has "
            f"thinned: {len(shape.missing_templates)} templates are missing entirely."
        )

    if declared.model_generated_items > 0:
        if len(declared.generators) < MIN_GENERATORS:
            warnings.append(
                f"{len(declared.generators)} generator wrote the model-generated items. "
                "Generator diversity is one of the few things shown to mitigate collapse, and "
                "one generator has none of it."
            )
        if not declared.relabelled_by_frozen_reference:
            warnings.append(
                "the model-generated items were not relabelled by a frozen reference model, "
                "which is the other mitigation with evidence behind it."
            )
        if declared.decoding_budget is None:
            warnings.append(
                "no decoding budget is recorded for the generated items, so nothing can be said "
                "about whether they were sampled widely enough to keep the tail."
            )

    if shape.tail_share < 0.01 and shape.template_space > 1:
        warnings.append(
            f"only {shape.tail_share:.1%} of items sit in rarely-seen templates. An eval set "
            "flatter than the traffic it stands for will under-report tail risk."
        )

    return Certifiability(
        certifiable=not refusals, refusals=tuple(refusals), warnings=tuple(warnings)
    )


def build_provenance(
    template_ids: Sequence[str],
    template_space: Sequence[str],
    declared: DeclaredProvenance,
) -> DatasetProvenance:
    shape = tail_shape(template_ids, template_space)
    n = len(template_ids)
    return DatasetProvenance(
        n=n, declared=declared, shape=shape, assessment=assess(n, declared, shape)
    )


# ------------------------------------------------- resampling towards human-written items


class MachineTextDetector(Protocol):
    """Scores how likely a piece of text is to have been written by a model."""

    name: str

    def probability_machine(self, text: str) -> float: ...


#: Empty on purpose. A detector needs a model, this build has none (DECISIONS.md D1), and a
#: detector that guessed would importance-resample a set towards its own guess.
DETECTOR_REGISTRY: dict[str, MachineTextDetector] = {}


def detector(name: str) -> MachineTextDetector:
    try:
        return DETECTOR_REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(DETECTOR_REGISTRY)) or "none"
        raise ProvenanceError(
            f"no machine-generated-text detector named {name!r} is registered (available: "
            f"{available}). Detecting machine-written text needs a trained model, and this "
            "build has none. Resampling a set towards 'probably human' using a detector that "
            "was guessing would bias the set towards whatever the guess correlated with, which "
            "is worse than not resampling at all. `importance_weights` is here and tested for a "
            "caller who has a real detector."
        ) from None


#: The floor on an item's resampling weight. Same reasoning as the annotation policy's: an item
#: that can never be drawn is an item the resampled set cannot represent, and a detector is not
#: reliable enough to exclude anything outright.
MIN_RESAMPLE_WEIGHT = 0.05


def importance_weights(
    machine_probabilities: Sequence[float], *, floor: float = MIN_RESAMPLE_WEIGHT
) -> list[float]:
    """Weights that pull a set towards likely human-written items.

    Drayson et al. resample by the importance ratio between the human distribution and the
    mixture actually observed, which for a calibrated detector is proportional to
    ``P(human | text) = 1 - P(machine | text)``. Normalized to sum to 1, floored so no item is
    unsamplable, and refusing anything outside [0, 1] rather than clamping — a detector that
    returned 1.4 did not mean "certainly machine", it malfunctioned.
    """
    values = list(machine_probabilities)
    if not values:
        raise ProvenanceError("there is nothing to weight")
    if not 0 < floor <= 1:
        raise ProvenanceError("the weight floor must be in (0, 1]")
    for probability in values:
        if not 0.0 <= probability <= 1.0 or math.isnan(probability):
            raise ProvenanceError(
                f"a detector returned {probability}, which is not a probability. Clamping it "
                "would turn a malfunction into a confident answer."
            )
    raw = [max(floor, 1.0 - probability) for probability in values]
    total = sum(raw)
    return [value / total for value in raw]
