"""How much of a result is measured, and how much is estimated (UPGRADE_V4.md M14.2).

A visitor's first question about any claim on the Optimize screen is some version of "why should
I believe the grader?", and the honest answer is a chain: this verdict rests on that many tasks,
graded that way, drawn from that population, priced from that source. Every link in that chain is
already computed somewhere in the report. What was missing is the chain itself — one word at the
top of the screen, and the whole derivation one click away.

The grade is the **lowest rung any input forces**, never an average and never the best available
reading:

``insufficient``
    Something blocks a conclusion even about the traces themselves. An inconclusive verdict is
    the usual cause: a result that cannot separate two pipelines is not evidence that either is
    better, however many tasks went into it.
``simulated``
    Everything computed is true about the traces, and the traces are not a recording. Real engine
    output over invented answers — which is what this repository has shipped from the start.
``recording``
    The chain terminates in provider responses to tasks somebody actually observed.

Nothing here softens anything. The same refusals the report already makes decide the grade; what
changes is that a reader meets them as one word instead of five paragraphs (UPGRADE_V4.md
section 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Grade = Literal["recording", "simulated", "insufficient"]
Standing = Literal["ok", "simulated", "blocking"]

#: Worst first. A grade is the lowest standing any input forces.
_ORDER: dict[Standing, int] = {"blocking": 0, "simulated": 1, "ok": 2}
_GRADE_FOR: dict[Standing, Grade] = {
    "blocking": "insufficient",
    "simulated": "simulated",
    "ok": "recording",
}


@dataclass(frozen=True)
class EvidenceInput:
    """One link in the chain, with the number that settles it."""

    name: str
    value: str
    standing: Standing
    #: Where this number came from, so the reader can go and check it rather than trust it.
    source: str
    #: What would move this link, in one sentence. Empty when the link is already as good as it
    #: gets — a reader should not have to guess whether "ok" means "done" or "not measured".
    what_would_change_it: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "standing": self.standing,
            "source": self.source,
            "what_would_change_it": self.what_would_change_it,
        }


@dataclass(frozen=True)
class Evidence:
    """A grade, and every input that produced it."""

    grade: Grade
    headline: str
    inputs: tuple[EvidenceInput, ...]

    @property
    def blocking(self) -> tuple[EvidenceInput, ...]:
        return tuple(i for i in self.inputs if i.standing == "blocking")

    def as_dict(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "headline": self.headline,
            "inputs": [i.as_dict() for i in self.inputs],
            "blocking": [i.name for i in self.blocking],
        }


def _share(part: int, whole: int) -> str:
    return f"{part:,} of {whole:,}" + (f" ({part / whole:.0%})" if whole else "")


def _traffic(payload: Any) -> EvidenceInput:
    provenance = payload["dataset_provenance"]
    real = int(provenance["real_traffic_items"])
    n = int(provenance["n"])
    return EvidenceInput(
        name="Tasks from real traffic",
        value=_share(real, n),
        standing="ok" if real == n and n else "simulated",
        source="the workload's declared dataset provenance, checked against the set",
        what_would_change_it=(
            ""
            if real == n and n
            else "Ingesting tasks somebody actually observed. Until then nothing measured here "
            "is evidence about a distribution anyone has seen."
        ),
    )


def _answers(payload: Any) -> EvidenceInput:
    provenance = payload["provenance"]
    simulated = bool(provenance["is_test_data"])
    return EvidenceInput(
        name="Answers from a provider",
        value="simulated" if simulated else f"recorded, {provenance['origin']}",
        standing="simulated" if simulated else "ok",
        source=f"fixtures/{provenance['fixture_source']}/manifest.json",
        what_would_change_it=(
            "A live recording: `make record`, which needs a key and RECORD_BUDGET_USD."
            if simulated
            else ""
        ),
    )


def _split(payload: Any) -> EvidenceInput:
    power = payload["proof"]["power"]
    required = power["required_n"]
    enough = bool(power["sufficient"])
    if required is None:
        value = f"{power['observed_n']:,} tasks; no split size settles it"
    else:
        value = f"{power['observed_n']:,} tasks, {required:,} needed"
    return EvidenceInput(
        name="Test split",
        value=value,
        standing="ok" if enough else "blocking",
        source="McNemar power at the observed discordance, `core/stats.py`",
        what_would_change_it=(
            ""
            if enough
            else "More tasks, or a larger true difference. `tokop power` sizes it before a "
            "recording rather than after one."
        ),
    )


def _discordance(payload: Any) -> EvidenceInput:
    mcnemar = payload["proof"]["mcnemar"]
    n = int(payload["proof"]["candidate"]["n"])
    discordant = int(mcnemar["discordant"])
    return EvidenceInput(
        name="Tasks the two pipelines disagree on",
        value=f"{_share(discordant, n)}, exact p = {mcnemar['p_value']:.3f}",
        # Never blocking on its own: a low discordance means the pipelines agree, which is
        # information. What it costs is resolution, and the split input above says so.
        standing="ok",
        source="McNemar's exact test over the paired per-task outcomes",
        what_would_change_it=(
            "Nothing needs to. Only the discordant pairs carry information about a difference, "
            "which is why a small number of them means a wider interval, not a wrong one."
        ),
    )


def _verdict(payload: Any) -> EvidenceInput:
    verdict = payload["proof"]["verdict"]
    label = str(verdict["label"])
    return EvidenceInput(
        name="Verdict",
        value=str(verdict["display"]),
        standing="ok" if label == "non_inferior" else "blocking",
        source=f"a paired bootstrap against the {verdict['margin'] * 100:.0f}-point margin",
        what_would_change_it=(
            ""
            if label == "non_inferior"
            else "A conclusive result. An inconclusive one is not weak evidence that the "
            "candidate is as good; it is no evidence either way."
        ),
    )


def _grader(payload: Any) -> EvidenceInput:
    """How far the cheap judge can be trusted, measured against the strong grader."""
    contract = payload["contract"]
    if not contract.get("available"):
        return EvidenceInput(
            name="Grader agreement",
            value="not measured",
            standing="simulated",
            source="no judged arm on this workload",
            what_would_change_it=(
                "Running `tokop annotate`, which grades a sample with the strong grader and "
                "measures how often the cheap judge agrees with it."
            ),
        )
    arms = contract["arms"]
    worst = min(arms, key=lambda arm: arm["judge_agreement_with_strong_grader"])
    agreement = float(worst["judge_agreement_with_strong_grader"])
    return EvidenceInput(
        name="Cheap judge agrees with the strong grader",
        value=f"{agreement:.1%} on {worst['pipeline']}, its worst arm",
        standing="ok",
        source=f"{contract['annotated']} tasks graded by {contract['strong_grader']['model_id']}",
        what_would_change_it=(
            "This is the link the whole judged path rests on, and it is measured rather than "
            "assumed. The bias it implies is corrected for, not ignored."
        ),
    )


def _prices(payload: Any) -> EvidenceInput:
    provenance = payload["provenance"]
    verified = bool(provenance["prices_verified"])
    unverified = list(provenance["unverified_models"])
    return EvidenceInput(
        name="Prices",
        value=(
            f"snapshot {provenance['price_snapshot_id']}, all verified"
            if verified
            else f"unverified: {', '.join(unverified)}"
        ),
        standing="ok" if verified else "blocking",
        source="each price carries a source URL and a retrieval date",
        what_would_change_it=(
            ""
            if verified
            else "Verifying those prices against the provider's own page. Cost per successful "
            "task is the headline, so an unverified price makes the headline unverified."
        ),
    )


def _counter(payload: Any) -> EvidenceInput:
    provenance = payload["provenance"]
    counter = str(provenance["base_token_counter"])
    exact = counter != "bytes-bpe-approx-v1"
    return EvidenceInput(
        name="Token counts",
        value=counter,
        # Not blocking: every figure the approximation touched is already labelled, and the
        # costs themselves come from provider-reported usage rather than from a counter.
        standing="ok" if exact else "simulated",
        source="the counter the fixtures were built with, recorded in their manifest",
        what_would_change_it=(
            ""
            if exact
            else "Reaching the o200k_base vocabulary host, which this build's egress policy "
            "blocks. Provider-reported usage is unaffected; estimates made from the counter "
            "say which counter made them."
        ),
    )


def assess(payload: Any) -> Evidence:
    """Grade a report, and show the working.

    Takes the payload rather than the pieces so that every input is read from the same
    computation the screen is displaying. An evidence panel assembled from a second source is a
    panel that can disagree with the number beside it.
    """
    inputs = tuple(
        build(payload)
        for build in (
            _verdict,
            _split,
            _discordance,
            _traffic,
            _answers,
            _grader,
            _prices,
            _counter,
        )
    )
    worst = min(inputs, key=lambda i: _ORDER[i.standing])
    grade = _GRADE_FOR[worst.standing]
    if grade == "recording":
        headline = "Every link in this chain terminates in a recorded provider response."
    elif grade == "simulated":
        headline = (
            f"Real engine output over invented answers. {worst.name}: {worst.value}. "
            "Everything computed here is true about these traces and none of it is yet "
            "evidence about production."
        )
    else:
        blocking = [i for i in inputs if i.standing == "blocking"]
        headline = (
            f"{blocking[0].name}: {blocking[0].value}. "
            + (
                f"{len(blocking) - 1} other link{'s' if len(blocking) > 2 else ''} also "
                "blocks a conclusion. "
                if len(blocking) > 1
                else ""
            )
            + "This result does not support the claim it is attached to."
        )
    return Evidence(grade=grade, headline=headline, inputs=inputs)
