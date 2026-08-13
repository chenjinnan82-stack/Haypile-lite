from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app.services.desktop_diagnostics import (
    build_desktop_diagnostic_summary,
    read_build_commit,
)


class DesktopDiagnosticTests(unittest.TestCase):
    def test_summary_is_whitelisted_and_does_not_copy_asset_data(self) -> None:
        summary = build_desktop_diagnostic_summary(
            app_version="0.3.0a8",
            build_commit="a" * 40,
            runtime="packaged",
            platform_name="darwin",
            python_version="3.12.10",
            qt_version="6.11.1",
            language="zh",
            low_power_enabled=True,
            storage_ready=True,
            backend_phase="ready",
            manifest_state="ready",
            bundles=[
                {
                    "status": "ready",
                    "source_key": "private/portrait.gif",
                    "origin_url": "https://secret.example/path?token=value",
                    "local_path": "/Users/person/private.gif",
                    "sha256": "b" * 64,
                },
                {"status": "pending", "filename": "private-audio.m4a"},
                {"status": "missing"},
            ],
            ai_provider="api",
            ai_enabled=False,
        )

        encoded = json.dumps(summary)
        self.assertEqual(
            summary["asset_counts"],
            {"total": 3, "ready": 1, "pending": 1, "missing": 1},
        )
        self.assertNotIn("portrait", encoded)
        self.assertNotIn("secret.example", encoded)
        self.assertNotIn("/Users/", encoded)
        self.assertNotIn("private-audio", encoded)
        self.assertNotIn("b" * 64, encoded)

    def test_unknown_freeform_values_are_not_reflected(self) -> None:
        summary = build_desktop_diagnostic_summary(
            app_version="/Users/private/version",
            build_commit="/Users/private/commit",
            runtime="/Users/private/runtime",
            platform_name="host-secret",
            python_version="private_name",
            qt_version="bad/version",
            language="person@example.com",
            low_power_enabled=False,
            storage_ready=False,
            backend_phase="private-phase",
            manifest_state="private-state",
            bundles=[],
            ai_provider="https://secret.example",
            ai_enabled=True,
        )

        encoded = json.dumps(summary)
        self.assertNotIn("Users", encoded)
        self.assertNotIn("private_name", encoded)
        self.assertNotIn("person@example.com", encoded)
        self.assertNotIn("secret.example", encoded)
        self.assertEqual(summary["build_commit"], "unavailable")
        self.assertEqual(summary["backend_phase"], "unknown")

    def test_reads_only_valid_public_build_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            build_info = base / "BUILD_INFO.json"
            build_info.write_text(
                json.dumps(
                    {
                        "version": "0.3.0-alpha.8",
                        "commit": "0123456789abcdef" * 2 + "01234567",
                        "private_path": "/Users/person/project",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                read_build_commit(base),
                "0123456789abcdef" * 2 + "01234567",
            )

            build_info.write_text('{"commit":"/Users/person/project"}', encoding="utf-8")
            self.assertEqual(read_build_commit(base), "unavailable")
            build_info.unlink()
            self.assertEqual(read_build_commit(base), "unavailable")


if __name__ == "__main__":
    unittest.main()
