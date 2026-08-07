from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


_BUILD_COMMIT = re.compile(r"[0-9a-fA-F]{7,64}")
_VERSION_TOKEN = re.compile(r"[0-9][0-9A-Za-z.+_-]{0,63}")


def read_build_commit(base_dir: Path) -> str:
    base = Path(base_dir)
    candidates = [base / "BUILD_INFO.json"]
    if base.name == "MacOS" and base.parent.name == "Contents":
        candidates.append(base.parent / "Resources" / "BUILD_INFO.json")
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            continue
        commit = str(payload.get("commit") or "") if isinstance(payload, dict) else ""
        if _BUILD_COMMIT.fullmatch(commit):
            return commit.lower()
    return "unavailable"


def build_desktop_diagnostic_summary(
    *,
    app_version: str,
    build_commit: str,
    runtime: str,
    platform_name: str,
    python_version: str,
    qt_version: str,
    language: str,
    low_power_enabled: bool,
    storage_ready: bool,
    backend_phase: str,
    manifest_state: str,
    bundles: list[dict[str, Any]],
    ai_provider: str,
    ai_enabled: bool,
) -> dict[str, object]:
    statuses = [
        str(bundle.get("status") or "").strip().lower()
        for bundle in bundles
        if isinstance(bundle, dict)
    ]
    return {
        "diagnostic_version": "haypile.desktop-diagnostics.v1",
        "app_version": _safe_version(app_version),
        "build_commit": (
            build_commit.lower()
            if _BUILD_COMMIT.fullmatch(str(build_commit or ""))
            else "unavailable"
        ),
        "runtime": _choice(runtime, {"source", "packaged"}),
        "platform": _choice(platform_name, {"darwin", "linux", "win32"}),
        "python_version": _safe_version(python_version),
        "qt_version": _safe_version(qt_version),
        "language": _choice(language, {"auto", "zh", "en"}),
        "low_power_enabled": bool(low_power_enabled),
        "storage_ready": bool(storage_ready),
        "backend_phase": _choice(
            backend_phase,
            {
                "idle",
                "starting",
                "ready",
                "conflict",
                "disabled",
                "failed",
                "stopping",
                "terminating",
                "killing",
            },
        ),
        "manifest_state": _choice(
            manifest_state,
            {"ready", "dirty", "missing", "unreadable", "unavailable"},
        ),
        "asset_counts": {
            "total": len(bundles),
            "ready": statuses.count("ready"),
            "pending": statuses.count("pending"),
            "missing": statuses.count("missing"),
        },
        "ai": {
            "provider": _choice(ai_provider, {"local", "api", "off"}),
            "enabled": bool(ai_enabled),
        },
    }


def _choice(value: object, allowed: set[str]) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else "unknown"


def _safe_version(value: object) -> str:
    text = str(value or "").strip()
    return text if _VERSION_TOKEN.fullmatch(text) else "unknown"
