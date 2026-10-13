from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from app.core.config import Settings
from app.core.file_lock import InterProcessFileLock
from app.services.ai_provider import AIProviderConfig
from app.services.asset_provenance import read_asset_provenance, write_asset_provenance
from app.services.bundle_service import BundleService
from app.services.style_classifier import StyleClassificationResult, StyleClassifier
from app.services.theme_registry import ThemeRegistry
from app_gui import AIRefreshWorker, AIBatchWorker, MaterialPanelWindow, _classify_registered_bundle


class AIStabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = Settings(_env_file=None, STORAGE_DIR=root,
            ASSETS_DIR=root / "assets", INDEX_DIR=root / "index", THEMES_DIR=root / "themes",
            MANIFEST_PATH=root / "index/assets_manifest.json", VISION_CLASSIFIER_TIMEOUT_SECONDS=30,
            VISION_FALLBACK_THEME="private_theme")
        self.path = self.settings.ASSETS_DIR / "generic/images/sample.png"
        self.path.parent.mkdir(parents=True)
        Image.new("RGB", (512, 512), "red").save(self.path)
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.settings.INDEX_DIR.mkdir()
        self.settings.MANIFEST_PATH.write_text(json.dumps({"generic/images/sample.png": {
            "type": "image", "content_type": "image/png", "sha256": digest,
            "url_path": "/static/generic/images/sample.png"}}))
        self.service = BundleService(assets_dir=self.settings.ASSETS_DIR,
            manifest_path=self.settings.MANIFEST_PATH, themes_dir=self.settings.THEMES_DIR,
            runtime_db_path=self.settings.INDEX_DIR / "storage_runtime.db")
        ThemeRegistry(self.settings.THEMES_DIR).upsert_image_asset(theme_id="generic",
            asset_key="sample", asset_url="/static/generic/images/sample.png", role="unknown")
        self.bundle = self.service.get_bundle(digest)
        self.result = StyleClassificationResult(theme_id="generic", theme_confidence=0,
            role_confidence=.95, role="hero_image", source="model", reason="usable",
            quality="high", tags=["sunset"], agent_summary="Warm landscape")

    def classify(self, classifier, should_stop=lambda: False):
        return asyncio.run(_classify_registered_bundle(self.bundle, self.settings.ASSETS_DIR,
            classifier, ["generic"], self.service, should_stop))

    def test_normal_model_fallbacks_keep_prior_success(self):
        classifier = StyleClassifier(AIProviderConfig(mode="local"), settings=self.settings)
        for raw in ("", "not JSON", "{}", '{"role":"logo","role_confidence":0.9}'):
            with self.subTest(raw=raw):
                write_asset_provenance(self.path, {"origin_url": "https://example.org/source",
                    "ai_suggestions": self.result.ai_suggestions()})
                before = read_asset_provenance(self.path)
                with patch.object(classifier, "_call_model", AsyncMock(return_value=(raw, {}))):
                    result, ready = self.classify(classifier)
                self.assertEqual(result.source, "model_fallback")
                self.assertFalse(ready)
                self.assertEqual(read_asset_provenance(self.path), before)
                self.assertEqual(self.service.get_bundle(self.bundle["id"])["role"], "unknown")

    def test_manual_change_during_inference_wins_but_keeps_advice(self):
        async def finish(*args, **kwargs):
            self.service.set_bundle_role(self.bundle["id"], "logo")
            return self.result
        _, ready = self.classify(SimpleNamespace(classify_image=finish))
        self.assertFalse(ready)
        self.assertEqual(self.service.get_bundle(self.bundle["id"])["role"], "logo")
        self.assertEqual(read_asset_provenance(self.path)["ai_suggestions"]["tags"], ["sunset"])

    def test_local_model_response_schema_before_accepting_advice(self):
        classifier = StyleClassifier(AIProviderConfig(mode="local"), settings=self.settings)
        valid = {"role": "hero_image", "role_confidence": .9, "reason": "主体清晰",
                 "tags": ["红色", "主体"], "agent_summary": "适合主视觉"}
        cases = [(valid, "model"), ({**valid, "role": "unknown", "role_confidence": 0}, "model")]
        cases.extend(({**valid, **changed}, "model_fallback") for changed in (
            {"role": "not-a-role"}, {"role": ["logo"]},
            {"role_confidence": True}, {"role_confidence": "0.9"},
            {"role_confidence": 95}, {"role_confidence": -1},
            {"role_confidence": 10 ** 400}, {"role_confidence": float("nan")},
            {"reason": {}}, {"agent_summary": []},
            {"tags": "red"}, {"tags": [1]}, {"tags": ["tag"] * 7},
            {"unexpected": "field"},
        ))
        for payload, expected_source in cases:
            with self.subTest(payload=payload):
                with patch.object(classifier, "_call_model", AsyncMock(return_value=(json.dumps(payload), {}))):
                    result = asyncio.run(classifier.classify_image(self.path))
                self.assertEqual(result.source, expected_source)
                if expected_source == "model_fallback":
                    self.assertEqual(result.role, "unknown")
                    self.assertEqual(result.role_confidence, 0)

        for transport in ("openai", "sophon"):
            with self.subTest(transport=transport):
                classifier.transport = transport
                legacy = '{"role":"logo","role_confidence":"0.9"}'
                with patch.object(classifier, "_call_model", AsyncMock(return_value=(legacy, {}))):
                    result = asyncio.run(classifier.classify_image(self.path))
                self.assertEqual(result.source, "model")
                self.assertEqual(result.role, "logo")
                self.assertEqual(result.role_confidence, .9)

    def test_high_confidence_advice_never_confirms_usage(self):
        _, ready = self.classify(SimpleNamespace(classify_image=AsyncMock(return_value=self.result)))
        self.assertFalse(ready)
        bundle = self.service.get_bundle(self.bundle["id"])
        self.assertEqual(bundle["status"], "pending")
        self.assertEqual(bundle["ai_suggestions"]["usage"], "hero_image")
        self.assertEqual(self.service.list_bundles(status="ready"), [])
        self.service.set_bundle_role(self.bundle["id"], "logo")
        self.assertEqual(self.service.get_bundle(self.bundle["id"])["status"], "ready")

    def test_cancelled_or_changed_inference_does_not_persist(self):
        for mode in ("cancel", "change"):
            with self.subTest(mode=mode):
                cancelled = [False]
                async def finish(*args, **kwargs):
                    if mode == "cancel":
                        cancelled[0] = True
                    else:
                        Image.new("RGB", (512, 512), "blue").save(self.path)
                    return self.result
                with self.assertRaises(InterruptedError if mode == "cancel" else OSError):
                    self.classify(SimpleNamespace(classify_image=finish), lambda: cancelled[0])
                self.assertEqual(read_asset_provenance(self.path), {})
                self.assertEqual(self.service.get_bundle(self.bundle["id"])["role"], "unknown")

    def test_cancellation_with_model_exception_is_cancelled_for_both_workers(self):
        for worker_type in (AIRefreshWorker, AIBatchWorker):
            with self.subTest(worker=worker_type.__name__):
                args = (self.bundle,) if worker_type is AIRefreshWorker else ("batch", [self.bundle])
                worker = worker_type(*args, self.settings.ASSETS_DIR, settings=self.settings)
                cancelled = [False]
                async def fail(*args, **kwargs):
                    cancelled[0] = True
                    raise httpx.TimeoutException("timeout")
                worker.style_classifier = SimpleNamespace(classify_image=fail)
                signals = []
                worker.finished_signal.connect(lambda *values: signals.append(values))
                with patch.object(worker, "isInterruptionRequested", side_effect=lambda: cancelled[0]):
                    worker.run()
                self.assertIn("取消", signals[-1][1])
                self.assertEqual(read_asset_provenance(self.path), {})

    def test_cancellation_during_failure_persistence_reports_cancelled(self):
        for worker_type in (AIRefreshWorker, AIBatchWorker):
            with self.subTest(worker=worker_type.__name__):
                args = (self.bundle,) if worker_type is AIRefreshWorker else ("batch", [self.bundle])
                worker = worker_type(*args, self.settings.ASSETS_DIR, settings=self.settings)
                cancelled = [False]
                worker.style_classifier = SimpleNamespace(classify_image=AsyncMock(side_effect=RuntimeError("offline")))
                signals = []
                worker.finished_signal.connect(lambda *values: signals.append(values))
                def cancel(*args, **kwargs):
                    cancelled[0] = True
                with patch.object(worker, "isInterruptionRequested", side_effect=lambda: cancelled[0]), patch(
                        "app_gui._persist_ai_failure", side_effect=cancel):
                    worker.run()
                self.assertIn("取消", signals[-1][1])
                self.assertEqual(read_asset_provenance(self.path), {})

    def test_worker_settings_do_not_touch_global_store(self):
        with patch("app_gui.get_settings", side_effect=AssertionError("global settings used")):
            worker = AIRefreshWorker(self.bundle, self.settings.ASSETS_DIR, settings=self.settings)
        self.assertEqual(worker.bundle_service.manifest_path, self.settings.MANIFEST_PATH)
        self.assertEqual(worker.theme_registry.themes_dir, self.settings.THEMES_DIR)
        self.assertIn("private_theme", worker.theme_candidates)
        self.assertEqual(worker.style_classifier.timeout_seconds, 30)

    def test_unsupported_types_have_no_retry_and_do_not_enter_batch(self):
        button = Mock()
        panel = SimpleNamespace(retry_ai_button=button, ai_refresh_worker=None)
        for suffix, kind in ((".svg", "image"), (".gif", "image"), (".woff2", "font"), (".mp3", "audio"),
                             (".png", "image"), (".jpg", "image"), (".webp", "image")):
            with self.subTest(suffix=suffix):
                bundle = {**self.bundle, "type": kind, "source_key": "sample" + suffix}
                supported = suffix in {".png", ".jpg", ".webp"}
                worker = AIBatchWorker("batch", [bundle], self.settings.ASSETS_DIR, settings=self.settings)
                self.assertEqual(bool(worker.bundles), supported)
                MaterialPanelWindow._refresh_retry_ai_button(panel, bundle)
                button.setVisible.assert_called_with(supported)

    def test_existing_keyword_search_uses_ai_tags_and_summary(self):
        bundle = {**self.bundle, "ai_suggestions": self.result.ai_suggestions()}
        panel = SimpleNamespace(_bundle_for_item=lambda _: bundle,
            **{name: lambda _: "" for name in ("_display_role_label", "_asset_type_label",
                "_bundle_status_label", "_agent_usability_label", "_role_label")})
        item = SimpleNamespace(**{name: "" for name in ("title", "usage_label", "asset_type",
            "status_label", "preview_url", "theme_id", "source_key", "origin_url")})
        self.assertTrue(MaterialPanelWindow._item_matches_query(panel, item, "sunset warm"))
        self.assertFalse(MaterialPanelWindow._item_matches_query(panel, item, "sunset ocean"))

    def test_completed_inference_waits_for_intake_lock(self):
        lock = InterProcessFileLock(self.settings.INDEX_DIR / "ingest.transaction.lock")
        self.assertTrue(lock.acquire(timeout=0))
        timer = threading.Timer(.1, lock.release)
        timer.start()
        try:
            _, ready = self.classify(SimpleNamespace(classify_image=AsyncMock(return_value=self.result)))
        finally:
            timer.join()
            lock.release()
        self.assertFalse(ready)
        self.assertEqual(self.service.get_bundle(self.bundle["id"])["role"], "unknown")
        self.assertEqual(read_asset_provenance(self.path)["ai_suggestions"]["source"], "model")

    def test_model_exception_retains_previous_success(self):
        write_asset_provenance(self.path, {"ai_suggestions": self.result.ai_suggestions()})
        before = read_asset_provenance(self.path)
        worker = AIRefreshWorker(self.bundle, self.settings.ASSETS_DIR, settings=self.settings)
        worker.style_classifier = SimpleNamespace(classify_image=AsyncMock(side_effect=RuntimeError("offline")))
        signals = []
        worker.finished_signal.connect(lambda *values: signals.append(values))
        worker.run()
        self.assertFalse(signals[-1][2])
        self.assertEqual(read_asset_provenance(self.path), before)

    def test_default_budget_and_explicit_override(self):
        self.assertEqual(Settings.model_fields["VISION_CLASSIFIER_TIMEOUT_SECONDS"].default, 30)
        settings = self.settings.model_copy(update={"VISION_CLASSIFIER_TIMEOUT_SECONDS": 12.5})
        self.assertEqual(StyleClassifier(settings=settings).timeout_seconds, 12.5)

    def test_cancel_at_persistence_checkpoints_does_not_write(self):
        for checkpoint in ("lock", "provenance"):
            with self.subTest(checkpoint=checkpoint):
                cancelled = [False]
                original = InterProcessFileLock.acquire
                def acquire(lock, timeout=8):
                    result = original(lock, timeout)
                    if checkpoint == "lock":
                        cancelled[0] = True
                    return result
                def read(path):
                    if checkpoint == "provenance":
                        cancelled[0] = True
                    return read_asset_provenance(path)
                with patch.object(InterProcessFileLock, "acquire", new=acquire), patch(
                        "app_gui.read_asset_provenance", side_effect=read):
                    with self.assertRaises(InterruptedError):
                        self.classify(SimpleNamespace(classify_image=AsyncMock(return_value=self.result)), lambda: cancelled[0])
                self.assertEqual(read_asset_provenance(self.path), {})
                contender = InterProcessFileLock(self.settings.INDEX_DIR / "ingest.transaction.lock")
                self.assertTrue(contender.acquire(timeout=0))
                contender.release()


if __name__ == "__main__":
    unittest.main()
