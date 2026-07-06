from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.models.bundle import BundlePayload
from app.services.bundle_service import BundleService

router = APIRouter(prefix="/bundles", tags=["bundles"])


def get_bundle_service() -> BundleService:
    return BundleService()


@router.get("", response_model=list[BundlePayload])
async def list_bundles(
    status_filter: str | None = Query(default=None, alias="status"),
    asset_type: str | None = Query(default=None, alias="type"),
    role: str | None = None,
    theme_id: str | None = None,
    bundle_service: BundleService = Depends(get_bundle_service),
) -> list[BundlePayload]:
    bundles = bundle_service.list_bundles(
        status=status_filter,
        asset_type=asset_type,
        role=role,
        theme_id=theme_id,
    )
    return [BundlePayload.model_validate(bundle) for bundle in bundles]


@router.get("/{bundle_id}", response_model=BundlePayload)
async def get_bundle(
    bundle_id: str,
    bundle_service: BundleService = Depends(get_bundle_service),
) -> BundlePayload:
    bundle = bundle_service.get_bundle(bundle_id)
    if bundle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bundle not found.",
        )
    return BundlePayload.model_validate(bundle)
