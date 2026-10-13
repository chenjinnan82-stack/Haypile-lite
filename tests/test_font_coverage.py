from __future__ import annotations

import struct
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fontTools.fontBuilder import FontBuilder
from fontTools.ttLib import TTFont
from fontTools.ttLib.woff2 import WOFF2DirectoryEntry
from PIL import ImageFont

from app.services.font_service import read_font_metadata
from tests.test_font_support import write_test_font


class FontCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def read_unchanged(self, path: Path) -> dict:
        original = path.read_bytes()
        modified = path.stat().st_mtime_ns
        metadata = read_font_metadata(path)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        return metadata

    def test_full_ascii_counts_only_mapped_characters(self) -> None:
        path = self.root / "ascii.ttf"
        write_test_font(path)
        self.assertEqual(self.read_unchanged(path)["coverage"], {
            "character_count": 95,
            "latin_letter_count": 52,
            "digit_count": 10,
            "cjk_ideograph_count": 0,
            "symbol_encoding": False,
        })

    def test_partial_unicode_counts_cjk_block_boundaries_without_language_claims(self) -> None:
        path = self.root / "partial.ttf"
        write_test_font(path)
        # Both ends of the unified ideograph and compatibility blocks, plus
        # ASCII, other scripts, punctuation, radicals, emoji, and adjacent gaps.
        codepoints = (
            0x3400, 0x4DBF, 0x4E00, 0x9FFF, 0xF900, 0xFAFF,
            0x20000, 0x2A6DF, 0x2A700, 0x2B73F, 0x2B740, 0x2B81F,
            0x2B820, 0x2CEAF, 0x2CEB0, 0x2EBEF, 0x2EBF0, 0x2EE5F,
            0x2F800, 0x2FA1F, 0x30000, 0x3134F, 0x31350, 0x323AF,
            0x323B0, 0x3347F,
            0x41, 0x7A, 0x35, 0xE9, 0x3A9, 0x42F, 0x2F00, 0x3002,
            0x1F600, 0x2EE60, 0x33480,
        )
        with TTFont(path) as font:
            FontBuilder(font=font).setupCharacterMap({code: "uni0041" for code in codepoints})
            font.save(path)
        self.assertEqual(self.read_unchanged(path)["coverage"], {
            "character_count": 37,
            "latin_letter_count": 2,
            "digit_count": 1,
            "cjk_ideograph_count": 26,
            "symbol_encoding": False,
        })

    def test_symbol_encoding_counts_raw_mappings_without_ascii_aliases(self) -> None:
        path = self.root / "symbol.ttf"
        write_test_font(path, symbol=True)
        self.assertEqual(self.read_unchanged(path)["coverage"], {
            "character_count": 95,
            "latin_letter_count": 0,
            "digit_count": 0,
            "cjk_ideograph_count": 0,
            "symbol_encoding": True,
        })

    def test_web_font_coverage_preserves_compressed_original_bytes(self) -> None:
        original = self.root / "ascii.ttf"
        write_test_font(original)
        expected = self.read_unchanged(original)["coverage"]
        for flavor in ("woff", "woff2"):
            with self.subTest(flavor=flavor):
                path = self.root / f"ascii.{flavor}"
                with TTFont(original) as font:
                    font.flavor = flavor
                    font.save(path)
                self.assertEqual(self.read_unchanged(path)["coverage"], expected)

    def test_cff_woff2_advisory_sfnt_size_can_differ_from_decoded_size(self) -> None:
        original = self.root / "cff.otf"
        write_test_font(original)
        expected = self.read_unchanged(original)
        path = self.root / "cff.woff2"
        with TTFont(original) as font:
            font.flavor = "woff2"
            font.save(path)
        valid = path.read_bytes()
        sfnt_size = struct.unpack_from(">I", valid, 16)[0]
        # WOFF2 §3.2 makes totalSfntSize advisory, including for CFF fonts.
        # The negative case reproduces a real Font Awesome 7 header discrepancy.
        for difference in (-164, 165):
            with self.subTest(difference=difference):
                payload = bytearray(valid)
                struct.pack_into(">I", payload, 16, sfnt_size + difference)
                path.write_bytes(payload)
                with TTFont(BytesIO(payload)) as font:
                    self.assertIn("CFF ", font)
                    self.assertEqual(len(font.getBestCmap()), 95)
                self.assertIsNotNone(ImageFont.truetype(BytesIO(payload), 32).getmask("Haypile"))
                self.assertEqual(self.read_unchanged(path), expected)

    def test_underreported_woff2_header_cannot_hide_oversized_sfnt_tables(self) -> None:
        original = self.root / "cff.otf"
        write_test_font(original)
        path = self.root / "cff.woff2"
        with TTFont(original) as font:
            font.flavor = "woff2"
            font.save(path)
        payload = bytearray(path.read_bytes())
        source = BytesIO(payload)
        source.seek(48)
        expanded_size = 0
        for _ in range(struct.unpack_from(">H", payload, 12)[0]):
            entry = WOFF2DirectoryEntry()
            entry.fromFile(source)
            expanded_size += entry.length
        budget = struct.unpack_from(">I", payload, 16)[0] - 1
        self.assertLess(len(payload), budget)
        self.assertLess(expanded_size, budget)
        struct.pack_into(">I", payload, 16, budget)
        path.write_bytes(payload)
        # Use a small budget to prove table headers/padding remain bounded even
        # when the compressed file, Brotli output, and advisory field fit it.
        with patch("app.services.font_service.MAX_FONT_BYTES", budget), \
             patch("app.services.font_service.brotli.Decompressor", side_effect=AssertionError("must not decompress")):
            with self.assertRaisesRegex(ValueError, "font_size_limit"):
                read_font_metadata(path)


if __name__ == "__main__":
    unittest.main()
