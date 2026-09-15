"""A workload's tasks, loaded from what the workload declares (UPGRADE_V4.md M15).

Before M15 the report, the recorder and the annotator all called the demo's *generator* to get
their tasks. That is why `tokop prove --workload` could only refuse anything else: there was no
other way to get a task into the engine.

Loading replaces generating. Every workload — the demo included — is read from the dataset file
committed beside it, because a committed file is the thing a recording was made against and a
generator is only the thing that produced the file once. `tokop fixtures-check` already asserts
the two agree, so reading the file rather than re-running the generator changes no number and
removes the last reason for a neutral module to import a workload-specific one.

Splits are read, never recomputed. A task that moved from the calibration side to the test side
between two runs would invalidate every result measured before the move, silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tokop.paths import data_dir
from tokop.workloads.item import IngestedItem, Item
from tokop.workloads.spec import WorkloadError, WorkloadSpec


@dataclass(frozen=True)
class Bundle:
    """The tasks of one workload, split, plus the document they are grounded in."""

    items: tuple[Item, ...]
    #: The text every pipeline is given to answer from, rendered into prompts as `{{grounding}}`.
    #: The demo calls its own one a handbook; the engine does not need to know that.
    grounding: str
    calibration: tuple[Item, ...]
    test: tuple[Item, ...]
    seed: int

    @property
    def by_id(self) -> dict[str, Item]:
        return {item.id: item for item in self.items}


def workload_dir(workload: WorkloadSpec) -> Path:
    """Where this workload's files live: beside the YAML that defines it."""
    if workload.directory is not None:
        return workload.directory
    return data_dir() / workload.id


def read_items(path: Path) -> list[IngestedItem]:
    """Read a dataset file: one JSON object per line, one task each."""
    if not path.exists():
        raise WorkloadError(
            f"no dataset at {path}. A workload is its tasks; generate them, or ingest them "
            "with `tokop ingest`."
        )
    items: list[IngestedItem] = []
    known = {
        "id",
        "question",
        "gold",
        "answer_type",
        "question_type",
        "aliases",
        "template_id",
        "sections",
        "split",
    }
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkloadError(f"{path}:{number} is not valid JSON: {exc}") from exc
        missing = [
            field for field in ("id", "question", "gold", "answer_type") if not row.get(field)
        ]
        if missing:
            raise WorkloadError(f"{path}:{number} is missing {', '.join(missing)}")
        items.append(
            IngestedItem(
                id=str(row["id"]),
                question=str(row["question"]),
                gold=str(row["gold"]),
                answer_type=row["answer_type"],
                question_type=str(row.get("question_type") or "unclassified"),
                aliases=tuple(row.get("aliases") or ()),
                template_id=str(row.get("template_id") or ""),
                sections=tuple(row.get("sections") or ()),
                split=str(row.get("split") or "test"),
                # Columns Tokop has no field for are kept rather than dropped, so an ingestion
                # can be audited against the file it came from.
                extra={k: v for k, v in row.items() if k not in known},
            )
        )
    if not items:
        raise WorkloadError(f"{path} has no tasks")
    return items


def bundle_from(items: list[IngestedItem], grounding: str, seed: int) -> Bundle:
    """Assemble a bundle from items that already know which split they are in."""
    ordered = sorted(items, key=lambda item: item.id)
    calibration = tuple(item for item in ordered if item.split == "calibration")
    test = tuple(item for item in ordered if item.split != "calibration")
    if not test:
        raise WorkloadError(
            "every task is in the calibration split, so there is nothing to prove anything on"
        )
    return Bundle(
        items=tuple(ordered),
        grounding=grounding,
        calibration=calibration,
        test=test,
        seed=seed,
    )


def load_bundle(workload: WorkloadSpec) -> Bundle:
    """The tasks this workload declares, and the document they are answered from."""
    directory = workload_dir(workload)
    dataset = workload.dataset
    grounding_file = directory / dataset.grounding if dataset.grounding else None
    if grounding_file is not None and not grounding_file.exists():
        raise WorkloadError(
            f"{workload.id} declares grounding {dataset.grounding!r} and "
            f"{grounding_file} does not exist"
        )
    grounding = grounding_file.read_text() if grounding_file is not None else ""
    bundle = bundle_from(read_items(directory / dataset.source), grounding, dataset.seed)
    if len(bundle.items) != dataset.size:
        raise WorkloadError(
            f"{workload.id} declares {dataset.size} tasks and {dataset.source} holds "
            f"{len(bundle.items)}. A size that does not match the file is a size nobody checked."
        )
    return bundle
