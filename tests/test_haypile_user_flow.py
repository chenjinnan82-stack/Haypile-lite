from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from app.core.config import get_settings
from app.services.bundle_service import BundleService
from app.services.style_classifier import StyleClassificationResult
from app_gui import IngestWorker
from examples.use_haypile_http import build_handoff


class HaypileUserFlowSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.storage_dir = self.tmpdir / "storage"
        self.assets_dir = self.storage_dir / "assets"
        self.themes_dir = self.storage_dir / "themes"
        self.index_dir = self.storage_dir / "index"
        self.manifest_path = self.index_dir / "assets_manifest.json"
        self.runtime_db_path = self.index_dir / "storage_runtime.db"
        self._env_names = [
            "STORAGE_DIR",
            "ASSETS_DIR",
            "THEMES_DIR",
            "INDEX_DIR",
            "MANIFEST_PATH",
            "VISION_CLASSIFIER_ENABLED",
            "VISION_FALLBACK_THEME",
            "HAYPILE_UI_LANG",
        ]
        self._old_env = {name: os.environ.get(name) for name in self._env_names}
        os.environ.update(
            {
                "STORAGE_DIR": self.storage_dir.as_posix(),
                "ASSETS_DIR": self.assets_dir.as_posix(),
                "THEMES_DIR": self.themes_dir.as_posix(),
                "INDEX_DIR": self.index_dir.as_posix(),
                "MANIFEST_PATH": self.manifest_path.as_posix(),
                "VISION_CLASSIFIER_ENABLED": "0",
                "VISION_FALLBACK_THEME": "generic",
                "HAYPILE_UI_LANG": "zh",
            }
        )
        get_settings.cache_clear()

    def tearDown(self) -> None:
        for name, value in self._old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_drop_flow_ingests_dedupes_exposes_handoff_and_survives_restart(self) -> None:
        image = self.tmpdir / "hero.svg"
        duplicate = self.tmpdir / "same-hero.svg"
        audio = self.tmpdir / "tone.wav"
        invalid = self.tmpdir / "fake.png"
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"></svg>'
        image.write_text(svg, encoding="utf-8")
        duplicate.write_text(svg, encoding="utf-8")
        invalid.write_text("not an image", encoding="utf-8")
        self._write_wav(audio)

        worker = IngestWorker([image, audio, duplicate, invalid], self.assets_dir, ai_enabled=True)
        worker.style_classifier = _HeroClassifier()
        finished: list[tuple[str, bool]] = []
        worker.finished_signal.connect(lambda message, ok: finished.append((message, ok)))

        worker.run()

        self.assertTrue(finished[-1][1])
        self.assertIn("新增 2", finished[-1][0])
        self.assertIn("去重 1", finished[-1][0])
        self.assertIn("拦截 1", finished[-1][0])
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), 2)
        self.assertTrue(all("/static/" in item["url_path"] for item in manifest.values()))

        service = BundleService(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            runtime_db_path=self.runtime_db_path,
        )
        ready = service.list_bundles(status="ready")
        handoff = build_handoff(ready)
        payload_text = json.dumps(handoff)

        self.assertEqual(len(ready), 2)
        self.assertTrue(any(item["role"] == "hero_image" for item in ready))
        self.assertTrue(any(item["type"] == "audio" for item in ready))
        self.assertEqual(handoff["source"], "haypile")
        self.assertEqual(handoff["handoff_version"], "haypile.asset-handoff.v1")
        self.assertNotIn("storage/assets", payload_text)
        self.assertNotIn(self.assets_dir.as_posix(), payload_text)
        self.assertTrue(all(item["resolved_url"].startswith("http://127.0.0.1:8010/static/") for item in handoff["assets"]))
        self.assertTrue(all(item["provenance"]["source"] == "haypile" for item in handoff["assets"]))

        restarted_service = BundleService(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            runtime_db_path=self.runtime_db_path,
        )
        self.assertEqual(
            [item["id"] for item in restarted_service.list_bundles(status="ready")],
            [item["id"] for item in ready],
        )

    def test_ai_toggle_disabled_skips_visual_classifier(self) -> None:
        image = self.tmpdir / "plain.svg"
        image.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"></svg>', encoding="utf-8")

        worker = IngestWorker([image], self.assets_dir, ai_enabled=False)
        worker.style_classifier = _ExplodingClassifier()
        finished: list[tuple[str, bool]] = []
        worker.finished_signal.connect(lambda message, ok: finished.append((message, ok)))

        worker.run()

        self.assertTrue(finished[-1][1])
        self.assertIn("新增 1", finished[-1][0])

    @staticmethod
    def _write_wav(path: Path) -> None:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 80)


class _HeroClassifier:
    async def classify_image(self, _image_path: Path, candidate_themes: list[str] | None = None) -> StyleClassificationResult:
        return StyleClassificationResult(
            theme_id="generic",
            theme_confidence=1.0,
            role_confidence=1.0,
            role="hero_image",
            source="test",
            reason="user_flow_smoke",
        )


class _ExplodingClassifier:
    async def classify_image(self, _image_path: Path, candidate_themes: list[str] | None = None) -> StyleClassificationResult:
        raise AssertionError("AI classifier should not be called")


if __name__ == "__main__":
    unittest.main()
