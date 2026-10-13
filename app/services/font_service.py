from __future__ import annotations

import hashlib
from io import BytesIO
import os
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any

import brotli
from fontTools.ttLib import TTFont
from fontTools.ttLib.woff2 import WOFF2DirectoryEntry
from PIL import Image, ImageDraw, ImageFont

from app.services.media_types import SUPPORTED_FONT_EXTENSIONS

MAX_FONT_BYTES = 50 * 1024 * 1024
FONT_USAGES = frozenset({"unknown", "heading", "body", "decorative"})
DEFAULT_FONT_TEXT = "冬藏 Haypile 0123456789"
# Unicode 17.0 CJK ideograph block ranges (inclusive), including compatibility
# blocks. Coverage counts mappings in these blocks, not language completeness.
_CJK_IDEOGRAPH_RANGES = (
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0x20000, 0x2A6DF),  # Extension B
    (0x2A700, 0x2B73F),  # Extension C
    (0x2B740, 0x2B81F),  # Extension D
    (0x2B820, 0x2CEAF),  # Extension E
    (0x2CEB0, 0x2EBEF),  # Extension F
    (0x2EBF0, 0x2EE5F),  # Extension I
    (0x2F800, 0x2FA1F),  # CJK Compatibility Ideographs Supplement
    (0x30000, 0x3134F),  # Extension G
    (0x31350, 0x323AF),  # Extension H
    (0x323B0, 0x3347F),  # Extension J
)


def _validated_font_bytes(path: Path) -> bytes:
    with path.open("rb") as source:
        payload = source.read(MAX_FONT_BYTES + 1)
    if not payload or len(payload) > MAX_FONT_BYTES:
        raise ValueError("font_size_limit")
    extension = path.suffix.lower()
    if extension not in {".woff", ".woff2"}:
        return payload
    header_size = 44 if extension == ".woff" else 48
    if len(payload) < header_size or payload[:4] != (b"wOFF" if extension == ".woff" else b"wOF2"):
        raise ValueError("font_extension_mismatch")
    length, count, reserved, sfnt_size = struct.unpack_from(">IHHI", payload, 8)
    if (length != len(payload) or not count or reserved or sfnt_size > MAX_FONT_BYTES
            or payload[4:8] not in {b"\x00\x01\x00\x00", b"OTTO"}):
        raise ValueError("invalid_font_directory")
    # Validate a snapshot before FontTools / FreeType decode it again. Both decoders
    # then see only streams whose expansion has already been bounded and checked.
    tags: set[str] = set()
    original_size = 12 + count * 16
    if extension == ".woff":
        directory_end = header_size + count * 20
        if directory_end > len(payload):
            raise ValueError("invalid_font_directory")
        entries = [struct.unpack_from(">4sIIII", payload, header_size + index * 20) for index in range(count)]
        ranges = []
        for tag, offset, compressed, original, _checksum in entries:
            name = tag.decode("latin1")
            if name in tags or offset < directory_end or offset + compressed > len(payload) or compressed > original:
                raise ValueError("invalid_font_directory")
            tags.add(name)
            ranges.append((offset, offset + compressed))
            original_size += (original + 3) & ~3
        ranges.sort()
        if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
            raise ValueError("invalid_font_directory")
        if "SVG " in tags:
            raise ValueError("font_svg_not_supported")
        if original_size != sfnt_size:
            raise ValueError("invalid_font_size")
        font_end = max(end for _start, end in ranges)
        for _tag, offset, compressed, original, _checksum in entries:
            if compressed == original:
                continue
            decoder = zlib.decompressobj()
            output = decoder.decompress(payload[offset:offset + compressed], original + 1)
            if len(output) != original or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError("invalid_font_compression")
    else:
        source = BytesIO(payload)
        source.seek(header_size)
        expanded_size = 0
        for _ in range(count):
            entry = WOFF2DirectoryEntry()
            entry.fromFile(source)
            if entry.tag in tags:
                raise ValueError("invalid_font_directory")
            tags.add(str(entry.tag))
            original_size += (entry.origLength + 3) & ~3
            expanded_size += entry.length
        if "SVG " in tags:
            raise ValueError("font_svg_not_supported")
        # WOFF2 §3.2 makes totalSfntSize advisory; bound directory sizes directly.
        if original_size > MAX_FONT_BYTES or expanded_size > MAX_FONT_BYTES:
            raise ValueError("font_size_limit")
        compressed_size = struct.unpack_from(">I", payload, 20)[0]
        start = source.tell()
        if not compressed_size or start + compressed_size > len(payload):
            raise ValueError("invalid_font_directory")
        font_end = start + compressed_size
        decoder = brotli.Decompressor()
        chunk = payload[start:start + compressed_size]
        expanded = 0
        while True:
            output = decoder.process(chunk, output_buffer_limit=65536)
            chunk = b""
            expanded += len(output)
            if expanded > expanded_size:
                raise ValueError("font_size_limit")
            if decoder.is_finished():
                break
            if not output:
                raise ValueError("invalid_font_compression")
        if expanded != expanded_size:
            raise ValueError("invalid_font_compression")
    # WOFF's optional XML / private blocks are not used for SFNT name metadata.
    # Clear their pointers so the libraries never decompress those untrusted blocks.
    metadata_offset = 24 if extension == ".woff" else 28
    safe_payload = bytearray(payload[:font_end])
    safe_payload.extend(b"\0" * (-font_end % 4))
    safe_payload[metadata_offset:metadata_offset + 20] = b"\0" * 20
    struct.pack_into(">I", safe_payload, 8, len(safe_payload))
    return bytes(safe_payload)


