from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.asset_provenance import public_origin_url, read_asset_provenance, write_asset_provenance
from app.services.json_io import atomic_write_json
from app.services.storage_runtime import StorageRuntimeDB


ALLOWED_IMAGE_ROLES = {
    "main_background",
    "hero_image",
    "logo",
    "icon",
    "content_image",
    "texture",
    "unknown",
}
ALLOWED_BUNDLE_ROLES = {*ALLOWED_IMAGE_ROLES, "audio"}
ALLOWED_AUDIO_USAGES = {"music", "voice", "ambience", "sound_effect", "loop", "unknown"}
MAX_BUNDLE_PAGE_SIZE = 100


class BundleService:
    def __init__(
        self,
        *,
        assets_dir: Path | None = None,
        manifest_path: Path | None = None,
        themes_dir: Path | None = None,
        runtime_db_path: Path | None = None,
    ) -> None:
        settings = get_settings()
        self.assets_dir = assets_dir or settings.ASSETS_DIR
        self.manifest_path = manifest_path or settings.MANIFEST_PATH
        self.themes_dir = themes_dir or settings.THEMES_DIR
        self.runtime_db_path = runtime_db_path or (settings.INDEX_DIR / "storage_runtime.db")

    def list_bundles(
        self,
        *,
        status: str | None = None,
        asset_type: str | None = None,
        role: str | None = None,
        theme_id: str | None = None,
        audio_usage: str | None = None,
        batch_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        manifest = self._read_json(self.manifest_path)
        theme_assets = self._theme_assets_by_url()
        sha_by_path = self._sha_by_dst_path()
        bundles: list[dict[str, str]] = []

        for source_key, item in sorted(manifest.items()):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url_path") or "").strip()
            item_type = str(item.get("type") or "").strip().lower()
            if not url or not item_type:
                continue

            theme_asset = theme_assets.get(url, {})
            item_role = self._role_from(source_key=source_key, theme_asset=theme_asset)
            source_path = self._asset_path(source_key)
            if source_path is None:
                continue
            sha256 = sha_by_path.get(str(source_path.resolve(strict=False))) or self._sha256(source_path)
            provenance = read_asset_provenance(source_path)
            ai_suggestions = provenance.get("ai_suggestions")
            bundle_audio_usage = self._audio_usage_from(provenance) if item_type == "audio" else "unknown"
            bundle_id = Path(source_key).stem
            bundles.append(
                {
                    "id": bundle_id,
                    "theme_id": self._theme_from_key(source_key),
                    "type": item_type,
                    "role": item_role,
                    "status": "pending" if item_role == "unknown" or (item_type == "audio" and bundle_audio_usage == "unknown") else "ready",
                    "sha256": sha256,
                    "url": url,
                    "access": "manifest_static",
                    "source_key": source_key,
                    "origin_url": public_origin_url(str(provenance.get("origin_url") or "")),
                    "content_type": str(provenance.get("content_type") or ""),
                    "downloaded_at": str(provenance.get("downloaded_at") or ""),
                    "ai_suggestions": ai_suggestions if isinstance(ai_suggestions, dict) else {},
                    "duration_seconds": self._duration_from(item),
                    "audio_metadata": self._audio_metadata_from(item),
                    "audio_tags": self._audio_tags_from(item),
                    "audio_usage": bundle_audio_usage,
                }
            )

        known_urls = {bundle["url"] for bundle in bundles}
        for url, asset in sorted(theme_assets.items()):
            if url in known_urls:
                continue
            source_key = url.removeprefix("/static/").lstrip("/")
            item_role = self._role_from(source_key=source_key, theme_asset=asset)
            asset_path = self._asset_path(source_key)
            provenance = read_asset_provenance(asset_path) if asset_path is not None else {}
            ai_suggestions = provenance.get("ai_suggestions")
            item_type = str(asset.get("type") or "asset")
            bundles.append(
                {
                    "id": Path(source_key).stem or self._safe_id(url),
                    "theme_id": str(asset.get("theme_id") or self._theme_from_key(source_key)),
                    "type": item_type,
                    "role": item_role,
                    "status": "missing",
                    "sha256": "",
                    "url": url,
                    "access": "manifest_static",
                    "source_key": source_key,
                    "origin_url": public_origin_url(str(provenance.get("origin_url") or "")),
                    "content_type": str(provenance.get("content_type") or ""),
                    "downloaded_at": str(provenance.get("downloaded_at") or ""),
                    "ai_suggestions": ai_suggestions if isinstance(ai_suggestions, dict) else {},
                    "duration_seconds": None,
                    "audio_metadata": {},
                    "audio_tags": {},
                    "audio_usage": self._audio_usage_from(provenance) if item_type == "audio" else "unknown",
                }
            )

        resolved_batch_id = StorageRuntimeDB(self.runtime_db_path).resolve_batch_id(batch_id)
        batch_order: dict[str, int] = {}
        if batch_id is not None:
            if not resolved_batch_id:
                return []
            batch_order = {
                sha256: index
                for index, sha256 in enumerate(
                    StorageRuntimeDB(self.runtime_db_path).batch_hashes(resolved_batch_id)
                )
            }
            bundles = [bundle for bundle in bundles if bundle["sha256"] in batch_order]

        filters = {
            "status": status,
            "type": asset_type,
            "role": role,
            "theme_id": theme_id,
            "audio_usage": audio_usage,
        }
        filtered = [
            bundle
            for bundle in bundles
            if all(value is None or bundle[key] == value for key, value in filters.items())
        ]
        ordered = sorted(
            filtered,
            key=(
                (lambda bundle: (batch_order.get(bundle["sha256"], len(batch_order)), bundle["id"]))
                if batch_order
                else (lambda bundle: (bundle["source_key"], bundle["id"]))
            ),
        )
        if cursor:
            if batch_order:
                cursor_index = next(
                    (
                        index
                        for index, bundle in enumerate(ordered)
                        if bundle["source_key"] == cursor
                    ),
                    -1,
                )
                ordered = ordered[cursor_index + 1 :] if cursor_index >= 0 else []
            else:
                # ponytail: source_key is the stable cursor for the default lexical order.
                ordered = [bundle for bundle in ordered if bundle["source_key"] > cursor]
        if limit is None:
            return ordered
        return ordered[: max(1, min(int(limit), MAX_BUNDLE_PAGE_SIZE))]

    def get_latest_batch(self) -> dict[str, object] | None:
        return StorageRuntimeDB(self.runtime_db_path).latest_batch()

    def get_bundle(self, bundle_id: str) -> dict[str, str] | None:
        wanted = str(bundle_id or "").strip()
        if not wanted:
            return None
        for bundle in self.list_bundles():
            if bundle["id"] == wanted:
                return bundle
        return None

    def set_bundle_role(self, bundle_id: str, role: str) -> dict[str, str] | None:
        normalized_role = str(role or "").strip().lower()
        if normalized_role not in ALLOWED_BUNDLE_ROLES:
            raise ValueError("unsupported bundle role")
        bundle = self.get_bundle(bundle_id)
        if bundle is None or bundle["status"] == "missing":
            return None

        theme_id = bundle["theme_id"] or self._theme_from_key(bundle["source_key"]) or "generic"
        theme_file = self.themes_dir / f"{theme_id}.json"
        payload = self._read_json(theme_file)
        if not payload:
            payload = {
                "theme_name": theme_id,
                "css_variables": {},
                "tailwind_extend": {},
                "fonts": [],
                "physical_assets": {},
                "ui_dev_instruction": "Use these theme assets for consistent visual rendering. Do not fabricate image URLs.",
            }
        physical_assets = payload.get("physical_assets")
        if not isinstance(physical_assets, dict):
            physical_assets = {}
            payload["physical_assets"] = physical_assets

        asset_key = ""
        for key, value in physical_assets.items():
            if isinstance(value, dict) and str(value.get("url") or "") == bundle["url"]:
                asset_key = str(key)
                break
        if not asset_key:
            asset_key = Path(bundle["source_key"]).stem or bundle["id"]

        asset = physical_assets.get(asset_key)
        asset_dict = asset if isinstance(asset, dict) else {}
        asset_dict.update(
            {
                "url": bundle["url"],
                "type": self._asset_contract_type(role=normalized_role, asset_type=bundle["type"]),
                "role": normalized_role,
                "css_advice": self._default_css_advice(normalized_role),
                "placement_intent": self._default_placement_intent(normalized_role),
            }
        )
        physical_assets[asset_key] = asset_dict
        atomic_write_json(theme_file, payload)
        return self.get_bundle(bundle_id)

    def set_bundle_audio_usage(self, bundle_id: str, audio_usage: str) -> dict[str, Any] | None:
        normalized_usage = str(audio_usage or "").strip().lower()
        if normalized_usage not in ALLOWED_AUDIO_USAGES:
            raise ValueError("unsupported audio usage")
        bundle = self.get_bundle(bundle_id)
        if bundle is None or bundle["status"] == "missing" or bundle["type"] != "audio":
            return None

        if self.set_bundle_role(bundle_id, "audio") is None:
            return None
        asset_path = self._asset_path(bundle["source_key"])
        if asset_path is None:
            return None
        provenance = read_asset_provenance(asset_path)
        provenance["audio_usage"] = normalized_usage
        try:
            write_asset_provenance(asset_path, provenance)
        except OSError:
            return None
        return self.get_bundle(bundle_id)

    def _theme_assets_by_url(self) -> dict[str, dict[str, Any]]:
        assets: dict[str, dict[str, Any]] = {}
        for theme_file in sorted(self.themes_dir.glob("*.json")):
            payload = self._read_json(theme_file)
            physical_assets = payload.get("physical_assets")
            if not isinstance(physical_assets, dict):
                continue
            theme_id = str(payload.get("theme_name") or theme_file.stem).strip()
            for source_key, value in physical_assets.items():
                if not isinstance(value, dict):
                    continue
                url = str(value.get("url") or "").strip()
                if not url:
                    continue
                assets[url] = {**value, "source_key": str(source_key), "theme_id": theme_id}
        return assets

    @staticmethod
    def _duration_from(item: dict[str, Any]) -> float | None:
        value = item.get("duration_seconds")
        try:
            duration = float(value)
        except (TypeError, ValueError):
            return None
        return duration if duration >= 0 else None

    @staticmethod
    def _audio_metadata_from(item: dict[str, Any]) -> dict[str, int]:
        value = item.get("audio_metadata")
        if not isinstance(value, dict):
            return {}
        return {
            str(key): int(metadata_value)
            for key, metadata_value in value.items()
            if isinstance(metadata_value, (int, float)) and metadata_value > 0
        }

    @staticmethod
    def _audio_tags_from(item: dict[str, Any]) -> dict[str, str]:
        value = item.get("audio_tags")
        if not isinstance(value, dict):
            return {}
        return {
            key: text[:160]
            for key in ("title", "artist", "album")
            if (text := str(value.get(key) or "").strip())
        }

    @staticmethod
    def _audio_usage_from(provenance: dict[str, Any]) -> str:
        value = str(provenance.get("audio_usage") or "").strip().lower()
        return value if value in ALLOWED_AUDIO_USAGES else "unknown"

    def _sha_by_dst_path(self) -> dict[str, str]:
        return {
            str(path): sha256_hex
            for sha256_hex, path in StorageRuntimeDB.read_asset_hash_index(
                self.runtime_db_path,
                self.assets_dir,
            ).items()
        }

    def _asset_path(self, source_key: str) -> Path | None:
        root = self.assets_dir.resolve(strict=False)
        candidate = self.assets_dir / Path(str(source_key).replace("\\", "/"))
        if candidate.is_symlink():
            return None
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            return None
        return resolved

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _theme_from_key(source_key: str) -> str:
        parts = Path(source_key).parts
        return parts[0] if parts else ""

    @staticmethod
    def _role_from(*, source_key: str, theme_asset: dict[str, Any]) -> str:
        for candidate in (theme_asset.get("role"), theme_asset.get("source_key"), source_key):
            text = str(candidate or "").strip().lower()
            for role in (
                "main_background",
                "hero_image",
                "content_image",
                "logo",
                "icon",
                "texture",
                "audio",
                "unknown",
            ):
                if role in text:
                    return role
        return "unknown"

    @staticmethod
    def _asset_contract_type(*, role: str, asset_type: str) -> str:
        if role == "main_background":
            return "background"
        if role == "audio" or str(asset_type or "").lower() == "audio":
            return "audio"
        return "image"

    @staticmethod
    def _default_css_advice(role: str) -> str:
        return {
            "main_background": "bg-cover bg-center bg-fixed",
            "hero_image": "object-cover object-center",
            "content_image": "w-full h-auto object-cover",
            "logo": "max-w-full h-auto object-contain",
            "icon": "w-6 h-6 object-contain",
            "texture": "bg-repeat opacity-80",
            "audio": "audio",
        }.get(role, "object-contain")

    @staticmethod
    def _default_placement_intent(role: str) -> str:
        return {
            "main_background": "Use as the full-screen base background image.",
            "hero_image": "Use as the primary hero visual.",
            "content_image": "Use as responsive section or article media.",
            "logo": "Use as a brand mark without cropping.",
            "icon": "Use as a functional icon or status marker.",
            "texture": "Use as a repeated or layered page texture.",
            "audio": "Use as an audio asset.",
        }.get(role, "Use as a general visual asset.")

    @staticmethod
    def _sha256(path: Path) -> str:
        try:
            with path.open("rb") as source:
                digest = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                return digest.hexdigest()
        except OSError:
            return ""

    @staticmethod
    def _safe_id(text: str) -> str:
        return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in text).strip("_") or "bundle"
