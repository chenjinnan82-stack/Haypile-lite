from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.gui.desktop_startup import (
    prepare_desktop_runtime,
    read_desktop_gui_state,
)


class DesktopStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = Settings(
            STORAGE_DIR=root / "storage",
            LOG_DIR=root / "logs",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_module_has_no_qt_dependency(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "app/gui/desktop_startup.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("PySide6", source)
        self.assertNotIn("app_gui", source)

    def test_reads_valid_state_and_defaults_for_missing_or_invalid_state(self) -> None:
        self.settings.INDEX_DIR.mkdir(parents=True)
        state_path = self.settings.INDEX_DIR / "gui_state.json"
        state_path.write_text('{"language":"zh","x":42}', encoding="utf-8")
        self.assertEqual(
            read_desktop_gui_state(self.settings),
            {"language": "zh", "x": 42},
        )

        state_path.write_text("[1,2,3]", encoding="utf-8")
        self.assertEqual(read_desktop_gui_state(self.settings), {})
        state_path.write_text("{", encoding="utf-8")
        self.assertEqual(read_desktop_gui_state(self.settings), {})
        state_path.unlink()
        self.assertEqual(read_desktop_gui_state(self.settings), {})

        with patch.object(Path, "read_text", side_effect=OSError("blocked")):
            self.assertEqual(read_desktop_gui_state(self.settings), {})

    def test_prepares_storage_and_removes_only_stale_browser_files(self) -> None:
        incoming = self.settings.STORAGE_DIR / "incoming/browser"
        incoming.mkdir(parents=True)
        stale = incoming / "stale.gif"
        sidecar = incoming / "stale.gif.provenance.json"
        recent = incoming / "recent.gif"
        stale.write_bytes(b"stale")
        sidecar.write_text("{}", encoding="utf-8")
        recent.write_bytes(b"recent")
        now = time.time()
        old = now - 25 * 60 * 60
        os.utime(stale, (old, old))
        os.utime(sidecar, (old, old))

        with patch(
            "app.gui.desktop_startup.time.time",
            return_value=now,
        ):
            result = prepare_desktop_runtime(self.settings, {})

        self.assertTrue(result.storage_ready)
        self.assertIsNone(result.error_code)
        self.assertFalse(stale.exists())
        self.assertFalse(sidecar.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(self.settings.ASSETS_DIR.is_dir())
        self.assertTrue(self.settings.THEMES_DIR.is_dir())
        self.assertTrue(self.settings.INDEX_DIR.is_dir())

    def test_loads_only_matching_saved_api_credential(self) -> None:
        state = {
            "ai_provider": "api",
            "ai_api_key_present": True,
            "ai_api_base_url": "https://api.example.com/v1",
            "ai_api_authorized_host": "api.example.com",
        }
        with patch(
            "app.gui.desktop_startup.SystemCredentialStore.get",
            return_value="secret",
        ) as credential_get:
            result = prepare_desktop_runtime(self.settings, state)

        self.assertTrue(result.storage_ready)
        self.assertEqual(result.session_api_key, "secret")
        credential_get.assert_called_once_with("api.example.com")

        state["ai_api_authorized_host"] = "other.example.com"
        with patch(
            "app.gui.desktop_startup.SystemCredentialStore.get",
            side_effect=AssertionError("mismatched host must not read credentials"),
        ):
            mismatch = prepare_desktop_runtime(self.settings, state)
        self.assertEqual(mismatch.session_api_key, "")

    def test_credential_failure_does_not_block_storage(self) -> None:
        state = {
            "ai_provider": "api",
            "ai_api_key_present": True,
            "ai_api_base_url": "https://api.example.com",
            "ai_api_authorized_host": "api.example.com",
        }
        with patch(
            "app.gui.desktop_startup.SystemCredentialStore.get",
            side_effect=OSError("credential store unavailable"),
        ):
            result = prepare_desktop_runtime(self.settings, state)

        self.assertTrue(result.storage_ready)
        self.assertEqual(result.session_api_key, "")
        self.assertIsNone(result.error_code)

    def test_storage_failure_is_stable_and_skips_credentials(self) -> None:
        with (
            patch.object(Path, "mkdir", side_effect=OSError("read only")),
            patch(
                "app.gui.desktop_startup.SystemCredentialStore.get",
                side_effect=AssertionError("must not read credentials"),
            ),
        ):
            result = prepare_desktop_runtime(
                self.settings,
                {
                    "ai_provider": "api",
                    "ai_api_key_present": True,
                    "ai_api_base_url": "https://api.example.com",
                    "ai_api_authorized_host": "api.example.com",
                },
            )

        self.assertFalse(result.storage_ready)
        self.assertEqual(result.session_api_key, "")
        self.assertEqual(result.error_code, "storage_unavailable")


if __name__ == "__main__":
    unittest.main()
