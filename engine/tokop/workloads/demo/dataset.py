"""Write, load and verify the committed demo dataset (SPEC.md section 6).

The dataset is committed as JSONL so a reader can audit it without running anything, and
``tokop fixtures-check`` regenerates every gold answer from ``policy.yaml`` and asserts the
stored file still matches. That check is the guarantee behind the whole demo: if someone edits
a policy number without regenerating, the build fails rather than quietly answering questions
against a handbook that no longer says what the gold answers assume.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tokop.paths import data_dir, docs_dir
from tokop.workloads.bundle import Bundle
from tokop.workloads.demo.generator import generate, split_items
from tokop.workloads.demo.handbook import handbook_size_report, render_handbook
from tokop.workloads.demo.policy import Policy, load_policy
from tokop.workloads.item import Item

DATASET_FILE = "dataset.jsonl"
HANDBOOK_FILE = "handbook.md"


def build(
    policy: Policy | None = None,
    *,
    size: int = 300,
    calibration_size: int = 100,
    seed: int = 20260911,
) -> Bundle:
    resolved = policy or load_policy()
    items = generate(resolved, size=size, seed=seed)
    split = split_items(items, calibration_size=calibration_size, seed=seed)
    return Bundle(
        items=tuple(items),
        grounding=render_handbook(resolved),
        calibration=split.calibration,
        test=split.test,
        seed=seed,
    )


def dataset_path() -> Path:
    return data_dir() / "demo" / DATASET_FILE


def handbook_path() -> Path:
    return data_dir() / "demo" / HANDBOOK_FILE


def write(bundle: Bundle) -> tuple[Path, Path]:
    """Write the dataset and the rendered handbook to data/demo/."""
    split_of = {i.id: "calibration" for i in bundle.calibration}
    split_of.update({i.id: "test" for i in bundle.test})

    lines = []
    for item in bundle.items:
        payload = item.to_dict()
        payload["split"] = split_of[item.id]
        lines.append(json.dumps(payload, sort_keys=True))
    path = dataset_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")

    hb = handbook_path()
    hb.write_text(bundle.grounding)
    return path, hb


def _sections_of(row: dict[str, Any]) -> list[str]:
    """The supporting section IDs of a stored row, whatever JSON handed back."""
    raw = row.get("sections")
    return [str(x) for x in raw] if isinstance(raw, list) else []


def load(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or dataset_path()
    if not target.exists():
        raise FileNotFoundError(
            f"no dataset at {target}. Run `tokop dataset --write` to generate it."
        )
    return [json.loads(line) for line in target.read_text().splitlines() if line.strip()]


@dataclass(frozen=True)
class CheckResult:
    """What fixtures-check found."""

    ok: bool
    checks: list[tuple[str, bool, str]]

    def failures(self) -> list[tuple[str, bool, str]]:
        return [c for c in self.checks if not c[1]]


def verify(policy: Policy | None = None) -> CheckResult:
    """Regenerate from YAML and assert the committed dataset still matches."""
    checks: list[tuple[str, bool, str]] = []
    bundle = build(policy)

    try:
        stored = load()
    except FileNotFoundError as exc:
        return CheckResult(False, [("dataset present", False, str(exc))])

    checks.append(
        (
            "dataset size",
            len(stored) == len(bundle.items),
            f"{len(stored)} stored, {len(bundle.items)} regenerated",
        )
    )

    regenerated = {item.id: item for item in bundle.items}
    stored_by_id = {str(row["id"]): row for row in stored}

    missing = sorted(set(regenerated) - set(stored_by_id))
    extra = sorted(set(stored_by_id) - set(regenerated))
    checks.append(
        (
            "every regenerated item is stored",
            not missing,
            f"{len(missing)} missing" + (f": {missing[:3]}" if missing else ""),
        )
    )
    checks.append(
        (
            "no stale items in the stored dataset",
            not extra,
            f"{len(extra)} extra" + (f": {extra[:3]}" if extra else ""),
        )
    )

    mismatched: list[str] = []
    for item_id, item in regenerated.items():
        row = stored_by_id.get(item_id)
        if row is None:
            continue
        if (
            row.get("gold") != item.gold
            or row.get("question") != item.question
            or row.get("answer_type") != item.answer_type
            or row.get("question_type") != item.question_type
            or _sections_of(row) != list(item.sections)
        ):
            mismatched.append(item_id)
    checks.append(
        (
            "gold answers regenerate identically",
            not mismatched,
            f"{len(mismatched)} mismatched"
            + (f": {mismatched[:3]}" if mismatched else " — every answer recomputed from YAML"),
        )
    )

    stored_handbook = handbook_path()
    checks.append(
        (
            "handbook matches the policy",
            stored_handbook.exists() and stored_handbook.read_text() == bundle.grounding,
            "rendered handbook is byte-identical to the committed one",
        )
    )

    from tokop.core.registry import load_registry

    registry = load_registry()
    minimums = {}
    ratios = {}
    for role in ("frontier", "mid", "cheap"):
        entry = registry.role(role)
        minimums[entry.model_id] = entry.min_cacheable_tokens or 0
        ratios[entry.model_id] = 1.3 if entry.tokenizer_generation == "newer" else 1.0
    report = handbook_size_report(bundle.grounding, minimums, ratios)
    checks.append(
        (
            "cached prefix clears every minimum by 20%",
            report.clears_every_minimum_by_20_percent,
            "; ".join(report.failures())
            or ", ".join(
                f"{m}: {report.tokens_by_model[m]:,} tok ({report.headroom[m]:.0%} headroom)"
                for m in sorted(report.tokens_by_model)
            ),
        )
    )

    splits = {row.get("split") for row in stored}
    checks.append(
        (
            "split is calibration/test only",
            splits == {"calibration", "test"},
            f"found {sorted(str(s) for s in splits)}",
        )
    )
    cal = sum(1 for row in stored if row.get("split") == "calibration")
    checks.append(
        (
            "split sizes",
            cal == len(bundle.calibration),
            f"{cal} calibration, {len(stored) - cal} test",
        )
    )

    return CheckResult(all(c[1] for c in checks), checks)


SAMPLES_PER_TYPE = 10


def write_dataset_doc(bundle: Bundle | None = None) -> Path:
    """docs/DATASET.md: ten sample items per question type, so the set can be audited
    before any money is spent (SPEC.md section 6)."""
    from tokop.core.tokenize import base_counter_name, get_base_counter
    from tokop.workloads.demo.handbook import section_text

    resolved = bundle or build()
    by_type: dict[str, list[Item]] = {}
    for item in sorted(resolved.items, key=lambda i: i.id):
        by_type.setdefault(item.question_type, []).append(item)

    counter = get_base_counter()
    base_tokens = counter.count(resolved.grounding)

    lines = [
        "# The demo dataset",
        "",
        "Generated from `data/demo/policy.yaml` by `tokop dataset --write`. Every gold answer is",
        "**computed from the policy in code**, never written by hand, so a question and its answer",
        "cannot drift apart. `tokop fixtures-check` regenerates the whole set and fails the",
        "build if the committed file no longer matches.",
        "",
        "Read this before running `make record`: once you set `RECORD_BUDGET_USD`, these are the",
        "questions your money will be spent answering.",
        "",
        "## Shape",
        "",
        f"- {len(resolved.items)} questions, {len(resolved.calibration)} calibration and "
        f"{len(resolved.test)} test, stratified by question type with seed {resolved.seed}.",
        f"- Handbook: {len(resolved.grounding):,} characters, {base_tokens:,} tokens on "
        f"`{base_counter_name()}`.",
        "- The router never sees the question type or the supporting section IDs. They are"
        " recorded for analysis after the fact only.",
        "",
        "| Question type | Count | Share |",
        "| --- | ---: | ---: |",
    ]
    for question_type in sorted(by_type):
        count = len(by_type[question_type])
        lines.append(f"| {question_type} | {count} | {count / len(resolved.items):.0%} |")

    lines += ["", "## Samples", ""]
    for question_type in sorted(by_type):
        lines += [
            f"### {question_type}",
            "",
            "| Question | Gold | Supporting quote |",
            "| --- | --- | --- |",
        ]
        for item in by_type[question_type][:SAMPLES_PER_TYPE]:
            body = section_text(resolved.grounding, item.sections[0])
            quote = " ".join(body.split())[:150]
            question = item.question.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {question} | `{item.gold}` | {quote}… |")
        lines.append("")

    lines += [
        "## How each answer is computed",
        "",
        "Every template names the policy function that produces its gold answer. There is no"
        " second implementation to disagree with.",
        "",
        "| Template | Type | Answer | Sections |",
        "| --- | --- | --- | --- |",
    ]
    seen: set[str] = set()
    for item in sorted(resolved.items, key=lambda i: i.template_id):
        if item.template_id in seen:
            continue
        seen.add(item.template_id)
        lines.append(
            f"| `{item.template_id}` | {item.question_type} | {item.answer_type} | "
            f"{', '.join(item.sections)} |"
        )

    path = docs_dir() / "DATASET.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path
