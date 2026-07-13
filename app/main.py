from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.health import router as health_router
from app.api.v1.bundles import router as bundles_router
from app.api.v1.theme import router as theme_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.services.scanner import AssetScanner
from app.services.json_io import atomic_write_json

logger = logging.getLogger(__name__)
settings = get_settings()

settings.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
settings.INDEX_DIR.mkdir(parents=True, exist_ok=True)


class ManifestStaticFiles(StaticFiles):
    def __init__(self, *, directory: str, manifest_path: Path, name: str | None = None) -> None:
        super().__init__(directory=directory)
        self.manifest_path = manifest_path
        self.assets_root = Path(directory).resolve(strict=False)
        self.name = name

    def lookup_path(self, path: str):
        normalized = path.replace("\\", "/").lstrip("/")
        if normalized not in self._manifest_keys():
            return "", None
        full_path, stat_result = super().lookup_path(path)
        if not full_path:
            return "", None
        candidate = Path(full_path)
        if candidate.is_symlink():
            return "", None
        try:
            candidate.resolve(strict=False).relative_to(self.assets_root)
        except ValueError:
            return "", None
        return full_path, stat_result

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def _manifest_keys(self) -> set[str]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(payload, dict):
            return set()
        return {str(key).replace("\\", "/").lstrip("/") for key in payload}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    scanner = AssetScanner()
    try:
        await scanner.scan_assets_directory()
        logger.info("Assets manifest has been generated: %s", settings.MANIFEST_PATH)
    except Exception:
        logger.exception("Initial asset scan failed.")
        if not settings.MANIFEST_PATH.exists():
            atomic_write_json(settings.MANIFEST_PATH, {})
    yield


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def attach_request_id(request: Request, call_next) -> Response:
    incoming_request_id = request.headers.get("X-Request-ID")
    request_id = incoming_request_id or str(uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    if request.url.path.startswith(("/api/", "/healthz", "/readyz")):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


register_exception_handlers(app)
app.include_router(health_router)
app.include_router(theme_router, prefix="/api/v1")
app.include_router(bundles_router, prefix="/api/v1")
app.mount(
    "/static",
    ManifestStaticFiles(directory=str(settings.ASSETS_DIR), manifest_path=settings.MANIFEST_PATH),
    name="static",
)
