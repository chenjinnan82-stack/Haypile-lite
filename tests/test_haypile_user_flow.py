from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.core.config import get_settings
from app.services.bundle_service import BundleService
from app.services.scanner import AssetScanner
from app.services.storage_runtime import StorageRuntimeDB
from app.services.style_classifier import StyleClassificationResult
from app_gui import AIBatchWorker, IngestWorker
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
        finished: list[tuple[str, bool]] = []
        completed_batches: list[str] = []
        worker.finished_signal.connect(lambda message, ok: finished.append((message, ok)))
        worker.batch_signal.connect(lambda batch_id, _summary: completed_batches.append(batch_id))

        worker.run()

        self.assertTrue(finished[-1][1])
        self.assertIn("新增 2", finished[-1][0])
        self.assertIn("去重 1", finished[-1][0])
        self.assertIn("拦截 1", finished[-1][0])
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), 2)
        self.assertTrue(all("/static/" in item["url_path"] for item in manifest.values()))
        audio_manifest = next(item for item in manifest.values() if item["type"] == "audio")
        self.assertIn("duration_seconds", audio_manifest)
        self.assertEqual(audio_manifest["audio_metadata"]["sample_rate_hz"], 8000)
        self.assertEqual(audio_manifest["audio_metadata"]["channels"], 1)

        service = BundleService(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            runtime_db_path=self.runtime_db_path,
        )
        bundles = service.list_bundles()
        self.assertEqual(len(completed_batches), 1)
        batch_id = completed_batches[0]
        batch_bundles = service.list_bundles(batch_id=batch_id)
        self.assertEqual(len(batch_bundles), 2)
        self.assertEqual(service.get_latest_batch()["id"], batch_id)
        self.assertFalse(service.list_bundles(status="ready"))

        image_bundle = next(item for item in batch_bundles if item["type"] == "image")
        ai_worker = AIBatchWorker(batch_id, [image_bundle], self.assets_dir)
        ai_worker.style_classifier = _HeroClassifier()
        ai_worker.bundle_service = service
        ai_finished: list[tuple[str, str, bool]] = []
        ai_worker.finished_signal.connect(
            lambda completed_id, message, ok: ai_finished.append((completed_id, message, ok))
        )
        ai_worker.run()

        self.assertEqual(ai_finished[-1][0], batch_id)
        self.assertTrue(ai_finished[-1][2])
        ready = service.list_bundles(status="ready")
        handoff = build_handoff(ready)
        payload_text = json.dumps(handoff)

        self.assertEqual(len(bundles), 2)
        self.assertEqual(len(ready), 1)
        self.assertTrue(any(item["role"] == "hero_image" for item in ready))
        pending_audio = next(item for item in bundles if item["type"] == "audio")
        self.assertEqual(pending_audio["status"], "pending")
        self.assertEqual(pending_audio["audio_usage"], "unknown")
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

    def test_scanner_registers_m4a_and_skips_corrupt_audio(self) -> None:
        valid = self.assets_dir / "generic/audio/clip.m4a"
        corrupt = self.assets_dir / "generic/audio/broken.flac"
        valid.parent.mkdir(parents=True)
        valid.write_bytes(b"m4a")
        corrupt.write_bytes(b"broken")

        class FakeAudio:
            class info:
                length = 2.5
                sample_rate = 48_000
                channels = 2
                bitrate = 128_000

            def __init__(self) -> None:
                self.tags = {
                    "TIT2": type("Tag", (), {"text": ["Pika Call"]})(),
                    "TPE1": type("Tag", (), {"text": ["Winter Ridge"]})(),
                    "TALB": type("Tag", (), {"text": ["Haypile"]})(),
                }

        with patch(
            "app.services.scanner.MutagenFile",
            side_effect=lambda path: FakeAudio() if Path(path).suffix == ".m4a" else None,
        ):
            manifest = AssetScanner(self.assets_dir, self.manifest_path)._scan_assets_directory_sync()

        item = manifest["generic/audio/clip.m4a"]
        self.assertEqual(item["duration_seconds"], 2.5)
        self.assertEqual(item["audio_metadata"], {"bitrate_bps": 128000, "sample_rate_hz": 48000, "channels": 2})
        self.assertEqual(item["audio_tags"], {"title": "Pika Call", "artist": "Winter Ridge", "album": "Haypile"})
        self.assertNotIn("generic/audio/broken.flac", manifest)

    def test_ingest_reuses_verified_runtime_hash_index(self) -> None:
        existing = self.assets_dir / "generic/images/known.svg"
        existing.parent.mkdir(parents=True)
        existing.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>', encoding="utf-8")
        StorageRuntimeDB(self.runtime_db_path).record_link(
            sha256_hex="known-sha",
            src_path=existing,
            dst_path=existing,
            strategy="copy",
        )
        worker = IngestWorker([], self.assets_dir, ai_enabled=False)

        with patch.object(worker, "_compute_sha256", side_effect=AssertionError("verified assets should not be rehashed")):
            index = worker._build_hash_index()

        self.assertEqual(index, {"known-sha": existing.resolve()})

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

    def test_ai_failure_does_not_undo_ingest_or_mark_asset_ready(self) -> None:
        image = self.tmpdir / "pending.svg"
        image.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800"></svg>',
            encoding="utf-8",
        )
        batches: list[str] = []
        worker = IngestWorker([image], self.assets_dir, ai_enabled=True)
        worker.batch_signal.connect(lambda batch_id, _summary: batches.append(batch_id))
        worker.run()

        service = BundleService(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            runtime_db_path=self.runtime_db_path,
        )
        bundle = service.list_bundles(batch_id=batches[0])[0]
        ai_worker = AIBatchWorker(batches[0], [bundle], self.assets_dir)
        ai_worker.style_classifier = _FailingClassifier()
        ai_worker.bundle_service = service
        ai_worker.run()

        after = service.get_bundle(bundle["id"])
        self.assertIsNotNone(after)
        self.assertEqual(after["status"], "pending")
        self.assertEqual(after["role"], "unknown")
        self.assertEqual(after["ai_suggestions"]["reason"], "model_call_failed")

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
            source="model",
            reason="user_flow_smoke",
            quality="high",
            quality_reason="scalable_vector",
        )


class _ExplodingClassifier:
    async def classify_image(self, _image_path: Path, candidate_themes: list[str] | None = None) -> StyleClassificationResult:
        raise AssertionError("AI classifier should not be called")


class _FailingClassifier:
    async def classify_image(self, _image_path: Path, candidate_themes: list[str] | None = None):
        raise RuntimeError("offline")


if __name__ == "__main__":
    unittest.main()
