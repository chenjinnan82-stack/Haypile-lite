from __future__ import annotations

import asyncio
import json
import logging
import os
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
from app.api.v1.batches import router as batches_router
from app.api.v1.theme import router as theme_router
from app.core.config import _ensure_private_directory, get_settings
from app.core.exceptions import register_exception_handlers
from app.services.ingest_service import IngestService
from app.services.scanner import manifest_dirty_path
from app.services.asset_integrity import asset_file_sha256
from app.services.storage_runtime import StorageRuntimeDB

logger = logging.getLogger(__name__)
settings = get_settings()

_ensure_private_directory(settings.STORAGE_DIR)
settings.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
settings.INDEX_DIR.mkdir(parents=True, exist_ok=True)


class ManifestStaticFiles(StaticFiles):
    def __init__(self, *, directory: str, manifest_path: Path, name: str | None = None) -> None:
        super().__init__(directory=directory)
        self.manifest_path = manifest_path
        self.dirty_path = manifest_dirty_path(manifest_path)
        self.assets_root = Path(directory).resolve(strict=False)
        self.name = name
        self._manifest_signature: tuple[int, ...] | None = None
        self._cached_manifest: dict[str, dict] = {}

    def lookup_path(self, path: str):
        normalized = path.replace("\\", "/").lstrip("/")
        item = self._manifest_entries().get(normalized)
        if item is None:
            return "", None
        full_path, stat_result = super().lookup_path(path)
        if not full_path:
            return "", None
        candidate = Path(full_path)
        if candidate.is_symlink() or (self.assets_root / normalized).is_symlink():
            return "", None
        try:
            candidate.resolve(strict=False).relative_to(self.assets_root)
        except ValueError:
            return "", None
        expected = str(item.get("sha256") or "").lower()
        actual = asset_file_sha256(candidate, legacy_type=None if expected else str(item.get("type") or ""))
        if not actual or (expected and expected != actual) or self.dirty_path.exists():
            return "", None
        return full_path, stat_result

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def _manifest_entries(self) -> dict[str, dict]:
        if self.dirty_path.exists():
            self._manifest_signature = None
            self._cached_manifest = {}
            return {}
        try:
            stat = self.manifest_path.stat()
        except OSError:
            self._manifest_signature = None
            self._cached_manifest = {}
            return {}
        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        # Windows ctime is creation-time and cannot invalidate equal-length edits.
        if os.name != "nt" and signature == self._manifest_signature:
            return self._cached_manifest
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._manifest_signature = None
            self._cached_manifest = {}
            return {}
        if not isinstance(payload, dict):
            self._manifest_signature = None
            self._cached_manifest = {}
            return {}
        recorded = {str(path): digest for digest, path in StorageRuntimeDB.read_recorded_asset_hash_index(
            self.manifest_path.parent / "storage_runtime.db", self.assets_root).items()}
        entries = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            normalized = str(key).replace("\\", "/").lstrip("/")
            expected = recorded.get(str((self.assets_root / normalized).resolve(strict=False)))
            if expected and value.get("sha256") and value["sha256"] != expected:
                continue
            entries[normalized] = {**value, "sha256": expected or value.get("sha256", "")}
        self._manifest_signature = signature
        self._cached_manifest = entries
        return entries


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    service = IngestService(
        storage_dir=settings.STORAGE_DIR,
        assets_dir=settings.ASSETS_DIR,
        index_dir=settings.INDEX_DIR,
        themes_dir=settings.THEMES_DIR,
        manifest_path=settings.MANIFEST_PATH,
        fallback_theme=settings.VISION_FALLBACK_THEME,
    )
    try:
        result = await asyncio.to_thread(
            service.recover_and_project,
            lock_timeout=8.0,
        )
    except Exception as exc:
        logger.critical(
            "Storage initialization failed: error_type=%s", type(exc).__name__
        )
        raise RuntimeError("Haypile storage initialization failed") from exc
    if result.success:
        logger.info("Assets manifest has been generated.")
    elif result.error_code == "manifest_projection_failed":
        logger.error("Initial asset scan failed: error_code=%s", result.error_code)
    else:
        logger.critical("Storage initialization failed: error_code=%s", result.error_code)
        raise RuntimeError("Haypile storage initialization failed")
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
app.include_router(batches_router, prefix="/api/v1")
app.mount(
    "/static",
    ManifestStaticFiles(directory=str(settings.ASSETS_DIR), manifest_path=settings.MANIFEST_PATH),
    name="static",
)
