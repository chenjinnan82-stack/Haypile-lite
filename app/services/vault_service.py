from __future__ import annotations

import json
import re
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from app.core.config import get_settings


class VaultService:
    _SAFE_THEME_ID_PATTERN = re.compile(r"[^a-z0-9_-]+")

    def __init__(self, themes_dir: Path | None = None, fallback_theme_id: str = "generic") -> None:
        settings = get_settings()
        self.themes_dir: Path = (themes_dir or settings.THEMES_DIR).resolve()
        self.themes_dir.mkdir(parents=True, exist_ok=True)
        self.fallback_theme_id: str = self._normalize_theme_id(fallback_theme_id)

    def get_theme_payload(self, theme_id: str) -> dict[str, Any]:
        requested_theme_id = self._normalize_theme_id(theme_id)
        requested_path = self._theme_file_path(requested_theme_id)
        fallback_path = self._theme_file_path(self.fallback_theme_id)

        requested_payload = self._load_theme_file(requested_path)
        if requested_payload is not None:
            return requested_payload

        fallback_payload = self._load_theme_file(fallback_path)
        if fallback_payload is not None:
            return fallback_payload

        return self._last_resort_payload(theme_id=requested_theme_id)

    def _load_theme_file(self, file_path: Path) -> dict[str, Any] | None:
        try:
            raw_payload = self._read_json(file_path)
            return self._normalize_payload(raw_payload, default_theme_name=file_path.stem)
        except (FileNotFoundError, JSONDecodeError, ValueError, TypeError):
            return None

    def list_themes(self) -> list[str]:
        self.themes_dir.mkdir(parents=True, exist_ok=True)
        return sorted([f.stem for f in self.themes_dir.glob("*.json")])

    def _theme_file_path(self, theme_id: str) -> Path:
        candidate = (self.themes_dir / f"{theme_id}.json").resolve()
        try:
            candidate.relative_to(self.themes_dir)
        except ValueError:
            return (self.themes_dir / f"{self.fallback_theme_id}.json").resolve()
        return candidate

    def _normalize_theme_id(self, theme_id: str) -> str:
        raw = str(theme_id or "").strip().lower()
        safe = self._SAFE_THEME_ID_PATTERN.sub("_", raw)
        compact = re.sub(r"_+", "_", safe).strip("_")
        return compact or "generic"

    @staticmethod
    def _read_json(file_path: Path) -> dict[str, Any]:
        raw = file_path.read_text(encoding="utf-8")
        parsed: Any = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"Theme payload must be an object: {file_path}")
        return parsed

    @staticmethod
    def _normalize_payload(raw_payload: dict[str, Any], default_theme_name: str) -> dict[str, Any]:
        css_variables = raw_payload.get("css_variables", {})
        tailwind_extend = raw_payload.get("tailwind_extend", {})
        fonts = raw_payload.get("fonts", [])
        assets = raw_payload.get("physical_assets", {})

        normalized_assets: dict[str, dict[str, Any]] = {}
        if isinstance(assets, dict):
            for key, value in assets.items():
                if not isinstance(value, dict):
                    continue
                url = value.get("url")
                asset_type = value.get("type")
                if not isinstance(url, str) or not isinstance(asset_type, str):
                    continue
                normalized_assets[str(key)] = {
                    "url": url,
                    "type": asset_type,
                    "resolution": value.get("resolution") if isinstance(value.get("resolution"), str) else None,
                    "aspect_ratio": value.get("aspect_ratio") if isinstance(value.get("aspect_ratio"), str) else None,
                    "css_advice": value.get("css_advice") if isinstance(value.get("css_advice"), str) else "",
                    "placement_intent": value.get("placement_intent")
                    if isinstance(value.get("placement_intent"), str)
                    else "",
                }

        return {
            "theme_name": str(raw_payload.get("theme_name", default_theme_name)),
            "css_variables": css_variables if isinstance(css_variables, dict) else {},
            "tailwind_extend": tailwind_extend if isinstance(tailwind_extend, dict) else {},
            "fonts": [str(item) for item in fonts] if isinstance(fonts, list) else [],
            "physical_assets": normalized_assets,
            "ui_dev_instruction": str(raw_payload.get("ui_dev_instruction", "使用通用安全主题约束。")),
        }

    @staticmethod
    def _last_resort_payload(theme_id: str) -> dict[str, Any]:
        return {
            "theme_name": f"last_resort_{theme_id or 'generic'}",
            "css_variables": {
                "--bg-primary": "#101318",
                "--text-primary": "#E5E7EB",
                "--accent": "#00A0E9",
            },
            "tailwind_extend": {},
            "fonts": [],
            "physical_assets": {},
            "ui_dev_instruction": "兜底主题：请使用安全默认样式，优先保证可读性与布局稳定。",
        }
