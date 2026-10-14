from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import ManifestStaticFiles
import app.main as main_module
from app.services.asset_collections import save_collection, collection_handoff
from app.services.bundle_service import BundleService
from app.services.ingest_service import IngestService, IngestCandidate
from app.services.scanner import AssetScanner
import app.services.asset_integrity as integrity


class AssetIntegrityTests(unittest.TestCase):
    def test_windows_path_and_handle_ctime_can_differ_for_the_same_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.svg"
            source.write_bytes(b'<svg width="12" height="8"></svg>')
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            info = source.stat()
            fields = {name: getattr(info, name) for name in (
                "st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns")}
            # CPython 3.12 Windows stat uses creation time for ctime; fstat uses change time.
            path_stat = SimpleNamespace(**fields, st_birthtime_ns=10, st_ctime_ns=10)
            handle_stat = SimpleNamespace(**fields, st_birthtime_ns=10, st_ctime_ns=20)
            changed_stat = SimpleNamespace(**fields, st_birthtime_ns=10, st_ctime_ns=30)
            with patch.object(Path, "stat", return_value=path_stat), patch.object(
                integrity, "os", SimpleNamespace(name="nt", fstat=lambda _fd: handle_stat),
            ):
                self.assertEqual(integrity.asset_file_sha256(source), expected)
                # A change during hashing must still fail, even if size and mtime match.
                with patch.object(integrity.os, "fstat", side_effect=[handle_stat, changed_stat]):
                    self.assertEqual(integrity.asset_file_sha256(source), "")

    def test_changed_committed_bytes_keep_identity_but_cannot_be_handed_off_or_served(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "storage"
            assets, index, themes = (storage / name for name in ("assets", "index", "themes"))
            manifest = index / "assets_manifest.json"
            source = root / "image.svg"
            original_bytes = b'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"></svg>'
            source.write_bytes(original_bytes)
            ingest = IngestService(storage_dir=storage, assets_dir=assets, index_dir=index,
                                   themes_dir=themes, manifest_path=manifest)
            self.assertTrue(ingest.ingest([IngestCandidate(source)]).success)
            service = BundleService(assets_dir=assets, manifest_path=manifest, themes_dir=themes,
                                    runtime_db_path=index / "storage_runtime.db")
            original = service.list_bundles()[0]
            service.set_bundle_role(original["id"], "hero_image")
            group = save_collection(storage / "collections.json", "Original", asset_id=original["id"])
            stored = assets / original["source_key"]
            app = FastAPI()
            app.mount("/static", ManifestStaticFiles(directory=str(assets), manifest_path=manifest))
            with TestClient(app) as client:
                self.assertEqual(client.get(original["url"]).content, original_bytes)
                for payload in (b"x" * len(original_bytes), b"invalid changed SVG", original_bytes.replace(b"12", b"24")):
                    with self.subTest(payload=payload):
                        before = stored.stat()
                        stored.write_bytes(payload)
                        os.utime(stored, ns=(before.st_atime_ns, before.st_mtime_ns))
                        current = service.get_bundle(original["id"])
                        self.assertIsNotNone(current)
                        self.assertEqual(current["status"], "missing")
                        self.assertEqual(current["sha256"], original["sha256"])
                        self.assertEqual(service.list_bundles(status="ready"), [])
                        with self.assertRaisesRegex(ValueError, "no_ready_assets"):
                            collection_handoff(storage / "collections.json", group["id"], service,
                                               base_url="http://127.0.0.1:8010")
                        self.assertEqual(client.get(original["url"]).status_code, 404)
                        self.assertEqual(stored.read_bytes(), payload)  # Reads never repair or delete files.
                        stored.write_bytes(original_bytes)
                        self.assertEqual(service.get_bundle(original["id"])["status"], "ready")
                        self.assertEqual(client.get(original["url"]).content, original_bytes)

    def test_legacy_projection_records_identity_without_creating_a_runtime_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets, themes, manifest = root / "assets", root / "themes", root / "index/manifest.json"
            assets.mkdir()
            source = assets / "image.svg"
            source.write_text('<svg width="12" height="8"></svg>')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            projected = asyncio.run(AssetScanner(assets, manifest).scan_assets_directory())
            self.assertEqual(projected["image.svg"]["sha256"], digest)
            service = BundleService(assets_dir=assets, manifest_path=manifest, themes_dir=themes,
                                    runtime_db_path=manifest.parent / "storage_runtime.db")
            self.assertEqual(service.list_bundles()[0]["id"], digest)
            source.write_text('<svg width="24" height="8"></svg>')
            self.assertEqual(service.get_bundle(digest)["status"], "missing")
            self.assertFalse(service.runtime_db_path.exists())
            self.assertEqual(ManifestStaticFiles(directory=str(assets), manifest_path=manifest).lookup_path("image.svg"), ("", None))

    def test_windows_manifest_cache_does_not_hide_an_identity_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets, manifest = root / "assets", root / "manifest.json"
            assets.mkdir()
            source = assets / "image.svg"
            source.write_text('<svg width="12" height="8"></svg>')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest.write_text(json.dumps({"image.svg": {"type": "image", "sha256": digest}}))
            original_stat = manifest.stat()
            real_stat = Path.stat
            def windows_stat(path, **kwargs):
                return original_stat if path == manifest else real_stat(path, **kwargs)
            # Windows creation-time can remain identical after an equal-length edit.
            with patch.object(main_module, "os", SimpleNamespace(name="nt")), patch.object(Path, "stat", windows_stat):
                static = ManifestStaticFiles(directory=str(assets), manifest_path=manifest)
                self.assertTrue(static.lookup_path("image.svg")[0])
                manifest.write_text(json.dumps({"image.svg": {"type": "image", "sha256": "0" * 64}}))
                self.assertEqual(static.lookup_path("image.svg"), ("", None))

    def test_unverified_legacy_manifest_cannot_trust_invalid_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets, manifest = root / "assets", root / "manifest.json"
            assets.mkdir()
            (assets / "bad.svg").write_bytes(b"not an svg")
            manifest.write_text(json.dumps({"bad.svg": {"type": "image", "url_path": "/static/bad.svg"}}))
            service = BundleService(assets_dir=assets, manifest_path=manifest, themes_dir=root / "themes",
                                    runtime_db_path=root / "storage_runtime.db")
            self.assertEqual(service.list_bundles()[0]["status"], "missing")
            self.assertEqual(ManifestStaticFiles(directory=str(assets), manifest_path=manifest).lookup_path("bad.svg"), ("", None))
            self.assertFalse(service.runtime_db_path.exists())
