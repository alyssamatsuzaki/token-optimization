"""`tokop sync-models`: pull the OpenRouter public model listing into the registry.

The listing is a convenience, not a source of truth. It gives Tokop broad model coverage for
the Compare screen without hand-maintaining a price table for every vendor, but a gateway
reports what *it* charges, which can differ from the provider's own published price. So
everything from the listing loads as ``verified: false`` and the UI marks it, while
``config/prices.yaml`` — read off the provider's own page, with the URL and the date — always
wins (SPEC.md 7.3).

The endpoint is public and needs no key.
https://openrouter.ai/docs/api-reference/overview
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from tokop.core.registry import OPENROUTER_MODELS_URL, parse_openrouter_listing


class SyncError(RuntimeError):
    """The model listing could not be fetched or parsed."""


def fetch_listing(url: str = OPENROUTER_MODELS_URL, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch the listing. Raises with the reason rather than returning a stale snapshot."""
    try:
        response = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        raise SyncError(
            f"could not reach {url}: {type(exc).__name__}: {exc}. The committed snapshot is "
            "still in use and still carries the date it was taken."
        ) from exc
    if response.status_code != 200:
        raise SyncError(f"{url} returned HTTP {response.status_code}: {response.text[:200]}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise SyncError(f"{url} did not return a JSON object")
    payload["_tokop_retrieved"] = datetime.now(UTC).date().isoformat()
    payload["_tokop_note"] = f"fetched from {url}"
    return payload


def write_snapshot(payload: dict[str, Any], path: Path) -> int:
    """Write the listing to disk and report how many models it yielded."""
    entries, _ = parse_openrouter_listing(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return len(entries)


def snapshot_age_days(taken: date | None, today: date | None = None) -> int | None:
    if taken is None:
        return None
    return ((today or date.today()) - taken).days
