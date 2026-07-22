from __future__ import annotations

import json
import math
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.services.json_io import atomic_write_json


def provenance_path_for(asset_path: Path) -> Path:
    return asset_path.with_name(asset_path.name + ".provenance.json")


def read_asset_provenance(asset_path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(provenance_path_for(asset_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return sanitize_provenance(parsed) if isinstance(parsed, dict) else {}


def write_asset_provenance(asset_path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(provenance_path_for(asset_path), sanitize_provenance(payload))


def sanitize_provenance(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    scalar_limits = {
        "content_type": 160,
        "downloaded_at": 64,
        "source_key": 512,
        "sha256": 64,
        "audio_usage": 32,
    }
    origin_url = public_origin_url(str(payload.get("origin_url") or ""))
    if origin_url:
        cleaned["origin_url"] = origin_url
    for key, limit in scalar_limits.items():
        value = _bounded_public_string(payload.get(key), limit)
        if value:
            cleaned[key] = value
    suggestions = _sanitize_ai_suggestions(payload.get("ai_suggestions"))
    if suggestions:
        cleaned["ai_suggestions"] = suggestions
    return cleaned


def _bounded_public_string(value: Any, limit: int) -> str:
    text = str(value or "").strip().replace("\x00", "")
    if not text:
        return ""
    if "://" not in text and (
        Path(text).is_absolute() or PureWindowsPath(text).is_absolute()
    ):
        return ""
    return text[:limit]


def _sanitize_ai_suggestions(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    limits = {
        "source": 64,
        "usage": 32,
        "quality": 32,
        "quality_reason": 80,
        "agent_summary": 60,
        "reason": 80,
        "trust": 32,
    }
    for key, limit in limits.items():
        text = _bounded_public_string(value.get(key), limit)
        if text:
            cleaned[key] = text
    tags = value.get("tags")
    if isinstance(tags, list):
        cleaned["tags"] = [
            text
            for item in tags[:16]
            if (text := _bounded_public_string(item, 24))
        ][:6]
    confidence = value.get("confidence")
    if isinstance(confidence, dict):
        scores: dict[str, float] = {}
        for key in ("theme", "role"):
            try:
                score = float(confidence[key])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(score):
                scores[key] = max(0.0, min(1.0, score))
        if scores:
            cleaned["confidence"] = scores
    if isinstance(value.get("must_not_execute"), bool):
        cleaned["must_not_execute"] = value["must_not_execute"]
    if cleaned:
        cleaned["trust"] = "untrusted_advisory"
        cleaned["must_not_execute"] = True
    return cleaned


def public_origin_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return ""
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))
