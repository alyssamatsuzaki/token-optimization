from __future__ import annotations

import runpy
from pathlib import Path


def _interpret(*, label: str, low: float, additional: int | None = None) -> tuple[str, str]:
    script = Path(__file__).parents[2] / "scripts" / "write_demo.py"
    interpret = runpy.run_path(str(script))["_proof_interpretation"]
    return interpret(
        {
            "delta_accuracy": {"low": low},
            "verdict": {
                "label": label,
                "margin": 0.03,
                "additional_tasks_needed": additional,
            },
        }
    )


def test_demo_explains_a_non_inferior_verdict_as_a_pass() -> None:
    explanation, faq = _interpret(label="non_inferior", low=-0.0245)

    assert "conclusive non-inferiority" in explanation
    assert "Why does the verdict pass" in faq
    assert "inconclusive" not in explanation


def test_demo_explains_an_inconclusive_verdict_and_its_remedy() -> None:
    explanation, faq = _interpret(label="inconclusive", low=-0.031, additional=42)

    assert "*inconclusive*" in explanation
    assert "42 more tasks" in explanation
    assert "42 more tasks" in faq


def test_demo_explains_a_worse_verdict_as_a_rejection() -> None:
    explanation, faq = _interpret(label="worse", low=-0.08)

    assert "rejects the candidate" in explanation
    assert "cheaper candidate rejected" in faq
