"""Runtime settings, read once from the environment.

Budget values are read, never written. Nothing in the engine may raise a cap (SPEC.md
section 1, "Money"). ``.env`` at the repository root is loaded if present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from dotenv import load_dotenv

from tokop.paths import repo_root


class Mode:
    LIVE = "live"
    REPLAY = "replay"


@dataclass(frozen=True)
class Settings:
    mode: str
    anthropic_api_key: str | None
    openrouter_api_key: str | None
    openai_api_key: str | None
    gemini_api_key: str | None
    deepseek_api_key: str | None
    record_budget_usd: Decimal | None
    daily_budget_usd: Decimal | None

    @property
    def is_replay(self) -> bool:
        return self.mode == Mode.REPLAY

    def api_key_for(self, env_var: str | None) -> str | None:
        if env_var is None:
            return None
        return os.environ.get(env_var) or None

    def configured_providers(self) -> dict[str, bool]:
        """Which providers have a key. Never returns the keys themselves (non-negotiable 7)."""
        return {
            "anthropic": bool(self.anthropic_api_key),
            "openai": bool(self.openai_api_key),
            "openrouter": bool(self.openrouter_api_key),
            "gemini": bool(self.gemini_api_key),
            "deepseek": bool(self.deepseek_api_key),
        }


def _decimal_or_none(raw: str | None) -> Decimal | None:
    if raw is None or raw.strip() == "":
        return None
    return Decimal(raw.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(repo_root() / ".env", override=False)
    mode = (os.environ.get("TOKOP_MODE") or Mode.REPLAY).strip().lower()
    if mode not in (Mode.LIVE, Mode.REPLAY):
        raise ValueError(f"TOKOP_MODE must be 'live' or 'replay', got {mode!r}")
    return Settings(
        mode=mode,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
        record_budget_usd=_decimal_or_none(os.environ.get("RECORD_BUDGET_USD")),
        daily_budget_usd=_decimal_or_none(os.environ.get("DAILY_BUDGET_USD")),
    )


def reset_settings_cache() -> None:
    """Tests change the environment and re-read settings."""
    get_settings.cache_clear()
