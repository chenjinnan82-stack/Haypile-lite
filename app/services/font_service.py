from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

MAX_FONT_BYTES = 50 * 1024 * 1024
FONT_USAGES = frozenset({"unknown", "heading", "body", "decorative"})
DEFAULT_FONT_TEXT = "冬藏 Haypile 0123456789"


def _check_bitmap_bounds(decoder: ImageFont.FreeTypeFont, text: str) -> None:
    left, top, right, bottom = decoder.getbbox(text)
    if right - left > 2048 or bottom - top > 2048:
        raise ValueError("font_bitmap_size_limit")


def read_font_metadata(path: Path) -> dict[str, Any]:
    """Validate the actual SFNT file and decoder before exposing its metadata."""
    size = path.stat().st_size
    if size <= 0 or size > MAX_FONT_BYTES:
        raise ValueError("font_size_limit")
    with path.open("rb") as source:
        header = source.read(12)
        if len(header) != 12:
            raise ValueError("invalid_font")
        signature, count, *_ = struct.unpack(">4sHHHH", header)
        expected = b"OTTO" if path.suffix.lower() == ".otf" else b"\x00\x01\x00\x00"
        if path.suffix.lower() not in {".ttf", ".otf"} or signature != expected:
            raise ValueError("font_extension_mismatch")
        if not count or 12 + count * 16 > size:
            raise ValueError("invalid_font_directory")
        tags: set[bytes] = set()
        ranges: list[tuple[int, int]] = []
        for _ in range(count):
            tag, _checksum, offset, length = struct.unpack(">4sIII", source.read(16))
            if tag in tags or offset < 12 + count * 16 or offset + length > size:
                raise ValueError("invalid_font_directory")
            tags.add(tag)
            if length:
                ranges.append((offset, offset + length))
        ranges.sort()
        if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
            raise ValueError("invalid_font_directory")
    with TTFont(path, lazy=False) as font:
        required = {"head", "hhea", "maxp", "hmtx", "name", "cmap"}
        if not required.issubset(font.keys()) or not any(tag in font for tag in ("glyf", "CFF ", "CFF2")):
            raise ValueError("invalid_font_tables")
        font.ensureDecompiled()
        if not font.getBestCmap():
            raise ValueError("invalid_font_cmap")
        names = font["name"]

        def name(*ids: int) -> str:
            return next((str(text).replace("\x00", "").strip()[:4096]
                         for number in ids if (text := names.getDebugName(number))), "")

        os2 = font.get("OS/2")
        metadata = {
            "family": name(16, 1),
            "style": name(17, 2),
            "postscript_name": name(6),
            "weight": int(getattr(os2, "usWeightClass", 400)),
            "italic": bool(int(getattr(os2, "fsSelection", 0)) & 1 or int(font["head"].macStyle) & 2),
            "is_variable": "fvar" in font,
            "license_description": name(13),
            "license_url": name(14),
        }
    decoder = ImageFont.truetype(str(path), 32)
    _check_bitmap_bounds(decoder, "Haypile 0123")
    decoder.getmask("Haypile 0123")
    return metadata


def render_font_preview(path: Path, text: str, size: int) -> tuple[Image.Image, str]:
    """Pillow uses this exact file; missing glyphs never invoke system fallback."""
    text = str(text)[:120].replace("\n", " ").replace("\r", " ")
    size = max(12, min(int(size), 72))
    with TTFont(path, lazy=True) as font:
        cmap = font.getBestCmap() or {}
        missing = "".join(dict.fromkeys(char for char in text if ord(char) not in cmap))
    decoder = ImageFont.truetype(str(path), size)
    lines = [""]
    for char in text:
        if lines[-1] and decoder.getlength(lines[-1] + char) > 680:
            lines.append("")
        lines[-1] += char
    line_height = max(size + 12, sum(decoder.getmetrics()) + 8)
    if line_height * len(lines) + 24 > 2048:
        raise ValueError("font_preview_size_limit")
    for line in lines:
        _check_bitmap_bounds(decoder, line)
    canvas = Image.new("RGBA", (720, line_height * len(lines) + 24), "#F6F1E4")
    draw = ImageDraw.Draw(canvas)
    for index, line in enumerate(lines):
        draw.text((20, 12 + index * line_height), line, font=decoder, fill="#333333", anchor="lt")
    return canvas, missing


def export_font_original(
    source: Path, target: Path, *, sha256: str, storage_dir: Path,
    protected_paths: tuple[Path, ...] = (),
) -> None:
    """Replace an external destination only after copying and verifying all bytes."""
    target = target.resolve(strict=False)
    if any(target.is_relative_to(root.resolve(strict=False)) for root in (storage_dir, *protected_paths)):
        raise ValueError("protected_storage_destination")
    if source.suffix.lower() not in {".ttf", ".otf"} or target.suffix.lower() != source.suffix.lower():
        raise ValueError("font_extension_mismatch")
    if source.is_symlink() or not source.is_file():
        raise ValueError("missing_font")
    temporary: Path | None = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix=".haypile-font-", suffix=".tmp", dir=target.parent)
        temporary = Path(raw_path)
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as output, source.open("rb") as original:
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if not sha256 or digest.hexdigest() != sha256:
            raise ValueError("font_hash_mismatch")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
