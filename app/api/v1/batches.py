from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.models.bundle import IngestBatchPayload
from app.services.bundle_service import BundleService
from app.services.scanner import ManifestReadinessError, read_manifest_snapshot

router = APIRouter(prefix="/batches", tags=["batches"])


def get_bundle_service() -> BundleService:
    return BundleService()


@router.get("/latest", response_model=IngestBatchPayload)
def get_latest_batch(
    response: Response,
    bundle_service: BundleService = Depends(get_bundle_service),
) -> IngestBatchPayload:
    try:
        _manifest, readiness = read_manifest_snapshot(bundle_service.manifest_path)
    except ManifestReadinessError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code},
            headers={"Retry-After": "1"},
        ) from exc
    batch = bundle_service.get_latest_batch()
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No completed ingest batch.")
    response.headers["X-Haypile-Manifest-Generation"] = str(readiness["manifest_generation"])
    return IngestBatchPayload.model_validate(batch)
