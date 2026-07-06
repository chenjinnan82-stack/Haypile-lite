from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services.vault_service import VaultService


def _theme_payload(name: str) -> dict[str, object]:
    return {
        "theme_name": name,
        "css_variables": {},
        "tailwind_extend": {},
        "fonts": [],
        "physical_assets": {},
        "ui_dev_instruction": f"{name} instruction",
    }


class VaultServiceSecurityTests(unittest.TestCase):
    def test_path_traversal_theme_id_falls_back_to_generic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            themes_dir = root / "themes"
            themes_dir.mkdir(parents=True, exist_ok=True)

            (themes_dir / "generic.json").write_text(
                json.dumps(_theme_payload("generic"), ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "secret.json").write_text(
                json.dumps(_theme_payload("secret"), ensure_ascii=False),
                encoding="utf-8",
            )

            service = VaultService(themes_dir=themes_dir, fallback_theme_id="generic")
            payload = service.get_theme_payload("..\\secret")

            self.assertEqual(payload["theme_name"], "generic")

    def test_theme_id_is_normalized_before_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            themes_dir = Path(tmpdir) / "themes"
            themes_dir.mkdir(parents=True, exist_ok=True)

            (themes_dir / "generic.json").write_text(
                json.dumps(_theme_payload("generic"), ensure_ascii=False),
                encoding="utf-8",
            )
            (themes_dir / "zelda_totk.json").write_text(
                json.dumps(_theme_payload("zelda_totk"), ensure_ascii=False),
                encoding="utf-8",
            )

            service = VaultService(themes_dir=themes_dir, fallback_theme_id="generic")
            payload = service.get_theme_payload("  Zelda TOTK  ")

            self.assertEqual(payload["theme_name"], "zelda_totk")


if __name__ == "__main__":
    unittest.main()
