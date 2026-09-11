"""FastAPI application: /api/* plus the built SPA (SPEC.md section 4)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from tokop import __version__
from tokop.api.routes import router
from tokop.paths import web_dist_dir

app = FastAPI(title="Tokop", version=__version__)
app.include_router(router)


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
