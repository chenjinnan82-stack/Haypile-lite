from __future__ import annotations

import math
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from mutagen import File as MutagenFile, MutagenError
from PIL import Image, UnidentifiedImageError

from app.services.media_types import SUPPORTED_AUDIO_EXTENSIONS


class MediaValidationError(ValueError):
    """Raised when a file is not a safe, supported Haypile asset."""


@dataclass(frozen=True)
class MediaValidation:
    kind: str
    mime_type: str
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None


RASTER_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
RASTER_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_RASTER_PIXELS = 80_000_000
MAX_RASTER_TOTAL_PIXELS = 160_000_000
MAX_RASTER_DIMENSION = 32_768
MAX_RASTER_FRAMES = 100
MAX_SVG_BYTES = 5 * 1024 * 1024
MAX_SVG_NODES = 10_000
MAX_SVG_DEPTH = 64
MAX_SVG_DIMENSION = 32_768
MAX_SVG_VIEWBOX_AREA = 80_000_000
MAX_SVG_ABS_COORDINATE = 1_000_000
_URL_REFERENCE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
_SVG_NUMBER = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
)


def validate_media(path: Path) -> MediaValidation:
    suffix = path.suffix.lower()
    if suffix in RASTER_EXTENSIONS:
        return validate_raster(path)
    if suffix == ".svg":
        return validate_svg(path)
    if suffix in SUPPORTED_AUDIO_EXTENSIONS:
        return validate_audio(path)
    raise MediaValidationError("unsupported_media")


def validate_raster(path: Path) -> MediaValidation:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = str(image.format or "").upper()
                if image_format not in RASTER_FORMATS:
                    raise MediaValidationError("unsupported_raster_format")
                expected_suffixes = {
                    "PNG": {".png"},
                    "JPEG": {".jpg", ".jpeg"},
                    "WEBP": {".webp"},
                }[image_format]
                if path.suffix.lower() not in expected_suffixes:
                    raise MediaValidationError("raster_extension_mismatch")
                width, height = (int(image.size[0]), int(image.size[1]))
                frames = int(getattr(image, "n_frames", 1) or 1)
                _validate_raster_limits(width, height, frames)
                image.verify()
            with Image.open(path) as image:
                for frame_index in range(frames):
                    image.seek(frame_index)
                    with image.copy() as frame:
                        frame.thumbnail((4096, 4096))
                        frame.load()
    except MediaValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise MediaValidationError("invalid_raster") from exc
    return MediaValidation("image", RASTER_FORMATS[image_format], width, height)


def _validate_raster_limits(width: int, height: int, frames: int) -> None:
    if width <= 0 or height <= 0:
        raise MediaValidationError("invalid_raster_dimensions")
    if width > MAX_RASTER_DIMENSION or height > MAX_RASTER_DIMENSION:
        raise MediaValidationError("raster_dimension_limit")
    if width * height > MAX_RASTER_PIXELS:
        raise MediaValidationError("raster_pixel_limit")
    if frames <= 0 or frames > MAX_RASTER_FRAMES:
        raise MediaValidationError("raster_frame_limit")
    if width * height * frames > MAX_RASTER_TOTAL_PIXELS:
        raise MediaValidationError("raster_total_pixel_limit")


