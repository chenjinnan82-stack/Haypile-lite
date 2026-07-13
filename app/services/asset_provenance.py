from __future__ import annotations

import json
from pathlib import Path
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
    return parsed if isinstance(parsed, dict) else {}


def write_asset_provenance(asset_path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(provenance_path_for(asset_path), payload)


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
