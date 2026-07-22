from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.models.theme import AestheticPayload
from app.services.vault_service import VaultService
from app.core.config import get_settings
from app.services.scanner import ManifestReadinessError, read_manifest_readiness

router = APIRouter(prefix="/vault", tags=["theme"])


def get_vault_service() -> VaultService:
    return VaultService()


def _require_catalog(response: Response) -> None:
    try:
        readiness = read_manifest_readiness(get_settings().MANIFEST_PATH)
    except ManifestReadinessError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code},
            headers={"Retry-After": "1"},
        ) from exc
    response.headers["X-Haypile-Manifest-Generation"] = str(readiness["manifest_generation"])


@router.get("", response_model=list[str])
def list_themes(
    response: Response,
    vault_service: VaultService = Depends(get_vault_service),
) -> list[str]:
    _require_catalog(response)
    return vault_service.list_themes()

@router.get("/{theme_id}", response_model=AestheticPayload)
def get_theme_contract(
    theme_id: str,
    response: Response,
    vault_service: VaultService = Depends(get_vault_service),
) -> AestheticPayload:
    _require_catalog(response)
    payload = vault_service.get_theme_payload(theme_id=theme_id)
    return AestheticPayload.model_validate(payload)