def validate_svg(path: Path) -> MediaValidation:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise MediaValidationError("missing_svg") from exc
    if size <= 0 or size > MAX_SVG_BYTES:
        raise MediaValidationError("svg_size_limit")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise MediaValidationError("invalid_svg") from exc
    lowered = payload[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise MediaValidationError("svg_doctype_or_entity")
    if b"<?xml-stylesheet" in payload.lower():
        raise MediaValidationError("external_svg_resource")
    try:
        root = ElementTree.fromstring(
            payload,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (DefusedXmlException, ElementTree.ParseError, ValueError) as exc:
        raise MediaValidationError("invalid_svg") from exc
    if _local_name(root.tag) != "svg":
        raise MediaValidationError("invalid_svg_root")

    node_count = 0
    stack: list[tuple[Any, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        node_count += 1
        if node_count > MAX_SVG_NODES:
            raise MediaValidationError("svg_node_limit")
        if depth > MAX_SVG_DEPTH:
            raise MediaValidationError("svg_depth_limit")
        tag = _local_name(element.tag).lower()
        if tag in {"script", "foreignobject"}:
            raise MediaValidationError("unsafe_svg_element")
        text_content = str(element.text or "").lower()
        if "@import" in text_content or "http://" in text_content or "https://" in text_content:
            raise MediaValidationError("external_svg_resource")
        for match in _URL_REFERENCE.finditer(text_content):
            target = match.group(2).strip()
            if target and not target.startswith("#"):
                raise MediaValidationError("external_svg_resource")
        for raw_name, raw_value in element.attrib.items():
            name = _local_name(raw_name).lower()
            value = str(raw_value or "").strip()
            if name.startswith("on"):
                raise MediaValidationError("unsafe_svg_event")
            if name in {"href", "src"} and value and not value.startswith("#"):
                raise MediaValidationError("external_svg_resource")
            for match in _URL_REFERENCE.finditer(value):
                target = match.group(2).strip()
                if target and not target.startswith("#"):
                    raise MediaValidationError("external_svg_resource")
        stack.extend((child, depth + 1) for child in list(element))

    width, height = _svg_dimensions(root)
    return MediaValidation("image", "image/svg+xml", width, height)


def _local_name(value: object) -> str:
    text = str(value)
    return text.rsplit("}", 1)[-1]


def _svg_dimensions(root: Any) -> tuple[int, int]:
    width = _numeric_dimension(root.attrib.get("width"))
    height = _numeric_dimension(root.attrib.get("height"))
    if width and height:
        return width, height
    raw_viewbox = str(root.attrib.get("viewBox") or "")
    viewbox = raw_viewbox.replace(",", " ").split()
    if raw_viewbox and (len(raw_viewbox) > 512 or len(viewbox) != 4):
        raise MediaValidationError("invalid_svg_viewbox")
    if len(viewbox) == 4:
        try:
            x, y, raw_width, raw_height = (float(item) for item in viewbox)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MediaValidationError("invalid_svg_viewbox") from exc
        if not all(math.isfinite(item) for item in (x, y, raw_width, raw_height)):
            raise MediaValidationError("invalid_svg_viewbox")
        if raw_width <= 0 or raw_height <= 0:
            raise MediaValidationError("invalid_svg_viewbox")
        if max(abs(x), abs(y), raw_width, raw_height) > MAX_SVG_ABS_COORDINATE:
            raise MediaValidationError("svg_coordinate_limit")
        if raw_width > MAX_SVG_DIMENSION or raw_height > MAX_SVG_DIMENSION:
            raise MediaValidationError("svg_dimension_limit")
        if raw_width * raw_height > MAX_SVG_VIEWBOX_AREA:
            raise MediaValidationError("svg_viewbox_area_limit")
        return max(1, int(raw_width)), max(1, int(raw_height))
    # SVG's intrinsic fallback viewport is 300x150 when width, height, and
    # viewBox are all absent.
    return 300, 150


def _numeric_dimension(value: object) -> int | None:
    text = str(value or "")
    if not text.strip():
        return None
    if len(text) > 128:
        raise MediaValidationError("invalid_svg_dimensions")
    match = _SVG_NUMBER.match(text)
    if not match:
        if text.strip().lower() == "auto":
            return None
        raise MediaValidationError("invalid_svg_dimensions")
    try:
        raw = float(match.group(1))
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaValidationError("invalid_svg_dimensions") from exc
    if not math.isfinite(raw) or raw <= 0:
        raise MediaValidationError("invalid_svg_dimensions")
    if raw > MAX_SVG_DIMENSION:
        raise MediaValidationError("svg_dimension_limit")
    return max(1, int(raw))


def validate_audio(path: Path) -> MediaValidation:
    try:
        audio = MutagenFile(path)
    except (MutagenError, OSError, ValueError) as exc:
        raise MediaValidationError("invalid_audio") from exc
    if audio is None or audio.info is None:
        raise MediaValidationError("invalid_audio")
    try:
        duration = float(getattr(audio.info, "length", 0.0) or 0.0)
    except (TypeError, ValueError) as exc:
        raise MediaValidationError("invalid_audio_duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MediaValidationError("invalid_audio_duration")
    return MediaValidation("audio", "audio/unknown", duration_seconds=duration)
