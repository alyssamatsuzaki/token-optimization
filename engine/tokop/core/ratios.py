"""Fit the per-model token ratios from recorded usage (SPEC.md 7.3).

``core/tokenize.py`` can correct a base token count into a specific model's count, but only if
somebody fits the ratio. This is the piece that does it, from the cassettes on disk: for every
recorded call, compare the base counter's count of the request text against the input token
count the provider reported, and fit a ratio through the origin per model.

Without this the estimator returns ratio 1.0 for every model and Inspect shows identical token
counts for Opus 5 and Haiku 4.5 — models that really differ by about 30%, because Anthropic's
tokenizer changed at Claude 4.7. That is not a rounding error; it is the per-model cost column
being wrong by a third for half the registry.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from tokop.adapters.cassette import CassetteStore
from tokop.core.tokenize import ModelRatio, TokenEstimator, fit_ratio, get_base_counter


def _request_text(request: dict[str, object]) -> str:
    """The text a request sent, reassembled from its canonical form."""
    parts: list[str] = []
    system = request.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    messages = request.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            blocks = message.get("blocks")
            if isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        parts.append(block["text"])
    return "\n\n".join(parts)


def fit_from_cassettes(store: CassetteStore) -> dict[str, ModelRatio]:
    """Fit one ratio per model from every cassette in a store.

    Uses **total input tokens** — uncached plus cache writes plus cache reads — because that is
    what the request's text actually cost to process. Using ``input_tokens`` alone would compare
    the base count of the whole prompt against the tokens after the last cache breakpoint, and
    fit a ratio near zero.
    """
    counter = get_base_counter()
    base: dict[str, list[int]] = defaultdict(list)
    provider: dict[str, list[int]] = defaultdict(list)

    for cassette in store.all():
        if cassette.error:
            continue
        text = _request_text(cassette.request)
        if not text:
            continue
        base[cassette.model].append(counter.count(text))
        provider[cassette.model].append(cassette.usage.total_input)

    return {
        model: fit_ratio(model, base[model], provider[model])
        for model in sorted(base)
        if base[model]
    }


@lru_cache(maxsize=2)
def _fit_estimator(cassette_dir: Path | None = None) -> TokenEstimator:
    from tokop.paths import fixtures_dir
    from tokop.recording_state import describe

    root = cassette_dir or fixtures_dir() / describe().fixture_source / "cassettes"
    estimator = TokenEstimator()
    if not root.exists():
        return estimator
    for ratio in fit_from_cassettes(CassetteStore(root)).values():
        estimator.set_ratio(ratio)
    return estimator


#: One fit at a time. ``lru_cache`` memoizes a *result*; it does not stop two threads both
#: missing and both doing the work. Fitting walks every cassette in the recording — nearly seven
#: thousand files, about nineteen seconds here — and the API serves sync endpoints from a
#: threadpool, so two browser tabs opening Inspect together used to pay for it twice, four tabs
#: four times, each one slower than the last as they contend. The lock makes the second arrival
#: wait for the first's answer instead of recomputing it.
_FIT_LOCK = threading.Lock()


def fitted_estimator(cassette_dir: Path | None = None) -> TokenEstimator:
    """A ``TokenEstimator`` with every ratio this build can fit.

    Cached, and cached **once**: fitting walks every cassette, and the answer only changes when
    the fixtures do.
    """
    with _FIT_LOCK:
        return _fit_estimator(cassette_dir)


def clear_cache() -> None:
    with _FIT_LOCK:
        _fit_estimator.cache_clear()
