from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.json_io import atomic_write_json
from app.services.scanner import AssetScanner
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
            self.assertEqual(calls[0][0], root / "index" / "manifest.json")

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
