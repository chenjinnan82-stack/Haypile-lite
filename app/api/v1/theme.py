from __future__ import annotations

from fastapi import APIRouter, Depends

from app.models.theme import AestheticPayload
from app.services.vault_service import VaultService

router = APIRouter(prefix="/vault", tags=["theme"])


def get_vault_service() -> VaultService:
    return VaultService()


@router.get("", response_model=list[str])
def list_themes(
    vault_service: VaultService = Depends(get_vault_service),
) -> list[str]:
    return vault_service.list_themes()

@router.get("/{theme_id}", response_model=AestheticPayload)
def get_theme_contract(
    theme_id: str,
    vault_service: VaultService = Depends(get_vault_service),
) -> AestheticPayload:
    payload = vault_service.get_theme_payload(theme_id=theme_id)
    return AestheticPayload.model_validate(payload)
