from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.models.bundle import BundlePayload
from app.services.bundle_service import BundleService
from app.services.scanner import ManifestReadinessError

router = APIRouter(prefix="/bundles", tags=["bundles"])


def get_bundle_service() -> BundleService:
    return BundleService()


@router.get("", response_model=list[BundlePayload])
def list_bundles(
    response: Response,
    status_filter: str | None = Query(default=None, alias="status"),
    asset_type: str | None = Query(default=None, alias="type"),
    role: str | None = None,
    theme_id: str | None = None,
    audio_usage: str | None = None,
    batch_id: str | None = Query(default=None, max_length=64),
    limit: int | None = Query(default=None, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    bundle_service: BundleService = Depends(get_bundle_service),
) -> list[BundlePayload]:
    try:
        bundles = bundle_service.list_bundles(
            status=status_filter,
            asset_type=asset_type,
            role=role,
            theme_id=theme_id,
            audio_usage=audio_usage,
            batch_id=batch_id,
            limit=limit,
            cursor=cursor,
        )
    except ManifestReadinessError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code},
            headers={"Retry-After": "1"},
        ) from exc
    response.headers["X-Haypile-Manifest-Generation"] = bundle_service.manifest_generation
    return [BundlePayload.model_validate(bundle) for bundle in bundles]


@router.get("/{bundle_id}", response_model=BundlePayload)
def get_bundle(
    bundle_id: str,
    response: Response,
    bundle_service: BundleService = Depends(get_bundle_service),
) -> BundlePayload:
    try:
        bundle = bundle_service.get_bundle(bundle_id)
    except ManifestReadinessError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code},
            headers={"Retry-After": "1"},
        ) from exc
    if bundle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bundle not found.",
        )
    response.headers["X-Haypile-Manifest-Generation"] = bundle_service.manifest_generation
    return BundlePayload.model_validate(bundle)
