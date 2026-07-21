from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import get_settings
from app.core.file_lock import InterProcessFileLock
from app.services.json_io import atomic_write_json


DEFAULT_UI_DEV_INSTRUCTION = (
    "Use these theme assets for consistent visual rendering. Do not fabricate image URLs."
)
THEME_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ThemeAssetUpsertResult:
    theme_id: str
    theme_file: Path
    asset_key: str
    created_theme_file: bool
    updated: bool


class ThemeRegistry:
    """
    Theme contract registry with deterministic upsert behavior.

    Responsibilities:
    - Ensure a per-theme JSON contract exists under storage/themes/{theme_id}.json
    - Upsert physical_assets entries for newly ingested assets
    - Preserve unknown fields from existing contracts (forward compatibility)
    """

    _SAFE_ID_PATTERN = re.compile(r"[^a-z0-9_-]+")

    def __init__(self, themes_dir: Path | None = None) -> None:
        self.themes_dir = themes_dir or get_settings().THEMES_DIR
        self.themes_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir = self.themes_dir.parent / "quarantine" / "themes"
        self.lock_dir = self.themes_dir.parent / "index" / "theme_locks"
        self.last_recovery: dict[str, str] | None = None

    def list_theme_ids(self) -> list[str]:
        self.themes_dir.mkdir(parents=True, exist_ok=True)
        return sorted(
            [
                p.stem
                for p in self.themes_dir.glob("*.json")
                if p.is_file() and p.stem.strip()
            ]
        )

    def ensure_theme_contract(self, theme_id: str) -> tuple[dict[str, Any], Path, bool]:
        normalized_theme_id = self._normalize_theme_id(theme_id)
        theme_file = self.themes_dir / f"{normalized_theme_id}.json"
        with self._theme_lock(normalized_theme_id):
            return self._ensure_theme_contract_unlocked(normalized_theme_id, theme_file)

    def _ensure_theme_contract_unlocked(
        self,
        normalized_theme_id: str,
        theme_file: Path,
    ) -> tuple[dict[str, Any], Path, bool]:

        if not theme_file.exists():
            payload = self._new_theme_payload(normalized_theme_id)
            payload = self._write_json(theme_file, payload)
            return payload, theme_file, True

        payload = self._read_json(theme_file)
        if payload is None:
            quarantined = self._quarantine_theme(theme_file)
            payload = self._new_theme_payload(normalized_theme_id)
            payload = self._write_json(theme_file, payload)
            self.last_recovery = {
                "theme_id": normalized_theme_id,
                "quarantined_path": quarantined.as_posix(),
            }
            logger.warning("Quarantined unreadable theme contract: theme_id=%s", normalized_theme_id)
            return payload, theme_file, True

        normalized_payload = self._normalize_theme_payload(payload, normalized_theme_id)
        if normalized_payload != payload:
            normalized_payload = self._write_json(theme_file, normalized_payload)
        return normalized_payload, theme_file, False

    def upsert_image_asset(
        self,
        *,
        theme_id: str,
        asset_key: str,
        asset_url: str,
        role: str = "unknown",
        css_advice: str | None = None,
        placement_intent: str | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> ThemeAssetUpsertResult:
        normalized_theme_id = self._normalize_theme_id(theme_id)
        theme_file = self.themes_dir / f"{normalized_theme_id}.json"
        with self._theme_lock(normalized_theme_id):
            payload, theme_file, created_theme_file = self._ensure_theme_contract_unlocked(
                normalized_theme_id,
                theme_file,
            )

            physical_assets = payload.get("physical_assets")
            if not isinstance(physical_assets, dict):
                physical_assets = {}
                payload["physical_assets"] = physical_assets

            normalized_key = self._normalize_asset_key(asset_key, role=role)
            existing_asset = physical_assets.get(normalized_key)
            existing_asset_dict = existing_asset if isinstance(existing_asset, dict) else {}

            merged_asset = {
                **existing_asset_dict,
                "url": asset_url.strip(),
                "type": self._infer_visual_type(role),
                "role": self._normalize_asset_key(role),
                "css_advice": (
                    css_advice.strip()
                    if isinstance(css_advice, str) and css_advice.strip()
                    else self._default_css_advice(role)
                ),
                "placement_intent": (
                    placement_intent.strip()
                    if isinstance(placement_intent, str) and placement_intent.strip()
                    else self._default_placement_intent(role)
                ),
            }

            if extra_fields:
                for key, value in extra_fields.items():
                    if not isinstance(key, str) or not key.strip():
                        continue
                    merged_asset[key.strip()] = value

            updated = existing_asset_dict != merged_asset
            physical_assets[normalized_key] = merged_asset

            if updated:
                self._write_json(theme_file, payload)

        return ThemeAssetUpsertResult(
            theme_id=normalized_theme_id,
            theme_file=theme_file,
            asset_key=normalized_key,
            created_theme_file=created_theme_file,
            updated=updated,
        )

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _write_json(self, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        written = dict(payload)
        written["schema_version"] = THEME_SCHEMA_VERSION
        written["revision"] = max(0, int(written.get("revision") or 0)) + 1
        atomic_write_json(path, written)
        return written

    def _new_theme_payload(self, theme_id: str) -> dict[str, Any]:
        return {
            "schema_version": THEME_SCHEMA_VERSION,
            "revision": 0,
            "theme_name": theme_id,
            "css_variables": {},
            "tailwind_extend": {},
            "fonts": [],
            "physical_assets": {},
            "ui_dev_instruction": DEFAULT_UI_DEV_INSTRUCTION,
        }

    def _normalize_theme_payload(
        self, payload: dict[str, Any], theme_id: str
    ) -> dict[str, Any]:
        normalized = dict(payload)
        source_schema_version = payload.get("schema_version")
        legacy_role_migration = (
            not isinstance(source_schema_version, int)
            or source_schema_version < THEME_SCHEMA_VERSION
        )

        normalized["schema_version"] = THEME_SCHEMA_VERSION
        try:
            normalized["revision"] = max(0, int(normalized.get("revision") or 0))
        except (TypeError, ValueError):
            normalized["revision"] = 0

        normalized["theme_name"] = (
            str(normalized.get("theme_name", theme_id) or theme_id).strip() or theme_id
        )

        css_variables = normalized.get("css_variables")
        normalized["css_variables"] = (
            css_variables if isinstance(css_variables, dict) else {}
        )

        tailwind_extend = normalized.get("tailwind_extend")
        normalized["tailwind_extend"] = (
            tailwind_extend if isinstance(tailwind_extend, dict) else {}
        )

        fonts = normalized.get("fonts")
        normalized["fonts"] = (
            [str(item) for item in fonts] if isinstance(fonts, list) else []
        )

        physical_assets = normalized.get("physical_assets")
        if not isinstance(physical_assets, dict):
            normalized["physical_assets"] = {}
        else:
            cleaned_assets: dict[str, dict[str, Any]] = {}
            for key, value in physical_assets.items():
                if not isinstance(key, str) or not key.strip():
                    continue
                if not isinstance(value, dict):
                    continue
                normalized_key = self._normalize_asset_key(key)
                if normalized_key in cleaned_assets:
                    raise ValueError(f"theme_asset_key_collision:{normalized_key}")
                cleaned = dict(value)
                explicit_role = str(cleaned.get("role") or "").strip().lower()
                if explicit_role not in {
                    "main_background",
                    "hero_image",
                    "logo",
                    "icon",
                    "content_image",
                    "texture",
                    "audio",
                    "unknown",
                }:
                    explicit_role = (
                        self._legacy_role(normalized_key, str(cleaned.get("url") or ""))
                        if legacy_role_migration
                        else "unknown"
                    )
                cleaned["role"] = explicit_role
                cleaned_assets[normalized_key] = cleaned
            normalized["physical_assets"] = cleaned_assets

        ui_instruction = normalized.get("ui_dev_instruction")
        if not isinstance(ui_instruction, str) or not ui_instruction.strip():
            normalized["ui_dev_instruction"] = DEFAULT_UI_DEV_INSTRUCTION

        return normalized

    def _quarantine_theme(self, path: Path) -> Path:
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.quarantine_dir / f"{path.stem}-{timestamp}-{uuid4().hex[:8]}.json"
        os.replace(path, destination)
        return destination

    def _theme_lock(self, theme_id: str) -> InterProcessFileLock:
        return InterProcessFileLock(self.lock_dir / f"{theme_id}.lock")

    def _normalize_theme_id(self, theme_id: str) -> str:
        raw = str(theme_id or "").strip().lower()
        safe = self._SAFE_ID_PATTERN.sub("_", raw)
        compact = re.sub(r"_+", "_", safe).strip("_")
        return compact or "generic"

    def _normalize_asset_key(self, asset_key: str, role: str | None = None) -> str:
        candidate = str(asset_key or "").strip().lower()
        if not candidate and role:
            candidate = str(role).strip().lower()
        if not candidate:
            candidate = "unknown"

        safe = self._SAFE_ID_PATTERN.sub("_", candidate)
        compact = re.sub(r"_+", "_", safe).strip("_")
        return compact or "unknown"

    @staticmethod
    def _legacy_role(asset_key: str, asset_url: str) -> str:
        allowed = (
            "main_background",
            "hero_image",
            "logo",
            "icon",
            "content_image",
            "texture",
            "audio",
        )
        candidates = (asset_key, Path(asset_url.removeprefix("/static/")).stem.lower())
        for candidate in candidates:
            if candidate in allowed:
                return candidate
            for role in allowed:
                if re.search(rf"(?:^|_)(?:img_|aud_)?{re.escape(role)}(?:_|$)", candidate):
                    return role
        return "unknown"

    @staticmethod
    def _infer_visual_type(role: str) -> str:
        normalized_role = str(role or "").strip().lower()
        if normalized_role == "main_background":
            return "background"
        if normalized_role in {"hero_image", "icon", "texture"}:
            return "image"
        if normalized_role == "audio":
            return "audio"
        return "image"

    @staticmethod
    def _default_css_advice(role: str) -> str:
        normalized_role = str(role or "").strip().lower()
        mapping = {
            "main_background": "bg-cover bg-center bg-fixed",
            "hero_image": "object-cover object-center",
            "icon": "w-6 h-6 object-contain",
            "texture": "bg-repeat opacity-80",
        }
        return mapping.get(normalized_role, "object-contain")

    @staticmethod
    def _default_placement_intent(role: str) -> str:
        normalized_role = str(role or "").strip().lower()
        mapping = {
            "main_background": "Use as the full-screen base background image.",
            "hero_image": "Use as the primary hero visual.",
            "icon": "Use as a functional icon or status marker.",
            "texture": "Use as a repeated or layered page texture.",
        }
        return mapping.get(normalized_role, "Use as a general visual asset.")
