from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.services.material_summary import build_material_panel_summary


class MaterialSummaryTests(unittest.TestCase):
    def test_catalog_summary_uses_only_supplied_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp)
            older = assets_dir / "generic/images/older.png"
            newer = assets_dir / "generic/images/newer.gif"
            older.parent.mkdir(parents=True)
            older.write_bytes(b"old")
            newer.write_bytes(b"new")
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))

            summary = build_material_panel_summary(
                [
                    self._bundle("older.png", "ready", "hero_image", "image"),
                    self._bundle("newer.gif", "pending", "unknown", "image"),
                    self._bundle("sound.mp3", "missing", "audio", "audio"),
                ],
                assets_dir=assets_dir,
                max_items=2,
            )

        self.assertEqual(
            (summary.total_count, summary.recognized_count, summary.pending_count),
            (3, 1, 1),
        )
        self.assertEqual(summary.recognition_status, "分类：有副本缺失")
        self.assertEqual(len(summary.recent_items), 2)
        self.assertEqual([item.title for item in summary.recent_items], ["newer.gif", "older.png"])
        self.assertEqual([item.usage_label for item in summary.recent_items], ["未确定", "主视觉"])

    def test_orphan_theme_reference_cannot_enter_summary(self) -> None:
        summary = build_material_panel_summary([], assets_dir=Path("/unused"))
        self.assertEqual(summary.total_count, 0)
        self.assertEqual(summary.recent_items, [])

    @staticmethod
    def _bundle(
        name: str,
        status: str,
        role: str,
        asset_type: str,
    ) -> dict[str, str]:
        source_key = f"generic/{'audio' if asset_type == 'audio' else 'images'}/{name}"
        return {
            "id": name,
            "theme_id": "generic",
            "type": asset_type,
            "role": role,
            "status": status,
            "source_key": source_key,
            "url": f"/static/{source_key}",
            "origin_url": "",
        }


if __name__ == "__main__":
    unittest.main()
