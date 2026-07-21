from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.models.bundle import IngestBatchPayload
from app.services.bundle_service import BundleService

router = APIRouter(prefix="/batches", tags=["batches"])


def get_bundle_service() -> BundleService:
    return BundleService()


@router.get("/latest", response_model=IngestBatchPayload)
async def get_latest_batch(
    bundle_service: BundleService = Depends(get_bundle_service),
) -> IngestBatchPayload:
    batch = bundle_service.get_latest_batch()
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No completed ingest batch.")
    return IngestBatchPayload.model_validate(batch)
