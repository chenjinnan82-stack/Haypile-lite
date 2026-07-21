from __future__ import annotations

import json
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
    blocked_keys = {
        "api_key",
        "authorization",
        "credential",
        "image_bytes",
        "local_path",
        "request_body",
        "source_path",
        "temp_file",
    }

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): cleaned
                for key, item in value.items()
                if str(key).strip().lower() not in blocked_keys
                and (cleaned := clean(item)) is not None
            }
        if isinstance(value, list):
            return [cleaned for item in value if (cleaned := clean(item)) is not None]
        if isinstance(value, str):
            text = value.strip()
            if text and "://" not in text and (
                Path(text).is_absolute() or PureWindowsPath(text).is_absolute()
            ):
                return None
        return value

    cleaned = clean(payload)
    return cleaned if isinstance(cleaned, dict) else {}


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
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))
