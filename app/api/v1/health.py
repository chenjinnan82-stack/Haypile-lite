from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.core.config import get_settings
from app.services.scanner import ManifestReadinessError, read_manifest_readiness

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str | int]:
    settings = get_settings()
    try:
        readiness = read_manifest_readiness(settings.MANIFEST_PATH)
    except ManifestReadinessError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Service not ready: {exc}.",
        ) from exc
    return {"status": "ok", **readiness}
