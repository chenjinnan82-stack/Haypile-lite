from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.services.asset_provenance import sanitize_provenance


def build_asset_handoff(
    bundles: list[dict[str, Any]],
    *,
    base_url: str,
    batch_id: str | None = None,
    manifest_generation: str = "",
    total_matching: int | None = None,
    complete: bool = True,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    payload = {
        "handoff_version": "haypile.asset-handoff.v1",
        "handoff_id": str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "haypile",
        "base_url": base_url,
        "manifest_generation": manifest_generation,
        "asset_count": len(bundles),
        "total_matching": len(bundles) if total_matching is None else int(total_matching),
        "complete": bool(complete),
        "next_cursor": next_cursor,
        "assets": [build_handoff_asset(bundle, base_url=base_url) for bundle in bundles],
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


def build_handoff_asset(bundle: dict[str, Any], *, base_url: str) -> dict[str, Any]:
    resolved_url = base_url + str(bundle["url"])
    public_metadata = sanitize_provenance(
        {
            "origin_url": bundle.get("origin_url", ""),
            "content_type": bundle.get("content_type", ""),
            "downloaded_at": bundle.get("downloaded_at", ""),
            "ai_suggestions": bundle.get("ai_suggestions", {}),
        }
    )
    return {
        "id": bundle["id"],
        "theme_id": bundle["theme_id"],
        "type": bundle["type"],
        "role": bundle["role"],
        "status": bundle["status"],
        "sha256": bundle["sha256"],
        "source_key": bundle["source_key"],
        "url": bundle["url"],
        "access": bundle["access"],
        "resolved_url": resolved_url,
        "content_type": public_metadata.get("content_type", ""),
        "ai_suggestions": public_metadata.get("ai_suggestions", {}),
        "duration_seconds": bundle.get("duration_seconds"),
        "frame_count": bundle.get("frame_count"),
        "loop_count": bundle.get("loop_count"),
        "audio_metadata": bundle.get("audio_metadata", {}),
        "audio_tags": bundle.get("audio_tags", {}),
        "audio_usage": bundle.get("audio_usage", "unknown"),
        "provenance": {
            "source": "haypile",
            "id": bundle["id"],
            "sha256": bundle["sha256"],
            "source_key": bundle["source_key"],
            "url": bundle["url"],
            "resolved_url": resolved_url,
            "access": bundle["access"],
            "origin_url": public_metadata.get("origin_url", ""),
            "content_type": public_metadata.get("content_type", ""),
            "downloaded_at": public_metadata.get("downloaded_at", ""),
        },
    }
