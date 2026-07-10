from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BINDING_FILE_NAME = "real_project_binding.json"


class HaypileRealProjectBindingError(ValueError):
    """Raised when a Haypile real project binding is invalid."""


@dataclass(frozen=True, slots=True)
class HaypileRealProjectBinding:
    project_root: Path
    source: str


def default_real_project_binding_path() -> Path:
    from app.core.config import get_settings

    return get_settings().STORAGE_DIR / BINDING_FILE_NAME


def resolve_haypile_real_project_root(
    *,
    binding_path: Path | None = None,
) -> HaypileRealProjectBinding | None:
    env_value = os.environ.get("HAYPILE_REAL_PROJECT_ROOT", "").strip()
    if env_value:
        return HaypileRealProjectBinding(
            project_root=Path(env_value).resolve(strict=False),
            source="env",
        )

    path = binding_path or default_real_project_binding_path()
    payload = _read_json(path)
    project_root = str(payload.get("project_root") or "").strip()
    if not project_root:
        return None
    return HaypileRealProjectBinding(
        project_root=Path(project_root).resolve(strict=False),
        source="binding_file",
    )


def write_haypile_real_project_binding(
    *,
    project_root: str | Path,
    binding_path: Path | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=False)
    if not root.is_dir():
        raise HaypileRealProjectBindingError("project_root must be an existing directory")
    path = binding_path or default_real_project_binding_path()
    payload = {
        "binding_type": "haypile_real_project_binding",
        "version": "haypile_real_project_binding.v1",
        "project_root": root.as_posix(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def clear_haypile_real_project_binding(*, binding_path: Path | None = None) -> None:
    path = binding_path or default_real_project_binding_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _read_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
