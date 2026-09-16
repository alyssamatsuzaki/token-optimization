"""Certificates expire, and something has to keep checking (UPGRADE_V3.md U5).

A proof is a snapshot. Nothing in it notices when the provider ships a new revision of a model
under the same name, and nothing in it notices when the workload drifts away from the split it
was measured on. Davidson et al. find that factual updates can leave a model verifying both the
old and the new answer, with residual verification bias after updates on frontier models; Fang
et al.'s AlphaEdit exists because parameter edits disturb knowledge that was supposed to be
preserved, and the disturbance compounds under sequential edits. A vendor's model is edited
continuously and you are not told. Treating a certificate as permanent is the single easiest way
for this product to be wrong in public.

So a certificate carries what it rests on — model snapshot identifiers, the price hash, the
grading mode, the calibration mode, the dataset's provenance — and an expiry. Between issue and
expiry, ``tokop canary`` takes *looks*: it re-scores a stratified subset and asks whether the
accuracy difference has moved.

**Why looking repeatedly needs care.** Check a stable quantity often enough at the 5% level and
you will find a 5% result. The looks are spread across the certificate's life, so alpha is spent
across them: with a planned number of looks K and independent samples at each, per-look levels
chosen from a spending function give a family-wise error rate of exactly the stated alpha. The
expiry is what makes K finite, which is what makes the spending well-defined — the two halves of
U5 need each other.

**What a canary cannot do in replay.** Re-scoring the same cassettes cannot produce a different
answer, so a look taken against the fixture set the certificate was issued from has no drift to
find. That is reported as *not applicable* with the reason rather than as a reassuring pass. A
look against a different recording — a later one, or one with a swapped snapshot identifier — is
a real look, and the identity checks fire whether or not there is anything to re-score.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from scipy import stats as scipy_stats

from tokop.optimize.fingerprint import Divergence, Fingerprint, divergence

CERTIFICATE_SCHEMA = "tokop.certificate.v1"

#: How long a certificate stands before it has to be re-earned. A month is short enough that a
#: silently-updated model is caught within one billing cycle and long enough that re-proving is
#: not the main cost of using the product. Named, not tuned.
CERTIFICATE_TTL_DAYS = 30

#: How often a canary looks, by default. With the TTL above that is four looks in a life.
CANARY_INTERVAL_DAYS = 7

#: Share of the test split one look re-scores, stratified by question type. A quarter is enough
#: to see a move worth acting on and cheap enough to run weekly.
CANARY_FRACTION = 0.25

DEFAULT_ALPHA = 0.05

POCOCK = "pocock"
OBRIEN_FLEMING = "obrien-fleming"


class CertificateError(ValueError):
    """A certificate could not be issued, read, or checked."""


# --------------------------------------------------------------------------- alpha spending


def cumulative_alpha(alpha: float, fraction: float, spending: str = POCOCK) -> float:
    """How much of the error budget has been spent by the time ``fraction`` of the looks are in.

    ``pocock`` spends it evenly, which suits a canary: every look matters as much as the last,
    because a model can be swapped in any week. ``obrien-fleming`` spends almost nothing early
    and the rest at the end, which suits a trial that wants to reach its planned size — offered
    because it is the other standard answer, not because it is the right default here.
    """
    if not 0 < alpha < 1:
        raise CertificateError("alpha must be in (0, 1)")
    if not 0 <= fraction <= 1:
        raise CertificateError("the information fraction must be in [0, 1]")
    if fraction == 0:
        return 0.0
    if spending == POCOCK:
        return alpha * fraction
    if spending == OBRIEN_FLEMING:
        z = float(scipy_stats.norm.ppf(1 - alpha / 2))
        return float(2 * (1 - scipy_stats.norm.cdf(z / math.sqrt(fraction))))
    raise CertificateError(
        f"unknown alpha-spending function {spending!r}; this build offers {POCOCK} and "
        f"{OBRIEN_FLEMING}"
    )


def alpha_schedule(alpha: float, looks: int, spending: str = POCOCK) -> list[float]:
    """Per-look significance levels whose family-wise error rate is exactly ``alpha``.

    The looks are **independent samples** — a canary draws a fresh stratified subset each time
    and re-scores it — so the probability of never raising under the null is the product of
    ``(1 - alpha_k)``. Setting that product to ``1 - S_k`` at every look gives

        alpha_k = 1 - (1 - S_k) / (1 - S_{k-1})

    and the whole family spends exactly ``S_K = alpha``. This is *not* a Pocock boundary in the
    group-sequential sense: those correct for looks at accumulating data, where the looks are
    correlated. Here they are not, and using the correlated correction would be conservative for
    a reason that does not apply.
    """
    if looks < 1:
        raise CertificateError("a certificate must plan at least one look")
    levels: list[float] = []
    previous = 0.0
    for index in range(1, looks + 1):
        spent = cumulative_alpha(alpha, index / looks, spending)
        level = 1.0 - (1.0 - spent) / (1.0 - previous)
        levels.append(max(0.0, min(1.0, level)))
        previous = spent
    return levels


# --------------------------------------------------------------------------- the certificate


@dataclass(frozen=True)
class ModelSnapshot:
    """One model the certificate rests on, and the revision it was measured against."""

    role: str
    model_id: str
    #: Whatever the recording captured to identify the revision. For a provider that pins a
    #: date in the model id this is that id; for one that does not, it is the id plus the
    #: fixture manifest's own stamp, which is the best available and says so.
    snapshot: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "model_id": self.model_id, "snapshot": self.snapshot}


@dataclass
class Certificate:
    """What was proven, what it rests on, and when it stops being true."""

    workload: str
    baseline_pipeline: str
    candidate_pipeline: str
    verdict: str
    margin: float
    delta_point: float
    delta_low: float
    delta_high: float
    n: int
    split_sizes: dict[str, int]
    grading_mode: str
    calibration_mode: str
    model_snapshots: tuple[ModelSnapshot, ...]
    price_snapshot_id: str
    origin: str
    dataset_provenance: dict[str, Any]
    issued: date
    expires: date
    looks_planned: int
    alpha: float
    spending: str
    schema: str = CERTIFICATE_SCHEMA
    #: What the split this was measured on *looked like*: the mix of task types, the input
    #: length quantiles and a difficulty proxy (UPGRADE_V4.md M18). A certificate binds to it as
    #: well as to the dataset, so that traffic drifting into a region the split barely covered
    #: expires it — on coverage, not only on time.
    workload_fingerprint: Fingerprint | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def standard_error(self) -> float:
        """Recovered from the interval it already carries, rather than stored twice."""
        z = float(scipy_stats.norm.ppf(0.975))
        return (self.delta_high - self.delta_low) / (2 * z)

    @property
    def certifiable(self) -> bool:
        return bool(self.dataset_provenance.get("certifiable"))

    def levels(self) -> list[float]:
        return alpha_schedule(self.alpha, self.looks_planned, self.spending)

    def identity(self) -> dict[str, Any]:
        """Everything a look has to find unchanged before its numbers mean anything."""
        return {
            "model_snapshots": [s.as_dict() for s in self.model_snapshots],
            "price_snapshot_id": self.price_snapshot_id,
            "grading_mode": self.grading_mode,
            "calibration_mode": self.calibration_mode,
        }

    def fingerprint(self) -> str:
        """A hash over what the certificate rests on, so two can be compared in one line."""
        payload = json.dumps(self.identity(), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def expired(self, today: date) -> bool:
        return today > self.expires

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "workload": self.workload,
            "baseline_pipeline": self.baseline_pipeline,
            "candidate_pipeline": self.candidate_pipeline,
            "verdict": self.verdict,
            "margin": self.margin,
            "delta_accuracy": {
                "point": self.delta_point,
                "low": self.delta_low,
                "high": self.delta_high,
                "standard_error": self.standard_error,
            },
            "n": self.n,
            "split_sizes": dict(self.split_sizes),
            "grading_mode": self.grading_mode,
            "calibration_mode": self.calibration_mode,
            "model_snapshots": [s.as_dict() for s in self.model_snapshots],
            "price_snapshot_id": self.price_snapshot_id,
            "origin": self.origin,
            "dataset_provenance": self.dataset_provenance,
            "issued": self.issued.isoformat(),
            "expires": self.expires.isoformat(),
            "looks_planned": self.looks_planned,
            "alpha": self.alpha,
            "spending": self.spending,
            "per_look_alpha": self.levels(),
            "fingerprint": self.fingerprint(),
            "workload_fingerprint": (
                self.workload_fingerprint.as_dict()
                if self.workload_fingerprint is not None
                else None
            ),
            "certifiable": self.certifiable,
            "notes": list(self.notes),
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True))
        return path

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Certificate:
        if str(raw.get("schema")) != CERTIFICATE_SCHEMA:
            raise CertificateError(
                f"certificate schema is {raw.get('schema')!r}, not {CERTIFICATE_SCHEMA!r}."
            )
        delta = raw["delta_accuracy"]
        return cls(
            workload=str(raw["workload"]),
            baseline_pipeline=str(raw["baseline_pipeline"]),
            candidate_pipeline=str(raw["candidate_pipeline"]),
            verdict=str(raw["verdict"]),
            margin=float(raw["margin"]),
            delta_point=float(delta["point"]),
            delta_low=float(delta["low"]),
            delta_high=float(delta["high"]),
            n=int(raw["n"]),
            split_sizes=dict(raw["split_sizes"]),
            grading_mode=str(raw["grading_mode"]),
            calibration_mode=str(raw["calibration_mode"]),
            model_snapshots=tuple(
                ModelSnapshot(str(s["role"]), str(s["model_id"]), str(s["snapshot"]))
                for s in raw["model_snapshots"]
            ),
            price_snapshot_id=str(raw["price_snapshot_id"]),
            origin=str(raw.get("origin", "simulated")),
            dataset_provenance=dict(raw.get("dataset_provenance") or {}),
            issued=date.fromisoformat(str(raw["issued"])),
            expires=date.fromisoformat(str(raw["expires"])),
            looks_planned=int(raw["looks_planned"]),
            alpha=float(raw["alpha"]),
            spending=str(raw["spending"]),
            workload_fingerprint=(
                Fingerprint.from_dict(raw["workload_fingerprint"])
                if raw.get("workload_fingerprint")
                else None
            ),
            notes=tuple(str(n) for n in raw.get("notes") or ()),
        )

    @classmethod
    def read(cls, path: Path) -> Certificate:
        if not path.exists():
            raise CertificateError(
                f"no certificate at {path}. Run `tokop certificate --out {path.name}`."
            )
        return cls.from_dict(json.loads(path.read_text()))


def issue(
    payload: Any,
    *,
    today: date,
    ttl_days: int = CERTIFICATE_TTL_DAYS,
    interval_days: int = CANARY_INTERVAL_DAYS,
    alpha: float = DEFAULT_ALPHA,
    spending: str = POCOCK,
    workload_fingerprint: Fingerprint | None = None,
) -> Certificate:
    """Turn a report into a certificate with an expiry and a look schedule.

    ``workload_fingerprint`` is what the split looked like. It is optional only so that a
    certificate written before M18 still reads; one issued without it cannot be checked for
    distribution drift, and :func:`take_look` says so rather than passing the check.
    """
    proof = payload["proof"]
    provenance = payload["provenance"]
    if interval_days < 1:
        raise CertificateError("a canary interval must be at least a day")
    if ttl_days < 1:
        raise CertificateError("a certificate must stand for at least a day")
    looks = max(1, ttl_days // interval_days)
    snapshots = tuple(
        ModelSnapshot(
            role=role,
            model_id=model_id,
            # The recording's own stamp is folded in because most model ids do not pin a
            # revision. It is the best available identifier and the certificate says so rather
            # than implying the provider supplied one.
            snapshot=f"{model_id}@{provenance.get('recorded_at') or 'unrecorded'}",
        )
        for role, model_id in sorted(provenance["model_ids"].items())
    )
    return Certificate(
        workload=payload["workload"]["id"],
        baseline_pipeline=proof["baseline"]["pipeline"],
        candidate_pipeline=proof["candidate"]["pipeline"],
        verdict=proof["verdict"]["label"],
        margin=float(proof["verdict"]["margin"]),
        delta_point=float(proof["delta_accuracy"]["point"]),
        delta_low=float(proof["delta_accuracy"]["low"]),
        delta_high=float(proof["delta_accuracy"]["high"]),
        n=int(proof["candidate"]["n"]),
        split_sizes=dict(proof["split_sizes"]),
        grading_mode=str(payload["workload"].get("grading", "gold")),
        calibration_mode=str(payload["calibration"]["mode"]),
        model_snapshots=snapshots,
        price_snapshot_id=str(provenance["price_snapshot_id"]),
        origin=str(provenance["origin"]),
        dataset_provenance=dict(payload["dataset_provenance"]),
        issued=today,
        expires=today + timedelta(days=ttl_days),
        looks_planned=looks,
        alpha=alpha,
        spending=spending,
        workload_fingerprint=workload_fingerprint,
        notes=(
            "A certificate is a claim about the models and prices it names. It expires because "
            "a provider's model is edited continuously and you are not told.",
            "It is also a claim about the tasks it was measured on. It carries their "
            "distribution, and a canary expires it on coverage as well as on time: traffic that "
            "has moved into a region the split barely covered is traffic the proof never saw."
            if workload_fingerprint is not None
            else "No workload fingerprint was supplied, so this certificate cannot be checked "
            "for distribution drift. A look will report that rather than pass it.",
        ),
    )


# --------------------------------------------------------------------------- the canary


@dataclass
class CanaryLog:
    """Which looks have been taken. Alpha already spent cannot be spent again."""

    certificate_fingerprint: str
    looks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def taken(self) -> int:
        return len(self.looks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "tokop.canary-log.v1",
            "certificate_fingerprint": self.certificate_fingerprint,
            "looks": self.looks,
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True))
        return path

    @classmethod
    def read(cls, path: Path, fingerprint: str) -> CanaryLog:
        if not path.exists():
            return cls(certificate_fingerprint=fingerprint)
        raw = json.loads(path.read_text())
        stored = str(raw.get("certificate_fingerprint", ""))
        if stored != fingerprint:
            # A log from a different certificate would let a fresh certificate inherit alpha
            # somebody else already spent, or start over after a change that should have reset
            # nothing. Neither is right, so the mismatch is named.
            raise CertificateError(
                f"the canary log at {path.name} belongs to certificate {stored!r} and this one "
                f"is {fingerprint!r}. Start a new log beside the new certificate."
            )
        return cls(certificate_fingerprint=stored, looks=list(raw.get("looks") or []))


@dataclass(frozen=True)
class CanaryResult:
    """One look: what it found, and what it was able to look at."""

    look: int
    of_looks: int
    alpha_this_look: float
    raised: bool
    reasons: tuple[str, ...]
    #: Whether an **outcome** drift test ran: a re-scored subset whose accuracy difference could
    #: have moved. Deliberately not the same field, or the same word, as the distribution check
    #: below: a look can pass every outcome test while being quoted about tasks the certificate
    #: never saw, and collapsing the two would let the passing one imply the other
    #: (UPGRADE_V4.md M18, PLAN.md section 9).
    drift_tested: bool
    drift_note: str = ""
    observed_delta: float | None = None
    observed_standard_error: float | None = None
    z: float | None = None
    subset_size: int = 0
    #: Whether a **distribution** drift test ran: recent traffic compared, as a distribution, to
    #: the split the certificate was measured on.
    distribution_tested: bool = False
    distribution_note: str = ""
    divergence: Divergence | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "look": self.look,
            "of_looks": self.of_looks,
            "alpha_this_look": self.alpha_this_look,
            "raised": self.raised,
            "reasons": list(self.reasons),
            "drift_tested": self.drift_tested,
            "drift_note": self.drift_note,
            "observed_delta": self.observed_delta,
            "observed_standard_error": self.observed_standard_error,
            "z": self.z,
            "subset_size": self.subset_size,
            "distribution_tested": self.distribution_tested,
            "distribution_note": self.distribution_note,
            "divergence": self.divergence.as_dict() if self.divergence is not None else None,
        }


def stratified_subset(
    task_ids: Sequence[str],
    strata: Sequence[str],
    fraction: float,
    seed: int,
) -> list[str]:
    """A fraction of the tasks, keeping each stratum's share of the whole.

    Stratified because a canary that happened to draw only lookups would miss exactly the drift
    worth catching: a cascade's risk lives in its hardest question types, and those are the
    smallest strata.
    """
    import random

    if len(task_ids) != len(strata):
        raise CertificateError("every task needs a stratum")
    if not 0 < fraction <= 1:
        raise CertificateError("the canary fraction must be in (0, 1]")
    grouped: dict[str, list[str]] = {}
    for task_id, stratum in zip(task_ids, strata, strict=True):
        grouped.setdefault(stratum, []).append(task_id)
    rng = random.Random(seed)
    chosen: list[str] = []
    for stratum in sorted(grouped):
        members = sorted(grouped[stratum])
        take = max(1, round(len(members) * fraction))
        chosen.extend(rng.sample(members, min(take, len(members))))
    return sorted(chosen)


def paired_delta(baseline: Sequence[int], candidate: Sequence[int]) -> tuple[float, float]:
    """(delta, standard error) of the paired accuracy difference, in closed form.

    Closed form rather than a bootstrap: a canary runs on a schedule and the per-task difference
    takes values in {-1, 0, +1}, where the normal approximation to its mean is accurate at the
    subset sizes a canary uses and a great deal cheaper.
    """
    if len(baseline) != len(candidate) or not baseline:
        raise CertificateError("a paired delta needs the same non-empty task set in both arms")
    differences = [c - b for b, c in zip(baseline, candidate, strict=True)]
    n = len(differences)
    mean = sum(differences) / n
    if n < 2:
        return float(mean), float("inf")
    variance = sum((d - mean) ** 2 for d in differences) / (n - 1)
    return float(mean), float(math.sqrt(variance / n))


def take_look(
    certificate: Certificate,
    log: CanaryLog,
    *,
    today: date,
    identity: dict[str, Any] | None = None,
    observed: tuple[float, float] | None = None,
    subset_size: int = 0,
    drift_note: str = "",
    recent: Fingerprint | None = None,
) -> CanaryResult:
    """One look at a standing certificate.

    ``identity`` is what the *current* world says about the models and prices; a mismatch raises
    immediately and is not a statistical question. ``observed`` is a fresh (delta, standard
    error) from a re-scored subset, or ``None`` when there is nothing new to score — replaying
    the cassettes the certificate was issued from cannot produce a different answer, and
    reporting that as a pass would be reporting a tautology as evidence.

    ``recent`` is the fingerprint of the traffic the certificate is being quoted about. It is
    checked against the one the certificate carries and raises on **coverage**: a mix that has
    moved, or a region of it that the certified split barely held. That is a different question
    from ``observed``, which asks whether the outcome moved on tasks the split did cover, and the
    two are reported in separate fields for exactly that reason (UPGRADE_V4.md M18).
    """
    look = log.taken + 1
    levels = certificate.levels()
    if look > len(levels):
        raise CertificateError(
            f"this certificate planned {certificate.looks_planned} looks and {log.taken} have "
            "been taken. There is no alpha left to spend; re-prove and issue a new one."
        )
    level = levels[look - 1]
    reasons: list[str] = []

    if certificate.expired(today):
        reasons.append(
            f"the certificate expired on {certificate.expires.isoformat()} and today is "
            f"{today.isoformat()}. It stopped being a claim about anything."
        )

    if identity is not None:
        current = dict(identity)
        for key in ("price_snapshot_id", "grading_mode", "calibration_mode"):
            if key in current and current[key] != certificate.identity()[key]:
                reasons.append(
                    f"{key} changed: the certificate was issued against "
                    f"{certificate.identity()[key]!r} and this look sees {current[key]!r}."
                )
        if "model_snapshots" in current:
            was = {s["role"]: s["snapshot"] for s in certificate.identity()["model_snapshots"]}
            now = {s["role"]: s["snapshot"] for s in current["model_snapshots"]}
            for role in sorted(set(was) | set(now)):
                if was.get(role) != now.get(role):
                    reasons.append(
                        f"the {role} model changed: {was.get(role)!r} became {now.get(role)!r}. "
                        "A revision shipped under the same name is the case this check exists "
                        "for, and nothing downstream of it can be trusted until it is re-proven."
                    )

    z: float | None = None
    delta: float | None = None
    standard_error: float | None = None
    drift_tested = observed is not None
    if observed is not None:
        delta, standard_error = observed
        combined = math.sqrt(certificate.standard_error**2 + standard_error**2)
        if combined <= 0:
            raise CertificateError("a drift test needs a positive combined standard error")
        z = (delta - certificate.delta_point) / combined
        critical = float(scipy_stats.norm.ppf(1 - level / 2))
        if abs(z) > critical:
            reasons.append(
                f"the accuracy difference moved: {delta * 100:+.1f} points against the "
                f"certificate's {certificate.delta_point * 100:+.1f}, z = {z:+.2f} against a "
                f"critical value of {critical:.2f} at this look's alpha of {level:.4f}."
            )

    # Distribution drift. Separate from everything above: it asks whether the split still
    # covers the traffic, not whether the outcome moved on the traffic it covers. It spends no
    # alpha, because it is not a test of a random quantity — a mix either moved or it did not.
    gap: Divergence | None = None
    distribution_tested = False
    if recent is None:
        distribution_note = (
            "No distribution check: this look was not given the recent traffic to fingerprint. "
            "The outcome checks above say nothing about whether the certified split still covers "
            "what is arriving."
        )
    elif certificate.workload_fingerprint is None:
        distribution_note = (
            "No distribution check: this certificate was issued without a workload fingerprint, "
            "so there is nothing to compare recent traffic against. Re-issue it to gain one."
        )
    else:
        distribution_tested = True
        gap = divergence(certificate.workload_fingerprint, recent)
        reasons.extend(gap.reasons)
        distribution_note = (
            f"compared {recent.n} recent tasks against the {certificate.workload_fingerprint.n} "
            f"the certificate was measured on; total variation {gap.total_variation:.2f}"
            + ("" if gap.drifted else ", within the covered region")
        )

    result = CanaryResult(
        look=look,
        of_looks=certificate.looks_planned,
        alpha_this_look=level,
        raised=bool(reasons),
        reasons=tuple(reasons),
        drift_tested=drift_tested,
        drift_note=drift_note
        or (
            ""
            if drift_tested
            else "no drift test: this look re-scored the recording the certificate was issued "
            "from, and replaying the same calls cannot produce a different answer. The identity "
            "checks above still apply."
        ),
        observed_delta=delta,
        observed_standard_error=standard_error,
        z=z,
        subset_size=subset_size,
        distribution_tested=distribution_tested,
        distribution_note=distribution_note,
        divergence=gap,
    )
    log.looks.append({"taken_on": today.isoformat(), **result.as_dict()})
    return result
