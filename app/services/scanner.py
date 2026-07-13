from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from mutagen import File as MutagenFile, MutagenError
from PIL import Image, UnidentifiedImageError

from app.core.config import get_settings
from app.services.json_io import atomic_write_json
from app.services.media_types import SUPPORTED_AUDIO_EXTENSIONS


class AssetScanner:
    IMAGE_EXTENSIONS: set[str] = {".png", ".webp", ".svg", ".jpg", ".jpeg"}
    AUDIO_EXTENSIONS: set[str] = set(SUPPORTED_AUDIO_EXTENSIONS)

    def __init__(self, assets_dir: Path | None = None, manifest_path: Path | None = None) -> None:
        settings = get_settings()
        self.assets_dir: Path = assets_dir or settings.ASSETS_DIR
        self.manifest_path: Path = manifest_path or settings.MANIFEST_PATH

    async def scan_assets_directory(self) -> dict[str, dict[str, Any]]:
        return await asyncio.to_thread(self._scan_assets_directory_sync)

    def _scan_assets_directory_sync(self) -> dict[str, dict[str, Any]]:
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)

        manifest: dict[str, dict[str, Any]] = {}
        assets_root = self.assets_dir.resolve(strict=False)
        for path in sorted(self.assets_dir.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                path.resolve(strict=False).relative_to(assets_root)
            except ValueError:
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

        atomic_write_json(self.manifest_path, manifest)
        return manifest

    def _scan_image(self, path: Path) -> dict[str, Any] | None:
        try:
            width, height = self._read_image_size(path)
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ):
            return None

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
            audio = MutagenFile(path)
        except (MutagenError, OSError, ValueError):
            return None
        if audio is None or audio.info is None:
            return None

        duration_seconds: float = float(getattr(audio.info, "length", 0.0) or 0.0)
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

    def _read_image_size(self, path: Path) -> tuple[int, int]:
        if path.suffix.lower() == ".svg":
            return self._read_svg_size(path)
        with Image.open(path) as image:
            width, height = image.size
        return int(width), int(height)

    def _read_svg_size(self, path: Path) -> tuple[int, int]:
        tree = ElementTree.parse(path)
        root = tree.getroot()

        width_raw: str | None = root.attrib.get("width")
        height_raw: str | None = root.attrib.get("height")

        width: int | None = self._parse_numeric_dimension(width_raw)
        height: int | None = self._parse_numeric_dimension(height_raw)

        if width is not None and height is not None:
            return width, height

        viewbox: str | None = root.attrib.get("viewBox")
        if viewbox:
            parts = [part for part in viewbox.replace(",", " ").split() if part]
            if len(parts) == 4:
                viewbox_width = float(parts[2])
                viewbox_height = float(parts[3])
                return int(viewbox_width), int(viewbox_height)

        raise ValueError(f"Cannot infer SVG dimensions: {path}")

    @staticmethod
    def _parse_numeric_dimension(value: str | None) -> int | None:
        if not value:
            return None
        matched = re.match(r"^\s*([0-9]*\.?[0-9]+)", value)
        if not matched:
            return None
        return int(float(matched.group(1)))

    @staticmethod
    def _format_ratio(value: float) -> str:
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text if "." in text else f"{text}.0"

    def _relative_key(self, path: Path) -> str:
        return path.relative_to(self.assets_dir).as_posix()

    def _to_url_path(self, path: Path) -> str:
        relative_path = path.relative_to(self.assets_dir).as_posix()
        return f"/static/{relative_path}"
