"""Which demo recording this build has, and why (SPEC.md section 10, check 7).

Three states, and the CLI, the API and the final report all read them from here so they
cannot drift apart:

``recorded``      ANTHROPIC_API_KEY and RECORD_BUDGET_USD were both set and fixtures/demo/
                  holds a complete recording.
``awaiting_review`` RECORD_BUDGET_USD was left empty on purpose. docs/DATASET.md exists so the
                  dataset can be audited before any money is spent; `make record` comes next.
``unrecorded``    Anything else. The reason names what is missing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from tokop.paths import docs_dir, fixtures_dir
from tokop.settings import get_settings

RECORDED = "recorded"
AWAITING_REVIEW = "awaiting_review"
UNRECORDED = "unrecorded"


@dataclass(frozen=True)
class RecordingState:
    state: str
    reason: str
    demo_manifest: Path | None
    fixture_source: str  # "demo" or "test"

    @property
    def is_test_data(self) -> bool:
        return self.fixture_source == "test"

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "fixture_source": self.fixture_source,
            "is_test_data": self.is_test_data,
        }


def _demo_recording_is_complete() -> bool:
    manifest = fixtures_dir() / "demo" / "manifest.json"
    if not manifest.exists():
        return False
    try:
        data = json.loads(manifest.read_text())
    except json.JSONDecodeError:
        return False
    return bool(data.get("complete")) and bool(data.get("runs"))


def describe() -> RecordingState:
    settings = get_settings()
    has_key = bool(settings.anthropic_api_key)
    budget_raw = os.environ.get("RECORD_BUDGET_USD")
    budget_set = budget_raw is not None and budget_raw.strip() != ""
    complete = _demo_recording_is_complete()
    source = "demo" if complete else "test"
    manifest = fixtures_dir() / "demo" / "manifest.json" if complete else None

    if has_key and budget_set and complete:
        return RecordingState(
            RECORDED, "fixtures/demo/ holds a complete recording.", manifest, source
        )

    if has_key and budget_set and not complete:
        return RecordingState(
            UNRECORDED,
            "ANTHROPIC_API_KEY and RECORD_BUDGET_USD are both set but fixtures/demo/ has no "
            "complete recording. Run `make record`.",
            None,
            source,
        )

    if not budget_set and (docs_dir() / "DATASET.md").exists():
        return RecordingState(
            AWAITING_REVIEW,
            "RECORD_BUDGET_USD is empty, so no money has been spent. The demo dataset is in "
            "docs/DATASET.md for review; run `make record` after reading it. The app runs on "
            "fixtures/test/, which is simulated test data, not a recording.",
            None,
            source,
        )

    missing = []
    if not has_key:
        missing.append("ANTHROPIC_API_KEY is not set")
    if not budget_set:
        missing.append("RECORD_BUDGET_USD is not set")
    if not (docs_dir() / "DATASET.md").exists():
        missing.append("docs/DATASET.md has not been generated")
    return RecordingState(
        UNRECORDED,
        "The demo is unrecorded: " + "; ".join(missing) + ".",
        None,
        source,
    )
