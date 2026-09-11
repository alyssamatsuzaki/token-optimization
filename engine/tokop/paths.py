"""Filesystem layout. Every path in the engine resolves through here."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """The repository root.

    Resolved from this file's location, so the engine works the same whether it is invoked
    through the CLI, through uvicorn, or from a test. ``TOKOP_ROOT`` overrides it, which is how
    tests point the engine at a temporary tree.
    """
    override = os.environ.get("TOKOP_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent.parent


def config_dir() -> Path:
    return repo_root() / "config"


def data_dir() -> Path:
    return repo_root() / "data"


def fixtures_dir() -> Path:
    return repo_root() / "fixtures"


def docs_dir() -> Path:
    return repo_root() / "docs"


def web_dist_dir() -> Path:
    return repo_root() / "web" / "dist"


def state_dir() -> Path:
    """Where the local ledger lives. Created on demand."""
    override = os.environ.get("TOKOP_STATE_DIR")
    path = Path(override).resolve() if override else repo_root() / ".tokop"
    path.mkdir(parents=True, exist_ok=True)
    return path
