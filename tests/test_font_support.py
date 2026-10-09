from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import struct
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTCollection, TTFont, newTable
from fontTools.ttLib.tables.S_V_G_ import SVGDocument

from app.api.v1.bundles import get_bundle_service, router
from app.core.config import Settings
from app.core.file_lock import InterProcessFileLock
from app.services.asset_provenance import provenance_path_for, read_asset_provenance, sanitize_provenance
from app.services.bundle_service import BundleService
from app.services.font_service import MAX_FONT_BYTES, export_font_original, render_font_preview
from app.services.handoff import build_asset_handoff
from app.services.ingest_service import IngestCandidate, IngestService
from app.services.media_validator import MediaValidationError, validate_media
from app.services.scanner import AssetScanner, ManifestReadinessError, mark_manifest_dirty
from app.services.storage_runtime import StorageRuntimeDB
import mcp_server


def write_test_font(path: Path, *, width: int = 450, variable: bool = False, license: bool = True,
                    symbol: bool = False) -> None:
    """Original deterministic outline fixtures; no third-party font is redistributed."""
    builder = FontBuilder(1000, isTTF=path.suffix == ".ttf")
    characters = " " + "".join(chr(number) for number in range(33, 127))
    names = [".notdef"] + [f"uni{ord(char):04X}" for char in characters]
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({ord(char): name for char, name in zip(characters, names[1:])})
    glyphs = {}
    for index, name in enumerate(names):
        pen = TTGlyphPen(None) if builder.isTTF else T2CharStringPen(600, None)
        if name != "uni0020":
            pen.moveTo((40, 0))
            pen.lineTo((width, 0))
            pen.lineTo((width, 650 if index % 2 else 500))
            pen.lineTo((40, 700))
            pen.closePath()
        glyphs[name] = pen.glyph() if builder.isTTF else pen.getCharString()
    if builder.isTTF:
        builder.setupGlyf(glyphs)
    else:
        builder.setupCFF("HaypileAcceptance-Regular", {"FullName": "Haypile Acceptance Regular"}, glyphs, {})
    builder.setupHorizontalMetrics({name: (600, 40) for name in names})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    labels = {"familyName": "Haypile Acceptance", "styleName": "Regular",
              "uniqueFontIdentifier": f"Haypile-{width}", "fullName": "Haypile Acceptance Regular",
              "psName": "HaypileAcceptance-Regular", "version": "Version 1.0"}
    if license:
        labels.update(licenseDescription="Original test outlines; released under MIT.",
                      licenseInfoURL="https://opensource.org/license/mit")
    builder.setupNameTable(labels)
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    if variable:
        builder.setupFvar([("wght", 100, 400, 900, "Weight")], [])
        builder.setupGvar({name: [] for name in names})
    if symbol:
        cmap = builder.font["cmap"].getcmap(3, 1)
        cmap.platEncID = 0
        cmap.cmap = {code + 0xF000: glyph for code, glyph in cmap.cmap.items()}
        builder.font["cmap"].tables = [cmap]
    builder.font["head"].created = builder.font["head"].modified = 3800000000
    builder.save(path)


class FontSupportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage = self.root / "storage"
        self.assets = self.storage / "assets"
        self.index = self.storage / "index"
        self.themes = self.storage / "themes"
        self.manifest = self.index / "assets_manifest.json"
        self.runtime = self.index / "storage_runtime.db"
        self.ingest = IngestService(storage_dir=self.storage, assets_dir=self.assets, index_dir=self.index,
                                    themes_dir=self.themes, manifest_path=self.manifest, fallback_theme="generic")
        self.bundles = BundleService(assets_dir=self.assets, themes_dir=self.themes,
                                     manifest_path=self.manifest, runtime_db_path=self.runtime)
        self.ttf = self.root / "acceptance.ttf"
        write_test_font(self.ttf)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def store(self, *paths: Path) -> list[dict]:
        result = self.ingest.ingest([IngestCandidate(path) for path in paths or (self.ttf,)])
        self.assertTrue(result.success, result)
        return self.bundles.list_bundles()

    def test_formats_license_and_default_variable_instance(self) -> None:
        otf, variable, unknown = (self.root / name for name in ("outline.otf", "variable.ttf", "unknown.ttf"))
        truetype_otf = self.root / "truetype.otf"
        truetype_otf.write_bytes(self.ttf.read_bytes())
        write_test_font(otf)
        write_test_font(variable, variable=True)
        write_test_font(unknown, license=False)
        for font in (self.ttf, otf, truetype_otf, variable, unknown):
            with self.subTest(font=font.name):
                metadata = validate_media(font)
                self.assertEqual(metadata.kind, "font")
                self.assertEqual(metadata.mime_type, "font/" + font.suffix[1:])
                self.assertEqual(metadata.font_metadata["family"], "Haypile Acceptance")
                image, missing = render_font_preview(font, "Haypile 冬", 32)
                self.assertEqual(missing, "冬")
                self.assertGreater(len(set(image.get_flattened_data())), 2)
                image.close()
        self.assertTrue(validate_media(variable).font_metadata["is_variable"])
        self.assertFalse(validate_media(unknown).font_metadata["license_description"])

    def test_symbol_charmap_intake_preview_and_missing_glyphs(self) -> None:
        symbol = self.root / "symbol.ttf"
        write_test_font(symbol, symbol=True)
        self.assertEqual(validate_media(symbol).kind, "font")
        bundle, = self.store(symbol)
        self.assertEqual(bundle["status"], "pending")
        explicit, _ = render_font_preview(symbol, "\uf042\uf043", 32)
        preview, missing = render_font_preview(symbol, "BC", 32)
        self.assertEqual(missing, "")
        self.assertEqual(preview.tobytes(), explicit.tobytes())
        explicit.close()
        preview.close()
        preview, missing = render_font_preview(symbol, "B\uf042冬", 32)
        self.assertEqual(missing, "冬")
        preview.close()

    def test_embedded_svg_is_rejected_before_unbounded_decompression(self) -> None:
        svg_font = self.root / "svg.ttf"
        with TTFont(self.ttf) as font:
            font["SVG "] = newTable("SVG ")
            font["SVG "].docList = [SVGDocument('<svg xmlns="http://www.w3.org/2000/svg">' +
                " " * 65536 + "</svg>", 2, 2, compressed=True)]
            font.save(svg_font)
        reads = []
        original_read = gzip.GzipFile.read
        def track_read(stream, size=-1):
            reads.append(size)
            return original_read(stream, size)
        rejected = False
        with patch.object(gzip.GzipFile, "read", track_read):
            try:
                validate_media(svg_font)
            except MediaValidationError:
                rejected = True
        self.assertEqual(reads, [])
        self.assertTrue(rejected)
        self.assertLess(svg_font.stat().st_size, 10000)

    def test_invalid_truncated_disguised_and_size_limits(self) -> None:
        invalid = self.root / "invalid.ttf"
        for payload in (b"", b"not a font", self.ttf.read_bytes()[:len(self.ttf.read_bytes()) // 2], self.ttf.read_bytes()[:40]):
            invalid.write_bytes(payload)
            with self.assertRaises(MediaValidationError):
                validate_media(invalid)
        collection = TTCollection()
        collection.fonts = [TTFont(self.ttf)]
        collection.save(invalid)
        collection.close()
        with self.assertRaises(MediaValidationError):
            validate_media(invalid)
        invalid_otf = self.root / "invalid.otf"
        invalid_otf.write_bytes(b"OTTO" + self.ttf.read_bytes()[4:])
        with self.assertRaises(MediaValidationError):
            validate_media(invalid_otf)
        oversized_glyphs = self.root / "oversized-glyphs.ttf"
        with TTFont(self.ttf) as font:
            font["head"].unitsPerEm = 16
            font.save(oversized_glyphs)
        with self.assertRaises(MediaValidationError):
            validate_media(oversized_glyphs)
        with TTFont(self.ttf) as font:
            font.flavor = "woff"
            font.save(invalid)
        with self.assertRaises(MediaValidationError):
            validate_media(invalid)
        invalid.write_bytes(self.ttf.read_bytes())
        with invalid.open("r+b") as output:
            output.truncate(MAX_FONT_BYTES + 1)
        self.assertEqual(self.ingest._candidate_rejection_code(invalid), "font_file_too_large")
        with self.assertRaises(MediaValidationError):
            validate_media(invalid)
        invalid.write_bytes(self.ttf.read_bytes())
        with invalid.open("r+b") as output:
            output.seek(20)
            output.write(struct.pack(">I", 0xFFFFFFF0))
        with self.assertRaises(MediaValidationError):
            validate_media(invalid)
        for suffix in (".ttc", ".otc", ".woff", ".woff2"):
            unsupported = self.root / ("unsupported" + suffix)
            unsupported.write_bytes(self.ttf.read_bytes())
            self.assertEqual(self.ingest._candidate_rejection_code(unsupported), "unsupported_extension")

    def test_identity_usage_notes_duplicate_latest_and_restart(self) -> None:
        bundle, = self.store()
        self.assertEqual((bundle["type"], bundle["role"], bundle["status"]), ("font", "font", "pending"))
        self.assertIn("/fonts/", bundle["source_key"])
        self.assertEqual(bundle["sha256"], hashlib.sha256(self.ttf.read_bytes()).hexdigest())
        self.assertFalse(self.bundles.list_bundles(status="ready"))
        with self.assertRaises(ValueError):
            self.bundles.set_bundle_role(bundle["id"], "font")
        self.bundles.update_bundle_font(bundle["id"], font_usage="body", annotation={
            "source_note": "https://user:password@example.com/private.ttf?token=secret",
            "license_note": "Manual review: internal prototype only.",
        })
        duplicate = self.ingest.ingest([IngestCandidate(self.ttf)])
        self.assertEqual(duplicate.duplicate_count, 1)
        latest, = self.bundles.list_bundles(batch_id="latest", asset_type="font", font_usage="body")
        self.assertEqual(latest["status"], "ready")
        self.assertEqual(latest["font_annotation"]["source_note"], "https://example.com")
        self.assertEqual(latest["font_metadata"], bundle["font_metadata"])
        self.assertFalse(list(self.themes.glob("*.json")))
        restarted = self.ingest.recover_and_project()
        self.assertTrue(restarted.success)
        self.assertEqual(self.bundles.get_bundle(bundle["id"])["font_usage"], "body")
        path = self.assets / bundle["source_key"]
        previous = provenance_path_for(path).read_bytes()
        with patch("app.services.asset_provenance.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.bundles.update_bundle_font(bundle["id"], annotation={"source_note": "replacement"})
        self.assertEqual(provenance_path_for(path).read_bytes(), previous)
        mark_manifest_dirty(self.manifest)
        with self.assertRaises(ManifestReadinessError):
            self.bundles.list_bundles(asset_type="font")
        asyncio.run(AssetScanner(self.assets, self.manifest).scan_assets_directory())
        path.unlink()
        self.assertEqual(self.bundles.get_bundle(bundle["id"])["status"], "missing")
        self.assertIsNone(self.bundles.update_bundle_font(bundle["id"], font_usage="heading"))

    def test_font_update_busy_keeps_notes_and_returns_promptly(self) -> None:
        bundle, = self.store()
        original = self.assets / bundle["source_key"]
        self.bundles.update_bundle_font(bundle["id"], annotation={"license_note": "Previous note"})
        previous = provenance_path_for(original).read_bytes()
        with InterProcessFileLock(self.index / "ingest.transaction.lock"):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                self.bundles.update_bundle_font(bundle["id"], font_usage="body", annotation={"license_note": "New note"})
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(provenance_path_for(original).read_bytes(), previous)
        self.assertEqual(self.bundles.get_bundle(bundle["id"])["status"], "pending")

    def test_legacy_registration_and_new_projection(self) -> None:
        path = self.assets / "generic/fonts/legacy.ttf"
        path.parent.mkdir(parents=True)
        path.write_bytes(self.ttf.read_bytes())
        runtime = StorageRuntimeDB(self.runtime)
        self.assertEqual(runtime.register_legacy_assets(self.assets), 1)
        asyncio.run(AssetScanner(self.assets, self.manifest).scan_assets_directory())
        self.assertEqual(self.bundles.list_bundles()[0]["type"], "font")

    def test_alpha9_reader_retains_font_bytes(self) -> None:
        bundle, = self.store()
        path = self.assets / bundle["source_key"]
        # The alpha.9 runtime must retain committed font bytes even though its scanner ignores fonts.
        try:
            source = subprocess.check_output(["git", "show", "4b198d2:app/services/storage_runtime.py"],
                                             text=True, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("Alpha.9 commit unavailable in this checkout; compatibility check needs repository history")
        old = types.ModuleType("alpha9_storage_runtime")
        import sys
        sys.modules[old.__name__] = old
        try:
            exec(compile(source, "alpha9_storage_runtime.py", "exec"), old.__dict__)
            previous_runtime = old.StorageRuntimeDB(self.runtime)
            self.assertIn(path.resolve(), previous_runtime.committed_asset_paths(self.assets, verify_hashes=True))
            previous_runtime.register_legacy_assets(self.assets)
        finally:
            sys.modules.pop(old.__name__, None)
        self.assertEqual(path.read_bytes(), self.ttf.read_bytes())
        asyncio.run(AssetScanner(self.assets, self.manifest).scan_assets_directory())
        self.assertEqual(self.bundles.list_bundles()[0]["type"], "font")

    def test_interrupted_font_commit_recovers_once(self) -> None:
        from app.services.vfs_storage import VFSStorage
        runtime = StorageRuntimeDB(self.runtime)
        batch = runtime.begin_batch()
        runtime.record_item_discovered(batch, 1, self.ttf.name, media_kind="font")
        staged = VFSStorage().stage(self.ttf, self.storage / "staging/ingest", "font-recovery")
        destination = self.assets / "generic/fonts/recovered.ttf"
        runtime.record_item_staged(batch, 1, media_kind="font", sha256_hex=staged.sha256,
                                  staging_path=staged.path, destination_path=destination)
        self.assertTrue(self.ingest.recover_and_project().success)
        self.assertTrue(self.ingest.recover_and_project().success)
        font, = self.bundles.list_bundles(asset_type="font")
        self.assertEqual(font["sha256"], staged.sha256)
        self.assertEqual(destination.read_bytes(), self.ttf.read_bytes())
        self.assertFalse(staged.path.exists())

    def test_same_family_exact_file_preview_and_sample_bounds(self) -> None:
        other = self.root / "other.ttf"
        write_test_font(other, width=200)
        first, missing = render_font_preview(self.ttf, "ABC冬冬", 32)
        second, _ = render_font_preview(other, "ABC冬冬", 32)
        self.assertEqual(missing, "冬")
        self.assertNotEqual(first.tobytes(), second.tobytes())
        first.close()
        second.close()
        long, _ = render_font_preview(self.ttf, "A" * 10000, 10000)
        self.assertLess(long.height, 2000)
        self.assertEqual(long.width, 720)
        long.close()
        excessive_metrics = self.root / "excessive-metrics.ttf"
        with TTFont(self.ttf) as font:
            font["hhea"].ascent = 32767
            font["hhea"].descent = -32768
            font.save(excessive_metrics)
        with self.assertRaisesRegex(ValueError, "preview_size_limit"):
            render_font_preview(excessive_metrics, "A" * 120, 72)

    def test_export_bytes_and_failed_replace_tamper_protected_destination(self) -> None:
        bundle, = self.store()
        original = self.assets / bundle["source_key"]
        target = self.root / "export.ttf"
        export_font_original(original, target, sha256=bundle["sha256"], storage_dir=self.storage)
        self.assertEqual(target.read_bytes(), self.ttf.read_bytes())
        target.write_bytes(b"previous destination")
        with patch("app.services.font_service.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                export_font_original(original, target, sha256=bundle["sha256"], storage_dir=self.storage)
        self.assertEqual(target.read_bytes(), b"previous destination")
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            export_font_original(original, target, sha256="0" * 64, storage_dir=self.storage)
        self.assertEqual(target.read_bytes(), b"previous destination")
        self.assertFalse(list(self.root.glob(".haypile-font-*")))
        for protected in (original, self.index / "export.ttf", self.storage / "other.ttf"):
            with self.assertRaisesRegex(ValueError, "protected_storage"):
                export_font_original(original, protected, sha256=bundle["sha256"], storage_dir=self.storage)
        external_library = self.root / "custom-assets"
        external_library.mkdir()
        with self.assertRaisesRegex(ValueError, "protected_storage"):
            export_font_original(original, external_library / "target.ttf", sha256=bundle["sha256"],
                                 storage_dir=self.storage, protected_paths=(external_library,))
        if os.name != "nt":
            link = self.root / "library-link"
            link.symlink_to(self.storage, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "protected_storage"):
                export_font_original(original, link / "target.ttf", sha256=bundle["sha256"], storage_dir=self.storage)

    def test_remote_font_mime_octet_stream_validation_and_cleanup(self) -> None:
        from app.services.safe_remote_fetcher import download_remote_media
        class Response:
            def __init__(self, mime, data, declared=None):
                self.headers = {"content-type": mime, "content-length": str(len(data) if declared is None else declared)}
                self.data = data
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def iter_bytes(self): yield self.data
        incoming = self.root / "incoming"
        incoming.mkdir()
        for index, mime in enumerate(("font/ttf", "application/x-font-ttf", "application/octet-stream")):
            path, error = download_remote_media("https://cdn.example.com/test.ttf?token=secret", incoming, index,
                max_bytes=MAX_FONT_BYTES, timeout=1, opener=lambda *_a, **_k: Response(mime, self.ttf.read_bytes()))
            self.assertEqual(error, "")
            self.assertEqual(path.read_bytes(), self.ttf.read_bytes())
            self.assertEqual(read_asset_provenance(path)["origin_url"], "https://cdn.example.com")
            path.unlink()
            provenance_path_for(path).unlink()
        otf = self.root / "remote.otf"
        write_test_font(otf)
        for mime in ("font/sfnt", "application/font-sfnt"):
            for original in (self.ttf, otf):
                for suffix in ("", original.suffix):
                    with self.subTest(mime=mime, original=original.suffix, url_suffix=suffix):
                        path, error = download_remote_media("https://cdn.example.com/download" + suffix,
                            incoming, 1, max_bytes=MAX_FONT_BYTES, timeout=1,
                            opener=lambda *_a, **_k: Response(mime, original.read_bytes()))
                        self.assertEqual(error, "")
                        self.assertEqual(path.suffix, suffix or ".otf")
                        self.assertEqual(path.read_bytes(), original.read_bytes())
                        self.assertEqual(read_asset_provenance(path)["content_type"], "font/" + path.suffix[1:])
                        path.unlink()
                        provenance_path_for(path).unlink()
        for mime, data, url, declared, expected in (
            ("text/css", self.ttf.read_bytes(), "test.ttf", None, "unsupported"),
            ("text/html", b"<html>", "test.ttf", None, "unsupported"),
            ("application/octet-stream", self.ttf.read_bytes(), "test.zip", None, "unsupported"),
            ("font/ttf", b"not-a-font", "test.ttf", None, "invalid_font"),
            ("font/sfnt", b"not-a-font", "download", None, "invalid_font"),
            ("font/ttf", self.ttf.read_bytes(), "test.ttf", MAX_FONT_BYTES + 1, "font_too_large"),
        ):
            path, error = download_remote_media("https://cdn.example.com/" + url, incoming, 1,
                max_bytes=MAX_FONT_BYTES, timeout=1, opener=lambda *_a, **_k: Response(mime, data, declared))
            self.assertIsNone(path)
            self.assertEqual(error, expected)
            self.assertFalse(list(incoming.iterdir()))
        path, error = download_remote_media("https://cdn.example.com/test.ttf", incoming, 1,
            max_bytes=MAX_FONT_BYTES, timeout=1, should_stop=lambda: True,
            opener=lambda *_a, **_k: Response("font/ttf", self.ttf.read_bytes()))
        self.assertEqual(error, "interrupted")
        self.assertFalse(list(incoming.iterdir()))

    def test_remote_cleanup_closes_download_before_removal(self) -> None:
        from app.services.safe_remote_fetcher import download_remote_media
        class Response:
            headers = {"content-type": "font/ttf", "content-length": "0"}
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def iter_bytes(self): yield self.ttf.read_bytes()
        response = Response()
        response.ttf = self.ttf
        incoming = self.root / "incoming"
        incoming.mkdir()
        real_fdopen, real_unlink = os.fdopen, Path.unlink
        opened = []
        def track_fdopen(*args, **kwargs):
            stream = real_fdopen(*args, **kwargs)
            opened.append(stream)
            return stream
        def locked_unlink(path, *, missing_ok=False):
            if any(not stream.closed for stream in opened):
                raise PermissionError("Windows cannot remove an open download")
            return real_unlink(path, missing_ok=missing_ok)
        for cancelled, expected in ((True, "interrupted"), (False, "font_too_large")):
            with self.subTest(cancelled=cancelled):
                opened.clear()
                with patch("app.services.safe_remote_fetcher.os.fdopen", side_effect=track_fdopen), \
                     patch.object(Path, "unlink", locked_unlink):
                    path, error = download_remote_media("https://cdn.example.com/test.ttf", incoming, 1,
                        max_bytes=8, timeout=1, should_stop=lambda: cancelled,
                        opener=lambda *_a, **_k: response)
                self.assertIsNone(path)
                self.assertEqual(error, expected)
                self.assertFalse(list(incoming.iterdir()))

    def test_http_mcp_static_and_shared_handoff(self) -> None:
        bundle, = self.store()
        self.bundles.update_bundle_font(bundle["id"], font_usage="heading", annotation={"license_note": "Reviewed"})
        api = FastAPI()
        api.include_router(router, prefix="/api/v1")
        api.dependency_overrides[get_bundle_service] = lambda: self.bundles
        from app.main import ManifestStaticFiles
        api.mount("/static", ManifestStaticFiles(directory=self.assets, manifest_path=self.manifest))
        with TestClient(api) as client:
            response = client.get("/api/v1/bundles", params={"type": "font", "font_usage": "heading"})
            self.assertEqual(response.status_code, 200)
            font, = response.json()
            original = client.get(font["url"])
            self.assertEqual(original.content, self.ttf.read_bytes())
            self.assertEqual(original.headers["content-type"], "font/ttf")
            with patch.object(mcp_server, "get_json", side_effect=lambda url: client.get(url).json()):
                result = mcp_server.call_tool("haypile_list_bundles", {"type": "font", "font_usage": "heading"})
                self.assertEqual(result, [font])
                self.assertEqual(mcp_server.call_tool("haypile_list_bundles", {"type": "asset"}), [font])
            for tool in ("haypile_list_bundles", "haypile_copy_handoff"):
                mcp_server._validate_tool_arguments(tool, {"type": "font", "role": "font", "font_usage": "body"})
            with self.assertRaises(ValueError):
                mcp_server._validate_tool_arguments("haypile_list_bundles", {"font_usage": "music"})
            shared = build_asset_handoff([font], base_url=mcp_server.BASE_URL)
            self.assertEqual(shared["assets"], mcp_server.build_handoff([font])["assets"])
            self.assertEqual(shared["assets"][0]["font_usage"], "heading")
            self.assertEqual(shared["assets"][0]["font_annotation"], {"license_note": "Reviewed"})
            self.assertFalse(client.get("/static/unregistered.ttf").is_success)

    def test_notes_are_bounded_plain_data_and_private_paths_removed(self) -> None:
        cleaned = sanitize_provenance({"font_annotation": {"source_note": "/Users/private/font.ttf",
            "license_note": "x" * 10000, "command": "execute something"}})
        self.assertNotIn("source_note", cleaned["font_annotation"])
        self.assertEqual(len(cleaned["font_annotation"]["license_note"]), 2048)
        self.assertNotIn("command", cleaned["font_annotation"])
        for note in ("来源：/Users/private/font.ttf", r"Downloaded from C:\Users\private\font.ttf",
                     "file:///Users/private/font.ttf", r"Source: \\server\private\font.ttf",
                     "Source `/Users/private/font.ttf`", "Source //server/private/font.ttf",
                     "下载于/Users/private/font.ttf", r"下载于C:\Users\private\font.ttf"):
            with self.subTest(note=note):
                cleaned = sanitize_provenance({"font_annotation": {"source_note": note, "license_note": note}})
                self.assertEqual(cleaned["font_annotation"], {})
        notes = {"source_note": "HTTPS://user:secret@example.com:8443/font.ttf?token=secret",
                 "license_note": "See (HtTpS://example.com/license?token=secret). SIL/OFL 1.1"}
        cleaned = sanitize_provenance({"font_annotation": notes})
        self.assertEqual(cleaned["font_annotation"], {
            "source_note": "https://example.com:8443",
            "license_note": "See (https://example.com). SIL/OFL 1.1",
        })
        self.assertEqual(sanitize_provenance(cleaned), cleaned)
        for note, expected in (
            ("See https://reader(test):PRIVATE_TOKEN@example.com/font.ttf", "See https://example.com"),
            ("See https://example.com/font.ttf?token='PRIVATE_TOKEN'", "See https://example.com"),
            ("https://example.com/font.ttf?token=" + "!" * 24, "https://example.com"),
            ("https://example.com/font.ttf?token=" + ")" * 24, "https://example.com"),
            ("See 'https://example.com/font.ttf?token=PRIVATE_TOKEN'", "See 'https://example.com'"),
            ("x" * 1990 + " https://" + "PRIVATE_TOKEN" * 20 + "@example.com/font.ttf",
             "x" * 1990 + " https://example.com"),
            ("See https://[2606:4700:4700::1111]", "See https://[2606:4700:4700::1111]"),
            ("See [https://[2606:4700:4700::1111]]", "See [https://[2606:4700:4700::1111]]"),
            ("See [https://example.com/font.ttf]", "See [https://example.com]"),
            ("See https://example.com/font.ttf?token=PRIVATE_TOKEN[1]", "See https://example.com"),
        ):
            with self.subTest(url_boundary=note[:20]):
                cleaned = sanitize_provenance({"font_annotation": {"license_note": note}})
                self.assertEqual(cleaned["font_annotation"]["license_note"], expected)
                self.assertEqual(sanitize_provenance(cleaned), cleaned)
        for field, prefix in (("source_note", 500), ("license_note", 2030)):
            note = "x" * prefix + " https://[2606:4700:4700::1111]/font.ttf?token=PRIVATE_TOKEN"
            cleaned = sanitize_provenance({"font_annotation": {field: note}})
            self.assertEqual(cleaned["font_annotation"][field], "x" * prefix)
            self.assertEqual(sanitize_provenance(cleaned), cleaned)

    def test_gui_fonts_filter_all_201_items_async_preview_notes_export_cancel(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import app_gui
        from PySide6.QtWidgets import QApplication
        application = QApplication.instance() or QApplication([])
        bundle, = self.store()
        settings = Settings(_env_file=None, STORAGE_DIR=self.storage, LOG_DIR=self.root / "logs")
        panel = app_gui.MaterialPanelWindow(settings=settings, embedded=True)
        panel._batch_scope = "all"
        panel.show()
        panel.refresh()
        panel._set_filter_mode("font")
        panel._select_recent_item(0)
        self.assertTrue(panel.font_controls.isVisible())
        self.assertTrue(panel.role_row.isHidden())
        self.assertTrue(panel.retry_ai_button.isHidden())
        self.assertEqual(panel.font_size_input.value(), 32)

        def drain() -> None:
            deadline = time.monotonic() + 5
            while panel.font_preview_worker is not None or panel._font_preview_timer.isActive():
                application.processEvents()
                if time.monotonic() > deadline:
                    self.fail("font preview did not finish")
                time.sleep(.005)

        try:
            drain()
            self.assertIn("冬", panel.font_missing_label.text())
            panel.font_text_input.setText("ABC")
            panel.font_size_input.setValue(40)
            drain()
            self.assertFalse("冬" in panel.font_missing_label.text())
            panel._set_selected_font_usage("body")
            drain()
            self.assertEqual(self.bundles.get_bundle(bundle["id"])["status"], "ready")
            with patch.object(app_gui.QFileDialog, "getSaveFileName", return_value=("", "")), \
                 patch.object(app_gui, "export_font_original") as exported:
                panel._export_selected_font()
                exported.assert_not_called()
            all_bundles = [dict(bundle, id=f"item{index}", type="image", source_key=f"image{index:03}.png")
                           for index in range(201)] + [bundle]
            with patch.object(panel, "_bundle_service", return_value=types.SimpleNamespace(
                list_bundles=lambda **_kwargs: all_bundles, theme_recoveries=[], get_latest_batch=lambda: None,
                _asset_path=self.bundles._asset_path)):
                panel.refresh()
                self.assertEqual(len(panel._all_recent_items), 202)
                self.assertEqual(len(panel._recent_items), 1)
                panel.search_input.setText("Haypile Acceptance")
                panel.refresh()
                self.assertEqual(len(panel._recent_items), 1)
            drain()
            self.assertEqual(panel.font_text_input.text(), "ABC")
            self.assertEqual(panel.font_size_input.value(), 40)
            for language in ("zh", "en"):
                app_gui.set_ui_language(language)
                panel.retranslate()
                panel.resize(428, 342)
                application.processEvents()
                scroll = panel.font_controls.parentWidget().parentWidget().parentWidget()
                self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                self.assertLessEqual(panel.font_controls.width(), scroll.viewport().width())
        finally:
            app_gui.set_ui_language("auto")
            panel._font_preview_closing = True
            panel._cancel_font_preview()
            drain()
            panel.close()
            application.processEvents()

    def test_gui_stale_preview_hide_and_cooperative_shutdown(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import app_gui
        from PySide6.QtWidgets import QApplication
        application = QApplication.instance() or QApplication([])
        other = self.root / "other.ttf"
        write_test_font(other, width=200)
        self.store(self.ttf, other)
        settings = Settings(_env_file=None, STORAGE_DIR=self.storage, LOG_DIR=self.root / "logs")
        panel = app_gui.MaterialPanelWindow(settings=settings, embedded=True)
        panel._batch_scope = "all"
        panel.show()
        panel.refresh()
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(5)
            return render_font_preview(*args)
        try:
            with patch.object(app_gui, "render_font_preview", side_effect=blocked):
                panel._select_recent_item(0)
                self.assertTrue(entered.wait(2))
                panel._select_recent_item(1)
                selected = panel._selected_bundle_id
                release.set()
                deadline = time.monotonic() + 5
                while panel.font_preview_worker is not None:
                    application.processEvents()
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.005)
                self.assertEqual(panel._selected_bundle_id, selected)
                self.assertFalse(panel.font_preview_label.pixmap().isNull())
                panel.font_text_input.setText("different")
                panel.hide()
                self.assertFalse(panel._font_preview_timer.isActive())
                self.assertIsNone(panel._font_preview_request)
        finally:
            release.set()
            panel._font_preview_closing = True
            panel._cancel_font_preview()
            while panel.font_preview_worker is not None:
                application.processEvents()
                time.sleep(.005)
            panel.close()


if __name__ == "__main__":
    unittest.main()
