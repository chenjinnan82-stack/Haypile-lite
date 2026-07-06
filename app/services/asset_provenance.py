from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
