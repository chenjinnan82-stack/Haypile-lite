from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.v1.health import readyz
from app.services.json_io import atomic_write_json
from app.services.scanner import (
    AssetScanner,
    ManifestReadinessError,
    mark_manifest_dirty,
    manifest_dirty_path,
    read_manifest_readiness,
)
from app.services.theme_registry import ThemeRegistry
from app.main import ManifestStaticFiles


class AtomicJsonIoTests(unittest.TestCase):
    def test_atomic_write_json_preserves_existing_file_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text('{"status": "old"}', encoding="utf-8")

            with patch("app.services.json_io.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    atomic_write_json(path, {"status": "new"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "old"})
            self.assertEqual(list(Path(tmpdir).glob("*.tmp")), [])

    def test_atomic_write_json_rejects_nonfinite_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text('{"status": "old"}', encoding="utf-8")

            with self.assertRaises(ValueError):
                atomic_write_json(path, {"confidence": float("nan")})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "old"})
            self.assertEqual(list(Path(tmpdir).glob("*.tmp")), [])

    def test_scanner_manifest_uses_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scanner = AssetScanner(assets_dir=root / "assets", manifest_path=root / "index" / "manifest.json")
            calls: list[tuple[Path, object]] = []

            def fake_atomic_write(path: Path, payload: object) -> None:
                calls.append((path, payload))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with patch("app.services.scanner.atomic_write_json", side_effect=fake_atomic_write):
                manifest = scanner._scan_assets_directory_sync()

            self.assertEqual(manifest, {})
            self.assertEqual(calls[-1][0], root / "index" / "manifest.json")
            self.assertFalse(manifest_dirty_path(scanner.manifest_path).exists())

    def test_scanner_leaves_dirty_marker_when_manifest_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "index" / "manifest.json"
            scanner = AssetScanner(assets_dir=root / "assets", manifest_path=manifest_path)

            def fail_manifest(path: Path, payload: object) -> None:
                if path == manifest_path:
                    raise OSError("manifest write failed")
                atomic_write_json(path, payload)

            with patch("app.services.scanner.atomic_write_json", side_effect=fail_manifest):
                with self.assertRaises(OSError):
                    scanner._scan_assets_directory_sync()

            self.assertTrue(manifest_dirty_path(manifest_path).exists())
            with self.assertRaises(ManifestReadinessError):
                read_manifest_readiness(manifest_path)

    def test_manifest_readiness_reports_generation_and_asset_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "manifest.json"
            atomic_write_json(manifest, {"one.png": {}, "two.wav": {}})

            readiness = read_manifest_readiness(manifest)

            self.assertEqual(readiness["asset_count"], 2)
            self.assertEqual(len(str(readiness["manifest_generation"])), 64)

    def test_readyz_fails_closed_and_recovers_after_manifest_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "manifest.json"
            settings = SimpleNamespace(MANIFEST_PATH=manifest)
            with patch("app.api.v1.health.get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as missing:
                    asyncio.run(readyz())
                self.assertEqual(missing.exception.status_code, 503)

                atomic_write_json(manifest, {"one.png": {}})
                mark_manifest_dirty(manifest)
                with self.assertRaises(HTTPException) as dirty:
                    asyncio.run(readyz())
                self.assertEqual(dirty.exception.status_code, 503)

                manifest_dirty_path(manifest).unlink()
                response = asyncio.run(readyz())
                self.assertEqual(response["status"], "ok")
                self.assertEqual(response["asset_count"], 1)

    def test_scanner_skips_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assets = root / "assets"
            outside = root / "outside.svg"
            outside.write_text('<svg width="10" height="10"></svg>', encoding="utf-8")
            assets.mkdir()
            try:
                (assets / "escape.svg").symlink_to(outside)
            except OSError:
                self.skipTest("symlink unavailable")

            scanner = AssetScanner(assets_dir=assets, manifest_path=root / "index" / "manifest.json")
            manifest = scanner._scan_assets_directory_sync()

            self.assertEqual(manifest, {})

    def test_static_files_only_serves_manifest_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assets = root / "assets"
            manifest = root / "index" / "manifest.json"
            assets.mkdir()
            manifest.parent.mkdir()
            (assets / "allowed.png").write_bytes(b"ok")
            (assets / "secret.txt").write_text("secret", encoding="utf-8")
            manifest.write_text(json.dumps({"allowed.png": {"url_path": "/static/allowed.png"}}), encoding="utf-8")
            static = ManifestStaticFiles(directory=str(assets), manifest_path=manifest)

            allowed_path, allowed_stat = static.lookup_path("allowed.png")
            secret_path, secret_stat = static.lookup_path("secret.txt")

            self.assertTrue(allowed_path.endswith("allowed.png"))
            self.assertIsNotNone(allowed_stat)
            self.assertEqual(secret_path, "")
            self.assertIsNone(secret_stat)

    def test_static_files_rejects_symlink_manifest_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assets = root / "assets"
            manifest = root / "index" / "manifest.json"
            outside = root / "outside.png"
            assets.mkdir()
            manifest.parent.mkdir()
            outside.write_bytes(b"secret")
            try:
                (assets / "linked.png").symlink_to(outside)
            except OSError:
                self.skipTest("symlink unavailable")
            manifest.write_text(json.dumps({"linked.png": {"url_path": "/static/linked.png"}}), encoding="utf-8")
            static = ManifestStaticFiles(directory=str(assets), manifest_path=manifest)

            linked_path, linked_stat = static.lookup_path("linked.png")

            self.assertEqual(linked_path, "")
            self.assertIsNone(linked_stat)

    def test_static_files_fail_closed_for_dirty_or_corrupt_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assets = root / "assets"
            manifest = root / "index" / "manifest.json"
            assets.mkdir()
            manifest.parent.mkdir()
            (assets / "allowed.png").write_bytes(b"ok")
            manifest.write_text(
                json.dumps({"allowed.png": {"url_path": "/static/allowed.png"}}),
                encoding="utf-8",
            )
            static = ManifestStaticFiles(directory=str(assets), manifest_path=manifest)
            self.assertTrue(static.lookup_path("allowed.png")[0])

            mark_manifest_dirty(manifest)
            self.assertEqual(static.lookup_path("allowed.png"), ("", None))
            manifest_dirty_path(manifest).unlink()
            manifest.write_text("{broken", encoding="utf-8")
            self.assertEqual(static.lookup_path("allowed.png"), ("", None))

    def test_theme_registry_uses_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = ThemeRegistry(themes_dir=Path(tmpdir))
            calls: list[tuple[Path, Path]] = []
            real_replace = os.replace

            def fake_replace(src: str, dst: Path) -> None:
                calls.append((Path(src), dst))
                real_replace(src, dst)

            with patch("app.services.json_io.os.replace", side_effect=fake_replace):
                payload, theme_file, created = registry.ensure_theme_contract("Demo Theme")

            self.assertTrue(created)
            self.assertEqual(theme_file.name, "demo_theme.json")
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(theme_file.read_text(encoding="utf-8"))["theme_name"], payload["theme_name"])


if __name__ == "__main__":
    unittest.main()
