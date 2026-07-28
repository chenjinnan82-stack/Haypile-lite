from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time

from app.core.config import Settings
from app.services.ai_provider import SystemCredentialStore, api_authority


@dataclass(frozen=True, slots=True)
class DesktopStartupResult:
    storage_ready: bool
    session_api_key: str = ""
    error_code: str | None = None


def read_desktop_gui_state(settings: Settings) -> dict[str, object]:
    path = settings.INDEX_DIR / "gui_state.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def prepare_desktop_runtime(
    settings: Settings,
    gui_state: dict[str, object],
) -> DesktopStartupResult:
    try:
        for path in (
            settings.ASSETS_DIR,
            settings.THEMES_DIR,
            settings.INDEX_DIR,
            settings.MANIFEST_PATH.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return DesktopStartupResult(
            storage_ready=False,
            error_code="storage_unavailable",
        )

    _cleanup_stale_browser_downloads(settings.STORAGE_DIR)
    return DesktopStartupResult(
        storage_ready=True,
        session_api_key=_load_session_api_key(gui_state),
    )


def _load_session_api_key(gui_state: dict[str, object]) -> str:
    if (
        str(gui_state.get("ai_provider") or "").strip().lower() != "api"
        or gui_state.get("ai_api_key_present") is not True
    ):
        return ""
    base_url = str(gui_state.get("ai_api_base_url") or "").strip()
    authorized_host = str(gui_state.get("ai_api_authorized_host") or "").strip()
    try:
        current_host = api_authority(base_url)
    except ValueError:
        return ""
    if not current_host or current_host != authorized_host:
        return ""
    try:
        return SystemCredentialStore.get(current_host)
    except Exception:
        return ""


def _cleanup_stale_browser_downloads(
    storage_dir: Path,
    *,
    now: float | None = None,
) -> None:
    incoming_dir = storage_dir / "incoming" / "browser"
    try:
        candidates = list(incoming_dir.iterdir())
    except OSError:
        return
    cutoff = (time.time() if now is None else now) - 24 * 60 * 60
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue
