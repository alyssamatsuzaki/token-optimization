"""FastAPI application: /api/* plus the built SPA (SPEC.md section 4)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from tokop import __version__, recording_state
from tokop.paths import web_dist_dir
from tokop.settings import get_settings

app = FastAPI(title="Tokop", version=__version__)


@app.get("/api/health")
def health() -> dict[str, object]:
    settings = get_settings()
    state = recording_state.describe()
    return {
        "version": __version__,
        "mode": settings.mode,
        "recording": state.to_dict(),
    }


def mount_spa(application: FastAPI, dist: Path) -> None:
    """Serve the built SPA, falling back to index.html so client routes work on reload."""
    if not dist.exists():
        return
    application.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @application.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


mount_spa(app, web_dist_dir())
