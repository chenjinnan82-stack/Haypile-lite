from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.core.config import (
    Settings,
    default_env_file,
    default_log_dir,
    default_resource_dir,
    default_storage_dir,
    macos_app_bundle,
    runtime_mode_command,
    windows_app_dir,
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
    MAC_EXECUTABLE = "/Applications/Haypile.app/Contents/MacOS/Haypile"
    WINDOWS_EXECUTABLE = "C:/Apps/Haypile/Haypile.exe"

    def test_macos_app_uses_bundle_resources_and_user_storage(self) -> None:
        with patch("app.core.config.sys.platform", "darwin"):
            self.assertEqual(
                macos_app_bundle(self.MAC_EXECUTABLE),
                Path(self.MAC_EXECUTABLE).resolve(strict=False).parents[2],
            )
            self.assertEqual(
                default_resource_dir(self.MAC_EXECUTABLE),
                Path(self.MAC_EXECUTABLE).resolve(strict=False).parent,
            )
            self.assertEqual(
                default_storage_dir(self.MAC_EXECUTABLE, home=Path("/Users/tester")),
                Path("/Users/tester/Library/Application Support/Haypile/storage"),
            )
            self.assertEqual(
                default_log_dir(self.MAC_EXECUTABLE, home=Path("/Users/tester")),
                Path("/Users/tester/Library/Logs/Haypile"),
            )
            self.assertIsNone(default_env_file(self.MAC_EXECUTABLE))
            self.assertEqual(default_env_file("/usr/bin/python3"), ".env")

    def test_windows_app_uses_executable_resources_and_local_app_data(self) -> None:
        with (
            patch("app.core.config.sys.platform", "win32"),
            patch.dict(
                "os.environ",
                {"LOCALAPPDATA": "C:/Users/tester/AppData/Local"},
                clear=False,
            ),
        ):
            self.assertEqual(windows_app_dir(self.WINDOWS_EXECUTABLE), Path("C:/Apps/Haypile"))
            self.assertEqual(default_resource_dir(self.WINDOWS_EXECUTABLE), Path("C:/Apps/Haypile"))
            self.assertEqual(
                default_storage_dir(self.WINDOWS_EXECUTABLE),
                Path("C:/Users/tester/AppData/Local/Haypile/storage"),
            )
            self.assertEqual(
                default_log_dir(self.WINDOWS_EXECUTABLE),
                Path("C:/Users/tester/AppData/Local/Haypile/logs"),
            )
            self.assertIsNone(default_env_file(self.WINDOWS_EXECUTABLE))
            self.assertEqual(default_env_file("C:/Python312/python.exe"), ".env")

        with (
            patch("app.core.config.sys.platform", "win32"),
            patch.dict("os.environ", {"LOCALAPPDATA": ""}, clear=False),
        ):
            self.assertEqual(
                default_storage_dir(self.WINDOWS_EXECUTABLE, home=Path("C:/Users/tester")),
                Path("C:/Users/tester/AppData/Local/Haypile/storage"),
            )

    def test_runtime_commands_switch_only_inside_app_bundle(self) -> None:
        with patch("app.core.config.sys.platform", "darwin"):
            self.assertEqual(
                runtime_mode_command("backend", executable=self.MAC_EXECUTABLE),
                [self.MAC_EXECUTABLE, "--backend"],
            )
            self.assertEqual(
                runtime_mode_command(
                    "mcp",
                    executable="/usr/bin/python3",
                    source_root=Path("/tmp/haypile"),
                ),
                ["/usr/bin/python3", str(Path("/tmp/haypile") / "mcp_server.py")],
            )

    def test_windows_runtime_commands_use_the_packaged_executable(self) -> None:
        with patch("app.core.config.sys.platform", "win32"):
            self.assertEqual(
                runtime_mode_command("backend", executable=self.WINDOWS_EXECUTABLE),
                [self.WINDOWS_EXECUTABLE, "--backend"],
            )
            self.assertEqual(
                runtime_mode_command(
                    "mcp",
                    executable="C:/Python312/python.exe",
                    source_root=Path("C:/src/haypile"),
                ),
                [
                    "C:/Python312/python.exe",
                    str(Path("C:/src/haypile") / "mcp_server.py"),
                ],
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
