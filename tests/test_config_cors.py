from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.core.config import (
    Settings,
    default_env_file,
    default_resource_dir,
    default_storage_dir,
    macos_app_bundle,
    runtime_mode_command,
)


class CorsConfigTests(unittest.TestCase):
    def test_wildcard_origin_disables_credentials(self) -> None:
        settings = Settings(_env_file=None, CORS_ORIGINS=["*"])
        self.assertFalse(settings.cors_allow_credentials)

    def test_explicit_origins_enable_credentials(self) -> None:
        settings = Settings(
            _env_file=None,
            CORS_ORIGINS=["http://127.0.0.1:5173", "http://localhost:5173"],
        )
        self.assertTrue(settings.cors_allow_credentials)


class PackagedRuntimeConfigTests(unittest.TestCase):
    APP_EXECUTABLE = "/Applications/Haypile.app/Contents/MacOS/Haypile"

    def test_macos_app_uses_bundle_resources_and_user_storage(self) -> None:
        with patch("app.core.config.sys.platform", "darwin"):
            self.assertEqual(
                macos_app_bundle(self.APP_EXECUTABLE),
                Path("/Applications/Haypile.app"),
            )
            self.assertEqual(
                default_resource_dir(self.APP_EXECUTABLE),
                Path("/Applications/Haypile.app/Contents/MacOS"),
            )
            self.assertEqual(
                default_storage_dir(self.APP_EXECUTABLE, home=Path("/Users/tester")),
                Path("/Users/tester/Library/Application Support/Haypile/storage"),
            )
            self.assertIsNone(default_env_file(self.APP_EXECUTABLE))
            self.assertEqual(default_env_file("/usr/bin/python3"), ".env")

    def test_runtime_commands_switch_only_inside_app_bundle(self) -> None:
        with patch("app.core.config.sys.platform", "darwin"):
            self.assertEqual(
                runtime_mode_command("backend", executable=self.APP_EXECUTABLE),
                [self.APP_EXECUTABLE, "--backend"],
            )
            self.assertEqual(
                runtime_mode_command(
                    "mcp",
                    executable="/usr/bin/python3",
                    source_root=Path("/tmp/haypile"),
                ),
                ["/usr/bin/python3", "/tmp/haypile/mcp_server.py"],
            )

    def test_storage_override_rebases_derived_paths(self) -> None:
        with TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir) / "store"
            settings = Settings(_env_file=None, STORAGE_DIR=storage, IPC_AUTHKEY="test-key")

        self.assertEqual(settings.ASSETS_DIR, storage / "assets")
        self.assertEqual(settings.THEMES_DIR, storage / "themes")
        self.assertEqual(settings.INDEX_DIR, storage / "index")
        self.assertEqual(settings.MANIFEST_PATH, storage / "index" / "assets_manifest.json")


if __name__ == "__main__":
    unittest.main()
