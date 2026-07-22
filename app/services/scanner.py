from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from collections.abc import Callable
from typing import Any

from mutagen import File as MutagenFile, MutagenError

from app.core.config import get_settings
from app.core.file_lock import InterProcessFileLock
from app.services.json_io import atomic_write_json
from app.services.media_validator import MediaValidationError, validate_audio, validate_media
from app.services.media_types import SUPPORTED_AUDIO_EXTENSIONS
from app.services.storage_runtime import StorageRuntimeDB


class ManifestReadinessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def manifest_dirty_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(f"{manifest_path.name}.dirty")


def mark_manifest_dirty(manifest_path: Path) -> None:
    atomic_write_json(manifest_dirty_path(manifest_path), {"dirty": True})


def clear_manifest_dirty(manifest_path: Path) -> None:
    manifest_dirty_path(manifest_path).unlink(missing_ok=True)


def read_manifest_snapshot(manifest_path: Path) -> tuple[dict[str, Any], dict[str, str | int]]:
    if manifest_dirty_path(manifest_path).exists():
        raise ManifestReadinessError(
            "catalog_projection_dirty",
            "assets manifest projection is dirty",
        )
    try:
        raw = manifest_path.read_bytes()
        payload = json.loads(raw)
    except FileNotFoundError as exc:
        raise ManifestReadinessError(
            "catalog_projection_missing",
            "assets manifest not found",
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestReadinessError(
            "catalog_projection_unreadable",
            "assets manifest is unreadable",
        ) from exc
    if not isinstance(payload, dict):
        raise ManifestReadinessError(
            "catalog_projection_unreadable",
            "assets manifest must be a JSON object",
        )
    if manifest_dirty_path(manifest_path).exists():
        raise ManifestReadinessError(
            "catalog_projection_dirty",
            "assets manifest projection became dirty while reading",
        )
    readiness = {
        "manifest_generation": hashlib.sha256(raw).hexdigest(),
        "asset_count": len(payload),
    }
    return payload, readiness


def read_manifest_readiness(manifest_path: Path) -> dict[str, str | int]:
    _payload, readiness = read_manifest_snapshot(manifest_path)
    return readiness


class AssetScanner:
    IMAGE_EXTENSIONS: set[str] = {".png", ".webp", ".svg", ".jpg", ".jpeg"}
    AUDIO_EXTENSIONS: set[str] = set(SUPPORTED_AUDIO_EXTENSIONS)

    def __init__(
        self,
        assets_dir: Path | None = None,
        manifest_path: Path | None = None,
        runtime_db_path: Path | None = None,
    ) -> None:
        settings = get_settings() if assets_dir is None or manifest_path is None else None
        self.assets_dir = assets_dir or settings.ASSETS_DIR  # type: ignore[union-attr]
        self.manifest_path = manifest_path or settings.MANIFEST_PATH  # type: ignore[union-attr]
        self.runtime_db_path = runtime_db_path or (self.manifest_path.parent / "storage_runtime.db")

    async def scan_assets_directory(
        self,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, dict[str, Any]]:
        return await asyncio.to_thread(self._scan_assets_directory_sync, should_stop)

    def _scan_assets_directory_sync(
        self,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, dict[str, Any]]:
        lock_path = self.manifest_path.with_name(f"{self.manifest_path.name}.projection.lock")
        with InterProcessFileLock(lock_path):
            return self._scan_assets_directory_locked(should_stop)

    def _scan_assets_directory_locked(
        self,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, dict[str, Any]]:
        stop_requested = should_stop or (lambda: False)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        mark_manifest_dirty(self.manifest_path)

        manifest: dict[str, dict[str, Any]] = {}
        assets_root = self.assets_dir.resolve(strict=False)
        committed_paths: set[Path] | None = None
        if self.runtime_db_path.is_file():
            runtime = StorageRuntimeDB(self.runtime_db_path)
            committed_paths = runtime.committed_asset_paths(self.assets_dir, verify_hashes=True)
        for path in sorted(self.assets_dir.rglob("*")):
            if stop_requested():
                raise InterruptedError("manifest_scan_interrupted")
            if path.is_symlink() or not path.is_file():
                continue
            try:
                resolved = path.resolve(strict=False)
                resolved.relative_to(assets_root)
            except ValueError:
                continue
            if committed_paths is not None and resolved not in committed_paths:
                continue

            suffix: str = path.suffix.lower()
            if suffix in self.IMAGE_EXTENSIONS:
                image_item = self._scan_image(path)
                if image_item:
                    manifest[self._relative_key(path)] = image_item
            elif suffix in self.AUDIO_EXTENSIONS:
                audio_item = self._scan_audio(path)
                if audio_item:
                    manifest[self._relative_key(path)] = audio_item

        if stop_requested():
            raise InterruptedError("manifest_scan_interrupted")
        atomic_write_json(self.manifest_path, manifest)
        clear_manifest_dirty(self.manifest_path)
        return manifest

    def _scan_image(self, path: Path) -> dict[str, Any] | None:
        try:
            validated = validate_media(path)
        except MediaValidationError:
            return None
        width, height = int(validated.width or 0), int(validated.height or 0)
        if height == 0:
            return None

        ratio: float = width / height
        return {
            "type": "image",
            "resolution": f"{width}x{height}",
            "aspect_ratio": self._format_ratio(ratio),
            "url_path": self._to_url_path(path),
        }

    def _scan_audio(self, path: Path) -> dict[str, Any] | None:
        try:
            validated = validate_audio(path)
            audio = MutagenFile(path)
        except (MediaValidationError, MutagenError, OSError, ValueError):
            return None
        if audio is None or audio.info is None:
            return None

        duration_seconds = float(validated.duration_seconds or 0.0)
        return {
            "type": "audio",
            "duration_seconds": round(duration_seconds, 3),
            "audio_metadata": self._audio_metadata(audio),
            "audio_tags": self._audio_tags(audio),
            "url_path": self._to_url_path(path),
        }

    @staticmethod
    def _audio_metadata(audio: Any) -> dict[str, int]:
        info = audio.info
        fields = {
            "bitrate_bps": getattr(info, "bitrate", None),
            "sample_rate_hz": getattr(info, "sample_rate", None),
            "channels": getattr(info, "channels", None),
        }
        return {
            key: int(value)
            for key, value in fields.items()
            if isinstance(value, (int, float)) and value > 0
        }

    @classmethod
    def _audio_tags(cls, audio: Any) -> dict[str, str]:
        tags = getattr(audio, "tags", None)
        if tags is None or not hasattr(tags, "get"):
            return {}
        fields = {
            "title": ("title", "TITLE", "TIT2", "\u00a9nam"),
            "artist": ("artist", "ARTIST", "TPE1", "\u00a9ART"),
            "album": ("album", "ALBUM", "TALB", "\u00a9alb"),
        }
        return {
            name: text
            for name, keys in fields.items()
            if (text := cls._tag_text(tags, keys))
        }

    @staticmethod
    def _tag_text(tags: Any, keys: tuple[str, ...]) -> str:
        for key in keys:
            value = tags.get(key)
            if value is None:
                continue
            value = getattr(value, "text", value)
            if isinstance(value, (list, tuple)):
                value = next((item for item in value if str(item).strip()), "")
            text = str(value or "").strip()
            if text:
                return text[:160]
        return ""

    @staticmethod
    def _format_ratio(value: float) -> str:
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text if "." in text else f"{text}.0"

    def _relative_key(self, path: Path) -> str:
        return path.relative_to(self.assets_dir).as_posix()

    def _to_url_path(self, path: Path) -> str:
        relative_path = path.relative_to(self.assets_dir).as_posix()
        return f"/static/{relative_path}"
