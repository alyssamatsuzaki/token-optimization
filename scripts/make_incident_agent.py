#!/usr/bin/env python3
"""Generate the incident-agent workload: the same tasks, shaped as an agent (UPGRADE_V4.md M16).

Run with `python scripts/make_incident_agent.py`, which rewrites `data/incident-agent/`.

It writes no tasks of its own. It copies the runbook and the dataset from `data/incident-triage/`
and changes one thing: the **pipelines**. That is the whole point. A graph workload whose tasks
also differed from the single-call one would be comparing two things at once, and the question
M16 exists to answer — what does the *shape* of a pipeline cost, over and above its prompt and
its model — needs the tasks held fixed.

Like the script it copies from, this lives in `scripts/` and not in the engine: the engine has
to be able to run a workload without importing whatever produced it (DECISIONS.md D44).
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "incident-triage"
OUT = ROOT / "data" / "incident-agent"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ("runbook.md", "dataset.jsonl"):
        source = SOURCE / name
        if not source.exists():
            raise SystemExit(f"{source} does not exist; run scripts/make_incident_triage.py first")
        shutil.copyfile(source, OUT / name)
    items = (OUT / "dataset.jsonl").read_text().strip().splitlines()
    calibration = sum(1 for line in items if '"split": "calibration"' in line)
    print(f"copied {len(items)} items ({calibration} calibration, {len(items) - calibration} test)")
    print("copied runbook.md; workload.yaml is hand-written and is not touched")


if __name__ == "__main__":
    main()