def _font_cmap(font: TTFont) -> tuple[dict[int, str], str]:
    cmap = font.getBestCmap()
    if cmap is not None:
        return cmap, ""
    symbol = font["cmap"].getcmap(3, 0)
    if symbol is None:
        return {}, ""
    return symbol.cmap, "symb"


def _check_bitmap_bounds(decoder: ImageFont.FreeTypeFont, text: str) -> None:
    left, top, right, bottom = decoder.getbbox(text)
    if right - left > 2048 or bottom - top > 2048:
        raise ValueError("font_bitmap_size_limit")


def read_font_metadata(path: Path) -> dict[str, Any]:
    """Validate the actual SFNT file and decoder before exposing its metadata."""
    payload = _validated_font_bytes(path)
    size = len(payload)
    extension = path.suffix.lower()
    signature = payload[4:8] if extension in {".woff", ".woff2"} else payload[:4]
    with BytesIO(payload) as source:
        header = source.read(12)
        if len(header) != 12:
            raise ValueError("invalid_font")
        _, count, *_ = struct.unpack(">4sHHHH", header)
        signatures = {b"\x00\x01\x00\x00"} if extension == ".ttf" else {b"\x00\x01\x00\x00", b"OTTO"}
        if extension not in SUPPORTED_FONT_EXTENSIONS or signature not in signatures:
            raise ValueError("font_extension_mismatch")
        if extension in {".woff", ".woff2"}:
            count = 0  # The compressed directory was already checked above.
        elif not count or 12 + count * 16 > size:
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
        # ponytail: SVG glyphs need bounded SVG decoding before we can support them.
        if b"SVG " in tags:
            raise ValueError("font_svg_not_supported")
    with TTFont(BytesIO(payload), lazy=True) as font:
        required = {"head", "hhea", "maxp", "hmtx", "name", "cmap"}
        outlines = ("CFF ", "CFF2") if signature == b"OTTO" else ("glyf",)
        if not required.issubset(font.keys()) or not any(tag in font for tag in outlines):
            raise ValueError("invalid_font_tables")
        for tag in required | {"OS/2", "fvar"}:
            if tag in font:
                font[tag]
        cmap, encoding = _font_cmap(font)
        if not cmap:
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
            "coverage": {
                "character_count": len(cmap),
                "latin_letter_count": sum(0x41 <= code <= 0x5A or 0x61 <= code <= 0x7A for code in cmap),
                "digit_count": sum(0x30 <= code <= 0x39 for code in cmap),
                "cjk_ideograph_count": sum(any(start <= code <= end for start, end in _CJK_IDEOGRAPH_RANGES)
                                           for code in cmap),
                "symbol_encoding": encoding == "symb",
            },
        }
    decoder = ImageFont.truetype(BytesIO(payload), 32, encoding=encoding)
    sample = "Haypile 0123" if not encoding else "".join(chr(code) for code in sorted(cmap)[:12])
    _check_bitmap_bounds(decoder, sample)
    decoder.getmask(sample)
    return metadata


def render_font_preview(path: Path, text: str, size: int, *, width: int = 720) -> tuple[Image.Image, str]:
    """Pillow uses this exact file; missing glyphs never invoke system fallback."""
    text = str(text)[:120].replace("\n", " ").replace("\r", " ")
    size = max(12, min(int(size), 72))
    width = max(80, min(int(width), 720))
    preview_text = text
    payload = _validated_font_bytes(path)
    with TTFont(BytesIO(payload), lazy=True) as font:
        cmap, encoding = _font_cmap(font)
        if encoding == "symb":
            preview_text = text.translate({code: code + 0xF000 for code in range(256)
                                           if code not in cmap and code + 0xF000 in cmap})
        missing = "".join(dict.fromkeys(char for char, rendered in zip(text, preview_text) if ord(rendered) not in cmap))
    decoder = ImageFont.truetype(BytesIO(payload), size, encoding=encoding)
    lines = [""]
    for char in preview_text:
        if lines[-1] and decoder.getlength(lines[-1] + char) > width - 40:
            lines.append("")
        lines[-1] += char
    line_height = max(size + 12, sum(decoder.getmetrics()) + 8)
    if line_height * len(lines) + 24 > 2048:
        raise ValueError("font_preview_size_limit")
    for line in lines:
        _check_bitmap_bounds(decoder, line)
    canvas = Image.new("RGBA", (width, line_height * len(lines) + 24), "#F6F1E4")
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
    if source.suffix.lower() not in SUPPORTED_FONT_EXTENSIONS or target.suffix.lower() != source.suffix.lower():
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
