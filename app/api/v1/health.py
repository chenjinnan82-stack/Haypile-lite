from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    settings = get_settings()
    if not settings.MANIFEST_PATH.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service not ready: assets manifest not found.",
        )
    return {"status": "ok"}
