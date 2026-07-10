from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("IPC_AUTHKEY", "test-ipc-authkey")
    from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent, QMouseEvent, QPixmap
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import app_gui as app_gui_module
    from app.services.asset_provenance import read_asset_provenance, write_asset_provenance
    from app.services.material_summary import MaterialPanelSummary, MaterialSummaryItem
    from app.services.style_classifier import StyleClassificationResult
    from app_gui import MaterialPanelWindow, QuickMenuWindow
except ImportError as exc:  # pragma: no cover - depends on optional GUI runtime
    QApplication = None
    MaterialPanelSummary = None
    MaterialPanelWindow = None
    QuickMenuWindow = None
    app_gui_module = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(_IMPORT_ERROR is not None, f"GUI runtime unavailable: {_IMPORT_ERROR}")
class GuiRealProjectConfirmationActionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.previous_root = os.environ.get("HAYPILE_REAL_PROJECT_ROOT")
        self.previous_haypile_picker_preview_path = os.environ.get("HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH")
        self.previous_haypile_gui_backend_start = os.environ.get("HAYPILE_GUI_ALLOW_BACKEND_START")
        self.previous_haypile_ui_lang = os.environ.get("HAYPILE_UI_LANG")
        self.previous_experimental_project_apply = os.environ.get("HAYPILE_ENABLE_EXPERIMENTAL_PROJECT_APPLY")
        os.environ["HAYPILE_UI_LANG"] = "zh"
        os.environ["HAYPILE_ENABLE_EXPERIMENTAL_PROJECT_APPLY"] = "1"
        os.environ.pop("HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH", None)

    def tearDown(self) -> None:
        if self.previous_root is None:
            os.environ.pop("HAYPILE_REAL_PROJECT_ROOT", None)
        else:
            os.environ["HAYPILE_REAL_PROJECT_ROOT"] = self.previous_root
        if self.previous_haypile_picker_preview_path is None:
            os.environ.pop("HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH", None)
        else:
            os.environ["HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH"] = self.previous_haypile_picker_preview_path
        if self.previous_haypile_gui_backend_start is None:
            os.environ.pop("HAYPILE_GUI_ALLOW_BACKEND_START", None)
        else:
            os.environ["HAYPILE_GUI_ALLOW_BACKEND_START"] = self.previous_haypile_gui_backend_start
        if self.previous_haypile_ui_lang is None:
            os.environ.pop("HAYPILE_UI_LANG", None)
        else:
            os.environ["HAYPILE_UI_LANG"] = self.previous_haypile_ui_lang
        if self.previous_experimental_project_apply is None:
            os.environ.pop("HAYPILE_ENABLE_EXPERIMENTAL_PROJECT_APPLY", None)
        else:
            os.environ["HAYPILE_ENABLE_EXPERIMENTAL_PROJECT_APPLY"] = self.previous_experimental_project_apply
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_confirmation_buttons_reapply_then_rollback_temp_project(self) -> None:
        project_root, _source_root, written_files = self._write_project(state="rolled_back")
        os.environ["HAYPILE_REAL_PROJECT_ROOT"] = project_root.as_posix()
        panel = MaterialPanelWindow()

        panel.refresh()
        self.assertEqual(panel.project_label.text(), "● signal-pool-demo")
        self.assertEqual(panel.project_label.toolTip(), project_root.resolve(strict=False).as_posix())
        self.assertTrue(panel.project_label.isHidden())
        self.assertEqual(panel.rehearsal_label.text(), "已撤回 · 投放 5")
        self.assertTrue(panel.rehearsal_label.isHidden())
        self.assertTrue(panel._confirmation_available)
        self.assertEqual(panel.confirmation_preview.title.text(), "重新投放？")
        self.assertEqual(panel.confirmation_preview.body.text(), "signal-pool-demo")
        self.assertEqual(panel.confirmation_preview.summary.text(), "5 项")
        self.assertEqual(panel.confirmation_preview.warning.text(), "再次确认后执行")
        self.assertEqual(panel.confirmation_preview.primary_button.text(), "重新投放")

        panel.confirmation_preview.primary_button.click()
        self.app.processEvents()
        self.assertEqual(panel.confirmation_preview.title.text(), "再次确认？")
        for path_ref in written_files:
            self.assertFalse((project_root / path_ref).exists())

        panel.confirmation_preview.primary_button.click()
        self.app.processEvents()
        self.assertEqual(panel.confirmation_preview.title.text(), "已重新投放")
        for path_ref in written_files:
            self.assertTrue((project_root / path_ref).is_file())

        panel.refresh()
        self.assertEqual(panel.confirmation_preview.primary_button.text(), "撤回投放")
        panel.confirmation_preview.primary_button.click()
        panel.confirmation_preview.primary_button.click()
        self.app.processEvents()

        self.assertEqual(panel.confirmation_preview.title.text(), "已撤回")
        for path_ref in written_files:
            self.assertFalse((project_root / path_ref).exists())

    def test_project_picker_handoff_refresh_populates_existing_confirmation_preview(self) -> None:
        project_root = self.tmpdir / "signal-pool-demo"
        summary = MaterialPanelSummary(
            total_count=0,
            recognized_count=0,
            pending_count=0,
            service_status="Haypile：运行中",
            recognition_status="识别服务：可用",
            real_project_root=project_root.as_posix(),
            project_display_label="● signal-pool-demo",
            project_display_state="rolled_back",
            panel_display_text="真实项目：已回滚\n重新投放",
            confirmation_available=True,
            confirmation_action="reapply",
            confirmation_primary_label="重新投放",
            confirmation_title="重新投放？",
            confirmation_body="signal-pool-demo",
            confirmation_summary="5 项",
            confirmation_warning="再次确认后执行",
            project_picker_status_line="Project Picker：已读取 /tmp/picker.json",
            project_picker_tooltip="Project Picker preview",
        )
        previous_builder = app_gui_module.build_material_panel_summary
        app_gui_module.build_material_panel_summary = lambda: summary
        panel = MaterialPanelWindow()
        try:
            panel.refresh()

            self.assertEqual(panel.project_label.text(), "● signal-pool-demo")
            self.assertEqual(panel.project_label.toolTip(), project_root.as_posix())
            self.assertTrue(panel.project_label.isHidden())
            self.assertEqual(panel.rehearsal_label.text(), "真实项目：已回滚\n重新投放")
            self.assertEqual(panel.rehearsal_label.toolTip(), "Project Picker preview")
            self.assertTrue(panel.rehearsal_label.isHidden())
            self.assertIn("Project Picker：已读取", panel.service_label.text())
            self.assertTrue(panel.service_label.isHidden())
            self.assertTrue(panel._confirmation_available)
            self.assertEqual(panel.confirmation_preview.title.text(), "重新投放？")
            self.assertEqual(panel.confirmation_preview.body.text(), "signal-pool-demo")
            self.assertEqual(panel.confirmation_preview.summary.text(), "5 项")
            self.assertEqual(panel.confirmation_preview.warning.text(), "再次确认后执行")
            self.assertEqual(panel.confirmation_preview.primary_button.text(), "重新投放")
            self.assertEqual(panel.confirmation_preview._action, "reapply")
            self.assertEqual(panel.confirmation_preview._project_root, project_root.as_posix())
        finally:
            app_gui_module.build_material_panel_summary = previous_builder
            panel.confirmation_preview.close()
            panel.close()

    def test_material_panel_recent_item_copies_asset_handoff(self) -> None:
        summary = MaterialPanelSummary(
            total_count=1,
            recognized_count=1,
            pending_count=0,
            service_status="Haypile：运行中",
            recognition_status="分类：可用",
            recent_items=[
                MaterialSummaryItem(
                    title="hero.png",
                    usage_label="主视觉",
                    confidence_label="中等把握",
                    status_label="已识别",
                    preview_url="/static/generic/images/hero.png",
                    theme_id="generic",
                    asset_type="image",
                    source_key="generic/images/hero.png",
                )
            ],
        )
        previous_builder = app_gui_module.build_material_panel_summary
        previous_bundle_service = app_gui_module.BundleService
        app_gui_module.build_material_panel_summary = lambda: summary
        app_gui_module.BundleService = lambda: type(
            "FakeBundleService",
            (),
            {
                "get_bundle": lambda _self, _bundle_id: {
                    "id": "hero",
                    "theme_id": "generic",
                    "type": "image",
                    "role": "hero_image",
                    "status": "ready",
                    "sha256": "deadbeef" * 8,
                    "url": "/static/generic/images/hero.png",
                    "access": "manifest_static",
                    "source_key": "generic/images/hero.png",
                    "origin_url": "https://cdn.example.com/hero.png",
                    "content_type": "image/png",
                    "downloaded_at": "2026-07-06T00:00:00+00:00",
                    "ai_suggestions": {
                        "tags": ["主视觉"],
                        "usage": "hero_image",
                        "quality": "high",
                        "agent_summary": "适合作为主视觉。",
                    },
                }
            },
        )()
        panel = MaterialPanelWindow()
        try:
            panel.refresh()
            point = panel.item_labels[0].rect().center()
            event = QMouseEvent(QEvent.Type.MouseButtonPress, point, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)

            panel._select_recent_item(0, event)

            payload = json.loads(QApplication.clipboard().text())
            self.assertEqual(payload["source"], "haypile")
            self.assertEqual(payload["assets"][0]["id"], "hero")
            self.assertEqual(payload["assets"][0]["role"], "hero_image")
            self.assertEqual(payload["assets"][0]["status"], "ready")
            self.assertEqual(payload["assets"][0]["source_key"], "generic/images/hero.png")
            self.assertEqual(payload["assets"][0]["resolved_url"], "http://127.0.0.1:8010/static/generic/images/hero.png")
            self.assertEqual(payload["assets"][0]["ai_suggestions"]["quality"], "high")
            self.assertEqual(payload["assets"][0]["provenance"]["source_key"], "generic/images/hero.png")
            self.assertEqual(payload["assets"][0]["provenance"]["origin_url"], "https://cdn.example.com/hero.png")
            self.assertNotIn("storage/assets", json.dumps(payload))
            self.assertIn("主视觉", panel.detail_label.text())
            self.assertIn("agent 可用", panel.detail_label.text())
            self.assertIn("AI high · 主视觉 · 适合作为主视觉。", panel.detail_label.text())
            self.assertIn("origin https://cdn.example.com/hero.png", panel.detail_label.text())
            self.assertIn("provenance 已包含", panel.detail_label.text())
            self.assertIn("border: 2px solid #C8A24A", panel.item_labels[0].styleSheet())
            self.assertFalse(panel.retry_ai_button.isHidden())
        finally:
            app_gui_module.build_material_panel_summary = previous_builder
            app_gui_module.BundleService = previous_bundle_service
            panel.close()

    def test_ai_refresh_worker_updates_asset_ai_suggestions(self) -> None:
        assets_dir = self.tmpdir / "assets"
        asset_path = assets_dir / "generic" / "images" / "hero.png"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(b"image")

        class FakeClassifier:
            async def classify_image(self, _asset_path, candidate_themes=None):
                return StyleClassificationResult(
                    theme_id="generic",
                    theme_confidence=0.8,
                    role_confidence=0.7,
                    role="hero_image",
                    source="model",
                    reason="可做主视觉",
                    tags=["主视觉"],
                    quality="high",
                    agent_summary="适合作为主视觉。",
                )

        worker = app_gui_module.AIRefreshWorker(
            {
                "id": "hero",
                "type": "image",
                "source_key": "generic/images/hero.png",
                "sha256": "d" * 64,
            },
            assets_dir,
        )
        worker.style_classifier = FakeClassifier()
        finished: list[tuple[str, str, bool]] = []
        worker.finished_signal.connect(lambda bundle_id, message, success: finished.append((bundle_id, message, success)))

        worker.run()

        provenance = read_asset_provenance(asset_path)
        self.assertEqual(finished, [("hero", "AI 分拣已更新", True)])
        self.assertEqual(provenance["source_key"], "generic/images/hero.png")
        self.assertEqual(provenance["sha256"], "d" * 64)
        self.assertEqual(provenance["ai_suggestions"]["quality"], "high")
        self.assertEqual(provenance["ai_suggestions"]["agent_summary"], "适合作为主视觉。")

    def test_ai_refresh_worker_reports_model_fallback_as_failure(self) -> None:
        assets_dir = self.tmpdir / "assets"
        asset_path = assets_dir / "generic" / "images" / "hero.png"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(b"image")

        class FakeClassifier:
            async def classify_image(self, _asset_path, candidate_themes=None):
                return StyleClassificationResult(
                    theme_id="generic",
                    theme_confidence=0.0,
                    role_confidence=0.0,
                    role="unknown",
                    source="model_fallback",
                    reason="model_call_failed",
                )

        worker = app_gui_module.AIRefreshWorker(
            {
                "id": "hero",
                "type": "image",
                "source_key": "generic/images/hero.png",
                "sha256": "d" * 64,
            },
            assets_dir,
        )
        worker.style_classifier = FakeClassifier()
        finished: list[tuple[str, str, bool]] = []
        worker.finished_signal.connect(lambda bundle_id, message, success: finished.append((bundle_id, message, success)))

        worker.run()

        provenance = read_asset_provenance(asset_path)
        self.assertEqual(finished, [("hero", "AI 分拣未得到模型结果：model_call_failed", False)])
        self.assertEqual(provenance["ai_suggestions"]["source"], "model_fallback")
        self.assertEqual(provenance["ai_suggestions"]["reason"], "model_call_failed")

    def test_material_panel_retry_ai_failure_stays_visible(self) -> None:
        summary = MaterialPanelSummary(
            total_count=1,
            recognized_count=1,
            pending_count=0,
            service_status="Haypile：运行中",
            recognition_status="分类：可用",
            recent_items=[
                MaterialSummaryItem(
                    title="hero.png",
                    usage_label="主视觉",
                    confidence_label="中等把握",
                    status_label="已识别",
                    preview_url="/static/generic/images/hero.png",
                    theme_id="generic",
                    asset_type="image",
                    source_key="generic/images/hero.png",
                )
            ],
        )
        bundle = {
            "id": "hero",
            "theme_id": "generic",
            "type": "image",
            "role": "hero_image",
            "status": "ready",
            "sha256": "d" * 64,
            "url": "/static/generic/images/hero.png",
            "access": "manifest_static",
            "source_key": "generic/images/hero.png",
            "ai_suggestions": {"source": "model_fallback", "reason": "model_call_failed"},
        }

        class FakeSignal:
            def __init__(self):
                self.callback = None

            def connect(self, callback):
                self.callback = callback

            def emit(self, *args):
                self.callback(*args)

        class FakeWorker:
            def __init__(self, _bundle, _assets_dir):
                self.finished_signal = FakeSignal()

            def isRunning(self):
                return False

            def start(self):
                self.finished_signal.emit("hero", "AI 分拣未得到模型结果：model_call_failed", False)

            def deleteLater(self):
                pass

        previous_builder = app_gui_module.build_material_panel_summary
        previous_bundle_service = app_gui_module.BundleService
        previous_ai_worker = app_gui_module.AIRefreshWorker
        previous_panel_ai_enabled = MaterialPanelWindow._panel_ai_enabled
        app_gui_module.build_material_panel_summary = lambda: summary
        app_gui_module.BundleService = lambda: type("FakeBundleService", (), {"get_bundle": lambda _self, _id: dict(bundle)})()
        app_gui_module.AIRefreshWorker = FakeWorker
        MaterialPanelWindow._panel_ai_enabled = staticmethod(lambda: True)
        panel = MaterialPanelWindow()
        toasts: list[tuple[str, bool]] = []
        panel.set_toast_handler(lambda message, success=True: toasts.append((message, success)))
        try:
            panel.refresh()
            event = QMouseEvent(QEvent.Type.MouseButtonPress, panel.item_labels[0].rect().center(), panel.item_labels[0].rect().center(), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            panel._select_recent_item(0, event)

            panel.retry_ai_button.click()

            self.assertIn("model_call_failed", panel.detail_label.text())
            self.assertIn("AI 分拣未得到模型结果：model_call_failed", panel.detail_label.text())
            self.assertTrue(panel.retry_ai_button.isEnabled())
            self.assertEqual(toasts, [("AI 分拣未得到模型结果：model_call_failed", False)])
        finally:
            app_gui_module.build_material_panel_summary = previous_builder
            app_gui_module.BundleService = previous_bundle_service
            app_gui_module.AIRefreshWorker = previous_ai_worker
            MaterialPanelWindow._panel_ai_enabled = previous_panel_ai_enabled
            panel.close()

    def test_material_panel_retry_ai_reenables_current_selection_after_switch(self) -> None:
        summary = MaterialPanelSummary(
            total_count=2,
            recognized_count=2,
            pending_count=0,
            service_status="Haypile：运行中",
            recognition_status="分类：可用",
            recent_items=[
                MaterialSummaryItem("hero.png", "主视觉", "中等把握", "已识别", "/static/generic/images/hero.png", "generic", "image", "generic/images/hero.png"),
                MaterialSummaryItem("icon.png", "图标", "中等把握", "已识别", "/static/generic/images/icon.png", "generic", "image", "generic/images/icon.png"),
            ],
        )
        bundles = {
            "hero": {"id": "hero", "theme_id": "generic", "type": "image", "role": "hero_image", "status": "ready", "sha256": "a" * 64, "url": "/static/generic/images/hero.png", "access": "manifest_static", "source_key": "generic/images/hero.png"},
            "icon": {"id": "icon", "theme_id": "generic", "type": "image", "role": "icon", "status": "ready", "sha256": "b" * 64, "url": "/static/generic/images/icon.png", "access": "manifest_static", "source_key": "generic/images/icon.png"},
        }

        class FakeSignal:
            def __init__(self):
                self.callback = None

            def connect(self, callback):
                self.callback = callback

            def emit(self, *args):
                self.callback(*args)

        class FakeWorker:
            created = []

            def __init__(self, _bundle, _assets_dir):
                self.finished_signal = FakeSignal()
                self.running = False
                FakeWorker.created.append(self)

            def isRunning(self):
                return self.running

            def start(self):
                self.running = True

            def deleteLater(self):
                self.running = False

        previous_builder = app_gui_module.build_material_panel_summary
        previous_bundle_service = app_gui_module.BundleService
        previous_ai_worker = app_gui_module.AIRefreshWorker
        previous_panel_ai_enabled = MaterialPanelWindow._panel_ai_enabled
        app_gui_module.build_material_panel_summary = lambda: summary
        app_gui_module.BundleService = lambda: type("FakeBundleService", (), {"get_bundle": lambda _self, bundle_id: dict(bundles[bundle_id])})()
        app_gui_module.AIRefreshWorker = FakeWorker
        MaterialPanelWindow._panel_ai_enabled = staticmethod(lambda: True)
        panel = MaterialPanelWindow()
        try:
            panel.refresh()
            event0 = QMouseEvent(QEvent.Type.MouseButtonPress, panel.item_labels[0].rect().center(), panel.item_labels[0].rect().center(), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            panel._select_recent_item(0, event0)
            panel.retry_ai_button.click()
            self.assertFalse(panel.retry_ai_button.isEnabled())

            event1 = QMouseEvent(QEvent.Type.MouseButtonPress, panel.item_labels[1].rect().center(), panel.item_labels[1].rect().center(), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            panel._select_recent_item(1, event1)
            self.assertFalse(panel.retry_ai_button.isEnabled())

            FakeWorker.created[-1].finished_signal.emit("hero", "AI 分拣已更新", True)

            self.assertEqual(panel._selected_bundle_id, "icon")
            self.assertTrue(panel.retry_ai_button.isEnabled())
            self.assertEqual(panel.retry_ai_button.text(), "重新 AI 分拣")
        finally:
            app_gui_module.build_material_panel_summary = previous_builder
            app_gui_module.BundleService = previous_bundle_service
            app_gui_module.AIRefreshWorker = previous_ai_worker
            MaterialPanelWindow._panel_ai_enabled = previous_panel_ai_enabled
            panel.close()

    def test_material_panel_empty_state_invites_dragging_assets(self) -> None:
        summary = MaterialPanelSummary(
            total_count=0,
            recognized_count=0,
            pending_count=0,
            service_status="Haypile：等待入库",
            recognition_status="分类：可用",
        )
        previous_builder = app_gui_module.build_material_panel_summary
        app_gui_module.build_material_panel_summary = lambda: summary
        panel = MaterialPanelWindow()
        try:
            panel.refresh()

            self.assertEqual(panel.detail_label.text(), "拖入图片或音频开始收纳")
            self.assertFalse(panel.copy_ready_button.isVisible())
        finally:
            app_gui_module.build_material_panel_summary = previous_builder
            panel.close()

    def test_material_panel_uses_english_labels_when_requested(self) -> None:
        os.environ["HAYPILE_UI_LANG"] = "en"
        summary = MaterialPanelSummary(
            total_count=0,
            recognized_count=0,
            pending_count=0,
            service_status="Haypile: running",
            recognition_status="Classifier: ready",
        )
        previous_builder = app_gui_module.build_material_panel_summary
        app_gui_module.build_material_panel_summary = lambda: summary
        panel = MaterialPanelWindow()
        try:
            panel.refresh()

            self.assertEqual(panel.title.text(), "Haypile")
            self.assertEqual(panel.filter_buttons["all"].text(), "All")
            self.assertEqual(panel.filter_buttons["pending"].text(), "Pending")
            self.assertEqual(panel.filter_buttons["image"].text(), "Images")
            self.assertEqual(panel.search_input.placeholderText(), "Search file, role, status")
            self.assertEqual(panel.detail_label.text(), "Drop images or audio to start storing")
        finally:
            app_gui_module.build_material_panel_summary = previous_builder
            panel.close()

    def test_ui_language_prefers_macos_apple_languages_over_locale(self) -> None:
        os.environ.pop("HAYPILE_UI_LANG", None)
        os.environ["LANG"] = "en_US.UTF-8"
        previous_cache = app_gui_module._UI_LANGUAGE_CACHE
        previous_macos_language = app_gui_module._macos_apple_language
        app_gui_module._UI_LANGUAGE_CACHE = None
        app_gui_module._macos_apple_language = lambda: "zh"
        try:
            self.assertEqual(app_gui_module.ui_language(), "zh")
        finally:
            app_gui_module._UI_LANGUAGE_CACHE = previous_cache
            app_gui_module._macos_apple_language = previous_macos_language

    def test_material_panel_shows_pending_audio_and_missing_statuses(self) -> None:
        summary = MaterialPanelSummary(
            total_count=2,
            recognized_count=1,
            pending_count=1,
            service_status="Haypile：运行中",
            recognition_status="分类：有待确认",
            recent_items=[
                MaterialSummaryItem(
                    title="voice.mp3",
                    usage_label="未确定",
                    confidence_label="低把握",
                    status_label="待确认",
                    preview_url="/static/generic/audio/voice.mp3",
                    theme_id="generic",
                    asset_type="audio",
                    source_key="generic/audio/voice.mp3",
                ),
                MaterialSummaryItem(
                    title="missing.png",
                    usage_label="图标",
                    confidence_label="中等把握",
                    status_label="已识别",
                    preview_url="/static/generic/images/missing.png",
                    theme_id="generic",
                    asset_type="image",
                    source_key="generic/images/missing.png",
                ),
            ],
        )
        bundles = {
            "voice": {
                "id": "voice",
                "theme_id": "generic",
                "type": "audio",
                "role": "unknown",
                "status": "pending",
                "sha256": "",
                "url": "/static/generic/audio/voice.mp3",
                "access": "manifest_static",
                "source_key": "generic/audio/voice.mp3",
            },
            "missing": {
                "id": "missing",
                "theme_id": "generic",
                "type": "image",
                "role": "icon",
                "status": "missing",
                "sha256": "",
                "url": "/static/generic/images/missing.png",
                "access": "manifest_static",
                "source_key": "generic/images/missing.png",
            },
        }
        previous_builder = app_gui_module.build_material_panel_summary
        previous_bundle_service = app_gui_module.BundleService
        app_gui_module.build_material_panel_summary = lambda: summary
        app_gui_module.BundleService = lambda: type(
            "FakeBundleService",
            (),
            {"get_bundle": lambda _self, bundle_id: bundles[bundle_id]},
        )()
        panel = MaterialPanelWindow()
        toasts: list[tuple[str, bool]] = []
        panel.set_toast_handler(lambda message, success=True: toasts.append((message, success)))
        try:
            panel.refresh()
            self.assertIn("音频 · 未确定 · 需确认后给 agent", panel.item_labels[0].text())
            self.assertIn("图片 · 图标 · agent 不可用", panel.item_labels[1].text())

            point = panel.item_labels[0].rect().center()
            event = QMouseEvent(QEvent.Type.MouseButtonPress, point, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            panel._select_recent_item(0, event)

            self.assertIn("音频资源", panel.preview_label.text())
            self.assertIn("key generic/audio/voice.mp3", panel.detail_label.text())
            self.assertIn("url /static/generic/audio/voice.mp3", panel.detail_label.text())
            self.assertIn("需确认后给 agent", panel.detail_label.text())
            self.assertIn("已复制 handoff", panel.detail_label.text())
            self.assertIn("border: 2px solid #C8A24A", panel.item_labels[0].styleSheet())
            self.assertNotIn("border: 2px solid #C8A24A", panel.item_labels[1].styleSheet())
            self.assertEqual(toasts, [])
        finally:
            app_gui_module.build_material_panel_summary = previous_builder
            app_gui_module.BundleService = previous_bundle_service
            panel.close()

    def test_material_panel_filters_and_searches_recent_items(self) -> None:
        summary = MaterialPanelSummary(
            total_count=3,
            recognized_count=2,
            pending_count=1,
            service_status="Haypile：运行中",
            recognition_status="分类：有待确认",
            recent_items=[
                MaterialSummaryItem(
                    title="hero.png",
                    usage_label="主视觉",
                    confidence_label="中等把握",
                    status_label="已识别",
                    preview_url="/static/generic/images/hero.png",
                    theme_id="generic",
                    asset_type="image",
                    source_key="generic/images/hero.png",
                ),
                MaterialSummaryItem(
                    title="voice.mp3",
                    usage_label="未确定",
                    confidence_label="低把握",
                    status_label="待确认",
                    preview_url="/static/generic/audio/voice.mp3",
                    theme_id="generic",
                    asset_type="audio",
                    source_key="generic/audio/voice.mp3",
                ),
                MaterialSummaryItem(
                    title="icon.png",
                    usage_label="图标",
                    confidence_label="中等把握",
                    status_label="已识别",
                    preview_url="/static/generic/images/icon.png",
                    theme_id="generic",
                    asset_type="image",
                    source_key="generic/images/icon.png",
                ),
            ],
        )
        bundles = {
            "hero": {"id": "hero", "status": "ready", "type": "image", "role": "hero_image"},
            "voice": {"id": "voice", "status": "pending", "type": "audio", "role": "unknown"},
            "icon": {"id": "icon", "status": "ready", "type": "image", "role": "icon"},
        }
        previous_builder = app_gui_module.build_material_panel_summary
        previous_bundle_service = app_gui_module.BundleService
        app_gui_module.build_material_panel_summary = lambda: summary
        app_gui_module.BundleService = lambda: type(
            "FakeBundleService",
            (),
            {"get_bundle": lambda _self, bundle_id: {**bundles[bundle_id], "theme_id": "generic", "sha256": "", "url": "", "access": "manifest_static", "source_key": ""}},
        )()
        panel = MaterialPanelWindow()
        try:
            panel.refresh()
            self.assertIn("hero.png", panel.item_labels[0].text())

            panel._set_filter_mode("pending")
            self.assertIn("voice.mp3", panel.item_labels[0].text())
            self.assertTrue(panel.item_labels[1].isHidden())

            panel._set_filter_mode("audio")
            self.assertIn("voice.mp3", panel.item_labels[0].text())

            panel._set_filter_mode("all")
            panel.search_input.setText("icon")
            self.assertIn("icon.png", panel.item_labels[0].text())
            self.assertTrue(panel.item_labels[1].isHidden())

            panel.search_input.setText("主视觉")
            self.assertIn("hero.png", panel.item_labels[0].text())
            self.assertTrue(panel.item_labels[1].isHidden())

            panel.search_input.setText("可用")
            self.assertIn("hero.png", panel.item_labels[0].text())
            self.assertIn("icon.png", panel.item_labels[1].text())
            self.assertTrue(panel.item_labels[2].isHidden())

            panel.search_input.setText("不存在")
            self.assertEqual(panel.detail_label.text(), "没有匹配资源")
            self.assertTrue(panel.item_labels[0].isHidden())
        finally:
            app_gui_module.build_material_panel_summary = previous_builder
            app_gui_module.BundleService = previous_bundle_service
            panel.close()

    def test_material_panel_can_update_selected_bundle_role(self) -> None:
        summary = MaterialPanelSummary(
            total_count=1,
            recognized_count=0,
            pending_count=1,
            service_status="Haypile：运行中",
            recognition_status="分类：有待确认",
            recent_items=[
                MaterialSummaryItem(
                    title="unknown.png",
                    usage_label="未确定",
                    confidence_label="低把握",
                    status_label="待确认",
                    preview_url="/static/generic/images/unknown.png",
                    theme_id="generic",
                    asset_type="image",
                    source_key="generic/images/unknown.png",
                )
            ],
        )
        calls: list[tuple[str, str]] = []
        bundle_state = {
            "id": "unknown",
            "theme_id": "generic",
            "type": "image",
            "role": "unknown",
            "status": "pending",
            "sha256": "c" * 64,
            "url": "/static/generic/images/unknown.png",
            "access": "manifest_static",
            "source_key": "generic/images/unknown.png",
        }
        previous_builder = app_gui_module.build_material_panel_summary
        previous_bundle_service = app_gui_module.BundleService

        class FakeBundleService:
            def get_bundle(self, _bundle_id):
                return dict(bundle_state)

            def set_bundle_role(self, bundle_id, role):
                calls.append((bundle_id, role))
                bundle_state.update({"role": role, "status": "ready"})
                return dict(bundle_state)

        app_gui_module.build_material_panel_summary = lambda: summary
        app_gui_module.BundleService = FakeBundleService
        panel = MaterialPanelWindow()
        toasts: list[tuple[str, bool]] = []
        panel.set_toast_handler(lambda message, success=True: toasts.append((message, success)))
        try:
            panel.refresh()
            point = panel.item_labels[0].rect().center()
            event = QMouseEvent(QEvent.Type.MouseButtonPress, point, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            panel._select_recent_item(0, event)

            self.assertFalse(panel.role_row.isHidden())
            panel._set_selected_role("hero_image")

            self.assertEqual(calls, [("unknown", "hero_image")])
            self.assertIn("已确认：主视觉", panel.detail_label.text())
            self.assertIn("agent 可用", panel.detail_label.text())
            self.assertIn("background: #6F7F5A", panel.role_buttons["hero_image"].styleSheet())
            payload = json.loads(QApplication.clipboard().text())
            self.assertEqual(payload["assets"][0]["role"], "hero_image")
            self.assertEqual(payload["assets"][0]["status"], "ready")
            self.assertIn("已复制 handoff", panel.detail_label.text())
            self.assertEqual(toasts, [])
        finally:
            app_gui_module.build_material_panel_summary = previous_builder
            app_gui_module.BundleService = previous_bundle_service
            panel.close()

    def test_material_panel_copy_ready_handoff_copies_all_ready_assets(self) -> None:
        previous_bundle_service = app_gui_module.BundleService
        app_gui_module.BundleService = lambda: type(
            "FakeBundleService",
            (),
            {
                "list_bundles": lambda _self, status=None: [
                    {
                        "id": "hero",
                        "theme_id": "generic",
                        "type": "image",
                        "role": "hero_image",
                        "status": status or "ready",
                        "sha256": "a" * 64,
                        "url": "/static/generic/images/hero.png",
                        "access": "manifest_static",
                        "source_key": "generic/images/hero.png",
                    },
                    {
                        "id": "icon",
                        "theme_id": "generic",
                        "type": "image",
                        "role": "icon",
                        "status": status or "ready",
                        "sha256": "b" * 64,
                        "url": "/static/generic/images/icon.png",
                        "access": "manifest_static",
                        "source_key": "generic/images/icon.png",
                    },
                ]
            },
        )()
        panel = MaterialPanelWindow()
        toasts: list[tuple[str, bool]] = []
        panel.set_toast_handler(lambda message, success=True: toasts.append((message, success)))
        try:
            panel._copy_ready_handoff()

            payload = json.loads(QApplication.clipboard().text())
            self.assertEqual(payload["source"], "haypile")
            self.assertEqual([asset["id"] for asset in payload["assets"]], ["hero", "icon"])
            self.assertEqual(payload["assets"][0]["role"], "hero_image")
            self.assertEqual(payload["assets"][1]["status"], "ready")
            self.assertEqual(payload["assets"][1]["resolved_url"], "http://127.0.0.1:8010/static/generic/images/icon.png")
            self.assertEqual(payload["assets"][0]["provenance"]["source"], "haypile")
            self.assertNotIn("storage/assets", json.dumps(payload))
            self.assertEqual(panel.copy_ready_button.text(), "已复制 2 个可用")
            QTest.qWait(950)
            self.app.processEvents()
            self.assertEqual(panel.copy_ready_button.text(), "复制可用 handoff")
            self.assertIn("已复制 2 个可用 assets", panel.detail_label.text())
            self.assertEqual(toasts, [])
        finally:
            app_gui_module.BundleService = previous_bundle_service
            panel.close()

    def test_material_panel_copy_ready_handoff_handles_empty_ready_assets(self) -> None:
        previous_bundle_service = app_gui_module.BundleService
        app_gui_module.BundleService = lambda: type(
            "FakeBundleService",
            (),
            {"list_bundles": lambda _self, status=None: []},
        )()
        panel = MaterialPanelWindow()
        toasts: list[tuple[str, bool]] = []
        panel.set_toast_handler(lambda message, success=True: toasts.append((message, success)))
        QApplication.clipboard().setText("keep")
        try:
            panel._copy_ready_handoff()

            self.assertEqual(QApplication.clipboard().text(), "keep")
            self.assertIn("没有可用 assets", panel.detail_label.text())
            self.assertEqual(toasts, [])
        finally:
            app_gui_module.BundleService = previous_bundle_service
            panel.close()

    def test_material_panel_copy_agent_recipe_describes_agent_contract(self) -> None:
        panel = MaterialPanelWindow()
        toasts: list[tuple[str, bool]] = []
        panel.set_toast_handler(lambda message, success=True: toasts.append((message, success)))
        try:
            panel._copy_agent_recipe()

            text = QApplication.clipboard().text()
            self.assertIn("GET http://127.0.0.1:8010/api/v1/bundles?status=ready", text)
            self.assertIn("id, sha256, source_key, url, resolved_url, and provenance", text)
            self.assertIn("MCP haypile_list_bundles", text)
            self.assertIn("Do not read Haypile's local asset directory directly.", text)
            self.assertEqual(panel.copy_recipe_button.text(), "已复制 agent 配方")
            QTest.qWait(950)
            self.app.processEvents()
            self.assertEqual(panel.copy_recipe_button.text(), "复制 agent 配方")
            self.assertIn("已复制 agent 配方", panel.detail_label.text())
            self.assertEqual(toasts, [])
        finally:
            panel.close()

    def test_project_picker_preview_file_display_only_gui_smoke(self) -> None:
        project_root = self.tmpdir / "signal-pool-demo"
        project_root.mkdir()
        preview_path = self.tmpdir / "picker-preview.json"
        os.environ["HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH"] = preview_path.as_posix()
        self._write_json(
            preview_path,
            self._picker_preview(
                project_root=project_root,
                project_state="rolled_back",
                status_label="真实项目：已回滚",
                picker_status="selection_ready",
                primary_action="reapply",
                primary_label="重新投放",
            ),
        )
        panel = MaterialPanelWindow()
        try:
            panel.refresh()

            self.assertEqual(panel.project_label.text(), "● signal-pool-demo")
            self.assertEqual(panel.project_label.toolTip(), project_root.as_posix())
            self.assertTrue(panel.project_label.isHidden())
            self.assertIn("真实项目：已回滚", panel.rehearsal_label.text())
            self.assertIn("重新投放", panel.rehearsal_label.text())
            self.assertTrue(panel.rehearsal_label.isHidden())
            self.assertIn("Project Picker：已读取", panel.service_label.text())
            self.assertTrue(panel.service_label.isHidden())
            self.assertTrue(panel._confirmation_available)
            self.assertEqual(panel.confirmation_preview.title.text(), "重新投放？")
            self.assertEqual(panel.confirmation_preview.body.text(), "signal-pool-demo")
            self.assertEqual(panel.confirmation_preview.summary.text(), "5 项")
            self.assertEqual(panel.confirmation_preview.warning.text(), "再次确认后执行")
            self.assertEqual(panel.confirmation_preview.primary_button.text(), "重新投放")
            self.assertEqual(panel.confirmation_preview._action, "reapply")
            self.assertEqual(panel.confirmation_preview._project_root, project_root.as_posix())
        finally:
            panel.confirmation_preview.close()
            panel.close()

    def test_quick_menu_exposes_actions(self) -> None:
        menu = QuickMenuWindow()
        actions: list[str] = []
        menu.set_action_handler(actions.append)
        try:
            self.assertEqual({action for action, _icon, _tooltip in menu.actions}, {"assets", "mcp", "http", "status", "ai"})
            self.assertEqual(menu.action_tooltips["assets"], "Haypile")

            point = menu._slot_rect("mcp").center()
            self.assertEqual(menu._action_at(point), "mcp")
            event = QMouseEvent(QEvent.Type.MouseButtonPress, point, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            menu.mousePressEvent(event)

            self.assertEqual(actions, ["mcp"])
            QTest.qWait(250)
            self.app.processEvents()
            self.assertFalse(menu.isVisible())
        finally:
            menu.close()

    def test_quick_menu_hover_tracks_current_action(self) -> None:
        menu = QuickMenuWindow()
        try:
            point = menu._slot_rect("assets").center()
            event = QMouseEvent(QEvent.Type.MouseMove, point, point, Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)

            menu.mouseMoveEvent(event)

            self.assertEqual(menu._hovered_action, "assets")
            self.assertEqual(menu.toolTip(), "Haypile")
            menu.hide_menu()
            self.assertEqual(menu._hovered_action, "")
            self.assertEqual(menu.toolTip(), "")

            menu.mouseMoveEvent(event)
            menu.leaveEvent(QEvent(QEvent.Type.Leave))
            self.assertEqual(menu._hovered_action, "")
        finally:
            menu.close()

    def test_quick_menu_empty_click_hides_menu(self) -> None:
        menu = QuickMenuWindow()
        actions: list[str] = []
        menu.set_action_handler(actions.append)
        try:
            menu.show_menu(0, 0)
            point = menu.rect().center()
            self.assertEqual(menu._action_at(point), "")
            event = QMouseEvent(QEvent.Type.MouseButtonPress, point, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)

            menu.mousePressEvent(event)

            self.assertEqual(actions, [])
            QTest.qWait(250)
            self.app.processEvents()
            self.assertFalse(menu.isVisible())
        finally:
            menu.close()

    def test_quick_menu_first_show_keeps_track_center_aligned(self) -> None:
        menu = QuickMenuWindow()
        try:
            menu.set_track_center(QPointF(96, 112))

            menu.show_menu(10, 20)

            self.assertEqual(menu._content_shift, QPointF(0, 0))
            center, _radius = menu._track_geometry()
            self.assertEqual(center, QPointF(96, 112))
        finally:
            menu.close()

    def test_quick_menu_attention_action_clears_after_click(self) -> None:
        menu = QuickMenuWindow()
        actions: list[str] = []
        menu.set_action_handler(actions.append)
        try:
            menu.set_attention_action("assets")
            self.assertEqual(menu._attention_action, "assets")

            menu._emit_action("assets")

            self.assertEqual(actions, ["assets"])
            self.assertEqual(menu._attention_action, "")
        finally:
            menu.close()

    def test_floating_windows_are_top_level_not_tool_windows(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        menu = QuickMenuWindow()
        try:
            for widget in (ball, menu):
                flags = widget.windowFlags()
                self.assertNotEqual(flags & Qt.WindowType.WindowType_Mask, Qt.WindowType.Tool)
                self.assertTrue(bool(flags & Qt.WindowType.WindowStaysOnTopHint))
                self.assertTrue(bool(flags & Qt.WindowType.WindowDoesNotAcceptFocus))
        finally:
            menu.close()
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_material_panel_accepts_focus_for_search_input(self) -> None:
        panel = MaterialPanelWindow()
        try:
            flags = panel.windowFlags()
            self.assertTrue(bool(flags & Qt.WindowType.WindowStaysOnTopHint))
            self.assertFalse(bool(flags & Qt.WindowType.WindowDoesNotAcceptFocus))
            self.assertEqual(panel.search_input.focusPolicy(), Qt.FocusPolicy.ClickFocus)
        finally:
            panel.close()

    def test_material_panel_search_cursor_is_click_only(self) -> None:
        panel = MaterialPanelWindow()
        try:
            panel.show_panel()
            QTest.qWait(20)
            self.app.processEvents()
            self.assertFalse(panel.search_input.hasFocus())

            panel.search_input.setFocus(Qt.FocusReason.MouseFocusReason)
            self.assertTrue(panel.search_input.hasFocus())

            panel._set_filter_mode("image")
            self.assertFalse(panel.search_input.hasFocus())
        finally:
            panel.close()

    def test_floating_ball_quick_menu_copies_http_url(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        try:
            ball._toggle_quick_menu()
            self.assertTrue(ball.quick_menu.isVisible())

            ball.quick_menu._emit_action("http")

            self.assertEqual(QApplication.clipboard().text(), "http://127.0.0.1:8010")
            QTest.qWait(250)
            self.app.processEvents()
            self.assertFalse(ball.quick_menu.isVisible())
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_left_click_closes_visible_material_panel(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        try:
            ball.material_panel.show()
            self.assertTrue(ball.material_panel.isVisible())
            point = ball.rect().center()
            event = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                point,
                point,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )

            ball.mouseReleaseEvent(event)
            QTest.qWait(190)
            self.app.processEvents()

            self.assertFalse(ball.material_panel.isVisible())
            self.assertFalse(ball.quick_menu.isVisible())
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_quick_menu_first_open_aligns_to_ball_center(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        ball._available_geometry = lambda: app_gui_module.QRect(0, 0, 900, 700)
        try:
            ball.move(300, 220)

            ball._toggle_quick_menu()
            self.app.processEvents()

            track_center, _radius = ball.quick_menu._track_geometry()
            expected = ball.frameGeometry().center() - ball.quick_menu.frameGeometry().topLeft()
            self.assertLessEqual(abs(track_center.x() - expected.x()), 1)
            self.assertLessEqual(abs(track_center.y() - expected.y()), 1)
            self.assertEqual(ball.quick_menu._content_shift, QPointF(0, 0))
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_quick_menu_actions_are_wired(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        previous_builder = app_gui_module.build_material_panel_summary
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        app_gui_module.build_material_panel_summary = lambda: MaterialPanelSummary(
            total_count=3,
            recognized_count=2,
            pending_count=1,
            service_status="Haypile：等待入库",
            recognition_status="分类：有待确认 · 模型：未安装 qwen3-vl:8b",
        )
        ball = app_gui_module.HaypileFloatingBall()
        toasts: list[tuple[str, bool]] = []
        ball.show_toast = lambda message, success=True: toasts.append((message, success))
        try:
            ball._handle_quick_menu_action("assets")
            self.assertTrue(ball.material_panel.isVisible())

            ball._handle_quick_menu_action("mcp")
            self.assertIn('"haypile"', QApplication.clipboard().text())
            self.assertIn(("已复制 MCP 配置", True), toasts)

            ball._probe_backend_via_ipc = lambda: True
            ball._handle_quick_menu_action("status")
            self.assertIn(("运行中 · 可用 2 · 待确认 1", True), toasts)

            initial_ai = ball.ai_enabled
            ball._ai_model_state = lambda: ("ready", "模型可用 qwen2.5vl:3b")
            ball._ai_status_text = lambda: "AI 分拣已开启" if ball.ai_enabled else "AI 分拣已关闭"
            ball._handle_quick_menu_action("ai")
            self.assertEqual(ball.ai_enabled, not initial_ai)
            self.assertEqual(ball.quick_menu._ai_enabled, ball.ai_enabled)
            self.assertIn(
                ("AI 分拣已开启" if ball.ai_enabled else "AI 分拣已关闭", True),
                toasts,
            )
        finally:
            ball.material_panel.close()
            ball.close()
            self.app.processEvents()
            app_gui_module.build_material_panel_summary = previous_builder
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_ai_action_opens_setup_when_model_missing(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        toasts: list[tuple[str, bool]] = []
        ball.show_toast = lambda message, success=True: toasts.append((message, success))
        ball._ai_model_state = lambda: ("missing", "模型未安装 qwen2.5vl:3b")
        try:
            ball.ai_enabled = False

            ball._handle_quick_menu_action("ai")

            self.assertFalse(ball.ai_enabled)
            self.assertTrue(ball.ai_setup_panel.isVisible())
            self.assertEqual(ball.ai_setup_panel.command.text(), f"ollama pull {ball.settings.VISION_CLASSIFIER_MODEL}")
            self.assertIn("模型未安装", ball.ai_setup_panel.body.text())
            self.assertIn(("先安装本地视觉模型", False), toasts)
        finally:
            ball.ai_setup_panel.close()
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_ai_setup_recheck_enables_when_model_ready(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        toasts: list[tuple[str, bool]] = []
        ball.show_toast = lambda message, success=True: toasts.append((message, success))
        ball._ai_model_state = lambda: ("ready", "模型可用 qwen2.5vl:3b")
        ball._ai_status_text = lambda: "AI 分拣已开启"
        try:
            ball.ai_enabled = False
            ball.ai_setup_panel.show()

            ball._recheck_ai_setup()

            self.assertTrue(ball.ai_enabled)
            self.assertFalse(ball.ai_setup_panel.isVisible())
            self.assertEqual(ball.quick_menu._ai_enabled, True)
            self.assertIn(("AI 分拣已开启", True), toasts)
        finally:
            ball.ai_setup_panel.close()
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_gui_state_keeps_ai_and_position_together(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        ball._gui_state_path = self.tmpdir / "gui_state.json"
        try:
            app_gui_module.atomic_write_json(ball._gui_state_path, {"x": 101, "y": 102})

            ball.ai_enabled = False
            ball._save_ai_enabled()
            payload = json.loads(ball._gui_state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["x"], 101)
            self.assertEqual(payload["y"], 102)
            self.assertFalse(payload["ai_enabled"])

            ball.move(120, 130)
            ball._save_window_position()
            payload = json.loads(ball._gui_state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["x"], 120)
            self.assertEqual(payload["y"], 130)
            self.assertFalse(payload["ai_enabled"])
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_toast_anchors_to_grass_pile_when_material_panel_visible(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        anchors = []
        ball.toast.show_message = lambda _message, success, anchor, available: anchors.append(anchor)
        try:
            ball.move(120, 140)
            ball.material_panel.show()
            ball.material_panel.move(240, 180)

            ball.show_toast("ok", success=True)

            self.assertEqual(anchors[-1], ball._toast_anchor())
            self.assertNotEqual(anchors[-1].topLeft(), ball.material_panel.frameGeometry().topLeft())
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_toast_defaults_below_grass_pile(self) -> None:
        toast = app_gui_module.ToastLabel()
        try:
            anchor = app_gui_module.QRect(100, 100, 72, 72)
            available = app_gui_module.QRect(0, 0, 500, 500)

            toast.show_message("ok", success=True, anchor=anchor, available=available)

            self.assertGreaterEqual(toast.y(), anchor.bottom())
        finally:
            toast.close()

    def test_floating_ball_toast_uses_side_position_near_bottom_edge(self) -> None:
        toast = app_gui_module.ToastLabel()
        try:
            anchor = app_gui_module.QRect(120, 430, 72, 72)
            available = app_gui_module.QRect(0, 0, 500, 520)

            toast.show_message("ok", success=True, anchor=anchor, available=available)

            self.assertLess(toast.y(), anchor.bottom())
            self.assertGreaterEqual(toast.y() + toast.height(), anchor.top())
            self.assertGreaterEqual(toast.x(), anchor.right())
        finally:
            toast.close()

    def test_floating_ball_toast_anchor_uses_visual_circle_when_expanded(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        try:
            ball.setGeometry(100, 120, ball.EXPANDED_SIZE, ball.EXPANDED_SIZE)

            anchor = ball._toast_anchor()

            self.assertEqual(anchor.size(), ball._get_collapsed_circle_rect().size())
            self.assertNotEqual(anchor.size(), ball.frameGeometry().size())
            self.assertEqual(anchor.center(), ball.frameGeometry().center())
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_drag_repositions_visible_toast(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        anchors = []
        ball.toast.reposition = lambda anchor, available: anchors.append(anchor)
        try:
            ball.setGeometry(120, 140, ball.COLLAPSED_SIZE, ball.COLLAPSED_SIZE)
            ball.drag_offset = app_gui_module.QPoint(12, 14)
            ball._press_global_pos = app_gui_module.QPoint(130, 150)
            event = QMouseEvent(
                QEvent.Type.MouseMove,
                app_gui_module.QPointF(0, 0),
                app_gui_module.QPointF(0, 0),
                app_gui_module.QPointF(220, 240),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )

            ball.mouseMoveEvent(event)

            self.assertTrue(anchors)
            self.assertEqual(anchors[-1], ball._toast_anchor())
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_progress_window_shows_at_target_position(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        positions: list[app_gui_module.QPoint] = []
        ball.progress_window.begin_at = lambda position: positions.append(position)
        ball._progress_window_position = lambda: app_gui_module.QPoint(222, 111)
        ball._reposition_progress_window = lambda: positions.append(app_gui_module.QPoint(-1, -1))
        ball.show_toast = lambda message, success=True: None
        try:
            ball._start_worker([])

            self.assertEqual(positions, [app_gui_module.QPoint(222, 111)])
        finally:
            if ball.worker is not None and ball.worker.isRunning():
                ball.worker.requestInterruption()
                ball.worker.wait(1000)
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_pending_badge_highlights_status_on_quick_menu_open(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        previous_builder = app_gui_module.build_material_panel_summary
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        app_gui_module.build_material_panel_summary = lambda: MaterialPanelSummary(
            total_count=1,
            recognized_count=0,
            pending_count=1,
            service_status="Haypile：运行中",
            recognition_status="分类：有待确认",
        )
        ball = app_gui_module.HaypileFloatingBall()
        try:
            ball._toggle_quick_menu()

            self.assertTrue(ball.quick_menu.isVisible())
            self.assertEqual(ball.quick_menu._attention_action, "status")
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.build_material_panel_summary = previous_builder
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_quick_menu_stays_anchored_when_ball_is_at_screen_edge(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        ball._available_geometry = lambda: app_gui_module.QRect(0, 0, 360, 360)
        positions = [
            (0, 0),
            (144, 0),
            (288, 0),
            (0, 144),
            (288, 144),
            (0, 288),
            (144, 288),
            (288, 288),
        ]
        try:
            for x, y in positions:
                with self.subTest(position=(x, y)):
                    ball.move(x, y)
                    ball_pos = ball.pos()

                    ball._reposition_quick_menu()

                    self.assertEqual(ball.pos(), ball_pos)
                    self.assertGreaterEqual(ball.quick_menu.x(), 10)
                    self.assertGreaterEqual(ball.quick_menu.y(), 10)
                    self.assertLessEqual(ball.quick_menu.frameGeometry().right(), 349)
                    self.assertLessEqual(ball.quick_menu.frameGeometry().bottom(), 349)
                    track_center, _radius = ball.quick_menu._track_geometry()
                    expected = ball.frameGeometry().center() - ball.quick_menu.frameGeometry().topLeft()
                    self.assertLessEqual(abs(track_center.x() - expected.x()), 1)
                    self.assertLessEqual(abs(track_center.y() - expected.y()), 1)
                    for action, _icon, _tooltip in ball.quick_menu.actions:
                        slot_rect = ball.quick_menu._slot_rect(action).toAlignedRect()
                        self.assertTrue(ball.quick_menu.rect().intersects(slot_rect))
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_resize_target_stays_on_screen_edge(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        ball._available_geometry = lambda: app_gui_module.QRect(0, 0, 360, 360)
        try:
            for x, y in ((0, 0), (288, 0), (0, 288), (288, 288)):
                with self.subTest(position=(x, y)):
                    ball.setGeometry(x, y, ball.COLLAPSED_SIZE, ball.COLLAPSED_SIZE)

                    expanded = ball._clamped_geometry_for_size(ball.EXPANDED_SIZE)
                    collapsed = ball._clamped_geometry_for_size(ball.COLLAPSED_SIZE)

                    for rect in (expanded, collapsed):
                        self.assertGreaterEqual(rect.left(), 10)
                        self.assertGreaterEqual(rect.top(), 10)
                        self.assertLessEqual(rect.right(), 349)
                        self.assertLessEqual(rect.bottom(), 349)
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_drop_lets_leaf_frame_close_before_collapsing(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        received: list[list[Path]] = []
        file_path = self.tmpdir / "hero.png"
        file_path.write_bytes(b"not-real-image")
        ball._start_worker = lambda files: received.append(files)
        try:
            ball.resize(ball.EXPANDED_SIZE, ball.EXPANDED_SIZE)
            ball.is_expanded = True
            ball._drag_hover = True
            ball._set_drop_open_progress(1.0)
            mime_data = QMimeData()
            mime_data.setUrls([QUrl.fromLocalFile(str(file_path))])
            event = QDropEvent(
                QPointF(10, 10),
                Qt.DropAction.CopyAction,
                mime_data,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )

            ball.dropEvent(event)

            self.assertEqual(received, [[file_path]])
            self.assertFalse(ball._drag_hover)
            self.assertTrue(ball.is_expanded)
            self.assertTrue(ball._collapse_timer.isActive())
            self.assertEqual(ball._drop_open_animation.endValue(), 0.0)
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_extracts_remote_media_urls_from_browser_drop(self) -> None:
        mime_data = QMimeData()
        mime_data.setUrls([QUrl("https://cdn.example.com/hero.webp")])
        mime_data.setHtml(
            '<img src="https://cdn.example.com/hero.webp">'
            '<audio src="https://cdn.example.com/theme.mp3"></audio>'
            '<source src="https://cdn.example.com/loop.ogg">'
        )
        mime_data.setText("https://cdn.example.com/voice.wav")

        urls = app_gui_module.HaypileFloatingBall._extract_remote_media_urls(mime_data)

        self.assertEqual(
            urls,
            [
                "https://cdn.example.com/hero.webp",
                "https://cdn.example.com/theme.mp3",
                "https://cdn.example.com/loop.ogg",
                "https://cdn.example.com/voice.wav",
            ],
        )

    def test_floating_ball_drop_remote_url_starts_download_worker(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        received: list[tuple[list[str], list[Path]]] = []
        ball._start_remote_download_worker = lambda urls, local_files=None: received.append((urls, local_files or []))
        try:
            mime_data = QMimeData()
            mime_data.setUrls([QUrl("https://cdn.example.com/hero.webp")])
            event = QDropEvent(
                QPointF(10, 10),
                Qt.DropAction.CopyAction,
                mime_data,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )

            ball.dropEvent(event)

            self.assertEqual(received, [(["https://cdn.example.com/hero.webp"], [])])
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_remote_download_worker_accepts_media_content_type(self) -> None:
        previous_stream = app_gui_module.httpx.stream
        body = b"webp-bytes"

        class FakeResponse:
            headers = {"content-type": "image/webp", "content-length": str(len(body))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self) -> None:
                return None

            def iter_bytes(self):
                yield body

        app_gui_module.httpx.stream = lambda *_args, **_kwargs: FakeResponse()
        worker = app_gui_module.RemoteDownloadWorker(
            ["https://cdn.example.com/path/hero.webp"],
            self.tmpdir / "incoming",
        )
        finished: list[tuple[list[Path], str, bool]] = []
        worker.finished_signal.connect(lambda files, message, success: finished.append((files, message, success)))
        try:
            worker.run()

            self.assertTrue(finished[-1][2])
            self.assertEqual(finished[-1][0][0].suffix, ".webp")
            self.assertEqual(finished[-1][0][0].read_bytes(), body)
            provenance = read_asset_provenance(finished[-1][0][0])
            self.assertEqual(provenance["origin_url"], "https://cdn.example.com/path/hero.webp")
            self.assertEqual(provenance["content_type"], "image/webp")
            self.assertEqual(provenance["temp_file"], str(finished[-1][0][0]))
            self.assertIn("downloaded_at", provenance)
        finally:
            app_gui_module.httpx.stream = previous_stream

    def test_remote_download_worker_reports_non_media_link_clearly(self) -> None:
        previous_stream = app_gui_module.httpx.stream

        class FakeResponse:
            headers = {"content-type": "text/html", "content-length": "42"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self) -> None:
                return None

        app_gui_module.httpx.stream = lambda *_args, **_kwargs: FakeResponse()
        worker = app_gui_module.RemoteDownloadWorker(
            ["https://example.com/page"],
            self.tmpdir / "incoming",
        )
        finished: list[tuple[list[Path], str, bool]] = []
        worker.finished_signal.connect(lambda files, message, success: finished.append((files, message, success)))
        try:
            worker.run()

            self.assertFalse(finished[-1][2])
            self.assertEqual(finished[-1][1], "没有找到可收纳的图片或音频")
        finally:
            app_gui_module.httpx.stream = previous_stream

    def test_remote_download_worker_blocks_private_network_url(self) -> None:
        previous_stream = app_gui_module.httpx.stream
        called = False

        def fake_stream(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("private URL should be blocked before HTTP")

        app_gui_module.httpx.stream = fake_stream
        worker = app_gui_module.RemoteDownloadWorker(
            ["http://127.0.0.1/private.png"],
            self.tmpdir / "incoming",
        )
        finished: list[tuple[list[Path], str, bool]] = []
        worker.finished_signal.connect(lambda files, message, success: finished.append((files, message, success)))
        try:
            worker.run()

            self.assertFalse(called)
            self.assertFalse(finished[-1][2])
            self.assertEqual(finished[-1][1], "没有找到可收纳的图片或音频")
        finally:
            app_gui_module.httpx.stream = previous_stream

    def test_remote_download_worker_reports_oversized_asset(self) -> None:
        previous_stream = app_gui_module.httpx.stream

        class FakeResponse:
            headers = {
                "content-type": "image/png",
                "content-length": str(app_gui_module.RemoteDownloadWorker.MAX_FILE_SIZE_BYTES + 1),
            }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self) -> None:
                return None

        app_gui_module.httpx.stream = lambda *_args, **_kwargs: FakeResponse()
        worker = app_gui_module.RemoteDownloadWorker(
            ["https://cdn.example.com/huge.png"],
            self.tmpdir / "incoming",
        )
        finished: list[tuple[list[Path], str, bool]] = []
        worker.finished_signal.connect(lambda files, message, success: finished.append((files, message, success)))
        try:
            worker.run()

            self.assertFalse(finished[-1][2])
            self.assertEqual(finished[-1][1], "网页素材超过 500MB")
        finally:
            app_gui_module.httpx.stream = previous_stream

    def test_remote_download_worker_accepts_audio_url(self) -> None:
        previous_stream = app_gui_module.httpx.stream
        body = b"mp3-bytes"

        class FakeResponse:
            headers = {"content-type": "audio/mpeg", "content-length": str(len(body))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self) -> None:
                return None

            def iter_bytes(self):
                yield body

        app_gui_module.httpx.stream = lambda *_args, **_kwargs: FakeResponse()
        worker = app_gui_module.RemoteDownloadWorker(
            ["https://cdn.example.com/audio/theme"],
            self.tmpdir / "incoming",
        )
        finished: list[tuple[list[Path], str, bool]] = []
        worker.finished_signal.connect(lambda files, message, success: finished.append((files, message, success)))
        try:
            worker.run()

            self.assertTrue(finished[-1][2])
            self.assertEqual(finished[-1][0][0].suffix, ".mp3")
            self.assertEqual(finished[-1][0][0].read_bytes(), body)
            self.assertEqual(read_asset_provenance(finished[-1][0][0])["content_type"], "audio/mpeg")
        finally:
            app_gui_module.httpx.stream = previous_stream

    def test_remote_download_worker_reports_http_failure(self) -> None:
        previous_stream = app_gui_module.httpx.stream
        request = app_gui_module.httpx.Request("GET", "https://cdn.example.com/forbidden.png")
        response = app_gui_module.httpx.Response(403, request=request)

        def fake_stream(*_args, **_kwargs):
            raise app_gui_module.httpx.HTTPStatusError("forbidden", request=request, response=response)

        app_gui_module.httpx.stream = fake_stream
        worker = app_gui_module.RemoteDownloadWorker(
            ["https://cdn.example.com/forbidden.png"],
            self.tmpdir / "incoming",
        )
        finished: list[tuple[list[Path], str, bool]] = []
        worker.finished_signal.connect(lambda files, message, success: finished.append((files, message, success)))
        try:
            worker.run()

            self.assertFalse(finished[-1][2])
            self.assertEqual(finished[-1][1], "网页素材无法下载")
        finally:
            app_gui_module.httpx.stream = previous_stream

    def test_ingest_worker_carries_browser_provenance_to_final_asset(self) -> None:
        assets_dir = self.tmpdir / "assets"
        source = self.tmpdir / "incoming" / "hero.webp"
        destination = assets_dir / "generic/images/generic_img_unknown_deadbeef.webp"
        source.parent.mkdir(parents=True)
        destination.parent.mkdir(parents=True)
        source.write_bytes(b"image")
        write_asset_provenance(
            source,
            {
                "origin_url": "https://cdn.example.com/hero.webp",
                "content_type": "image/webp",
                "downloaded_at": "2026-07-06T00:00:00+00:00",
                "temp_file": str(source),
            },
        )
        worker = app_gui_module.IngestWorker([], assets_dir)

        worker._persist_asset_provenance(
            source_path=source,
            destination=destination,
            sha256_hex="abc123",
        )

        provenance = read_asset_provenance(destination)
        self.assertEqual(provenance["origin_url"], "https://cdn.example.com/hero.webp")
        self.assertEqual(provenance["source_key"], "generic/images/generic_img_unknown_deadbeef.webp")
        self.assertEqual(provenance["sha256"], "abc123")

    def test_ingest_worker_persists_ai_suggestions_without_browser_provenance(self) -> None:
        assets_dir = self.tmpdir / "assets"
        source = self.tmpdir / "hero.png"
        destination = assets_dir / "generic/images/generic_img_hero_image_deadbeef.png"
        destination.parent.mkdir(parents=True)
        source.write_bytes(b"image")
        worker = app_gui_module.IngestWorker([], assets_dir)

        worker._persist_asset_provenance(
            source_path=source,
            destination=destination,
            sha256_hex="abc123",
            ai_suggestions={"quality": "high", "tags": ["主视觉"]},
        )

        provenance = read_asset_provenance(destination)
        self.assertEqual(provenance["source_key"], "generic/images/generic_img_hero_image_deadbeef.png")
        self.assertEqual(provenance["sha256"], "abc123")
        self.assertEqual(provenance["ai_suggestions"]["quality"], "high")

    def test_floating_ball_drag_enter_uses_short_prepare_state_before_leaf_frame(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        file_path = self.tmpdir / "hero.png"
        file_path.write_bytes(b"not-real-image")
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(str(file_path))])
        event = QDragEnterEvent(
            QPoint(10, 10),
            Qt.DropAction.CopyAction,
            mime_data,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        try:
            ball.dragEnterEvent(event)

            self.assertTrue(ball._drag_hover)
            self.assertTrue(ball._drag_prepare_active)
            self.assertTrue(ball._drag_prepare_timer.isActive())
            self.assertFalse(ball.is_expanded)
            self.assertEqual(ball._drop_open_progress, 0.0)

            ball._open_drop_target()

            self.assertFalse(ball._drag_prepare_active)
            self.assertEqual(ball._drop_open_animation.endValue(), 1.0)
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_success_ingest_triggers_single_bounce_feedback(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        try:
            rect = app_gui_module.QRectF(ball._get_collapsed_circle_rect())
            ball._on_ingest_finished("收纳完成", True)

            self.assertTrue(ball._drop_feedback_active())
            self.assertTrue(ball._bounce_feedback_active())
            self.assertEqual(ball.quick_menu._attention_action, "assets")
            self.assertNotEqual(ball._bounced_icon_rect(rect), rect)
            previous_monotonic = app_gui_module.time.monotonic
            try:
                ball._bounce_feedback_started_at = 1000.0
                ball._bounce_feedback_until = 1000.55
                ball._drop_feedback_until = 1000.55
                icon_rect = rect.adjusted(-1, -5, 1, 1)
                app_gui_module.time.monotonic = lambda: 1000.12
                crouched = ball._bounced_icon_rect(icon_rect)
                self.assertLess(crouched.height(), ball.height() - 2)
                app_gui_module.time.monotonic = lambda: 1000.30
                bounced = ball._bounced_icon_rect(icon_rect)
                self.assertLess(bounced.center().y(), crouched.center().y())
                self.assertGreaterEqual(bounced.left(), 1)
                self.assertGreaterEqual(bounced.top(), 1)
                self.assertLessEqual(bounced.right(), ball.width() - 1)
                self.assertLessEqual(bounced.bottom(), ball.height() - 1)
                app_gui_module.time.monotonic = lambda: 1000.62
                self.assertFalse(ball._bounce_feedback_active())
                self.assertFalse(ball._drop_feedback_active())
            finally:
                app_gui_module.time.monotonic = previous_monotonic

            ball._bounce_feedback_until = 0.0
            ball._drop_feedback_until = 0.0
            ball._on_ingest_finished("收纳失败", False)
            self.assertFalse(ball._bounce_feedback_active())
            self.assertFalse(ball._drop_feedback_active())
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_visual_timer_stops_when_idle(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        try:
            self.assertFalse(ball._visual_timer.isActive())

            ball._hovered = True
            ball._sync_visual_timer()
            self.assertTrue(ball._visual_timer.isActive())

            ball._hovered = False
            ball._sync_visual_timer()
            self.assertFalse(ball._visual_timer.isActive())

            ball._drop_feedback_until = app_gui_module.time.monotonic() + 1
            ball._sync_visual_timer()
            self.assertTrue(ball._visual_timer.isActive())

            ball._drop_feedback_until = 0
            ball._advance_visual_state()
            self.assertFalse(ball._visual_timer.isActive())
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_drag_release_has_set_down_feedback(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        previous_monotonic = app_gui_module.time.monotonic
        try:
            app_gui_module.time.monotonic = lambda: 1000.0
            ball._last_drag_global_pos = QPoint(10, 10)
            ball._last_drag_sample_at = 999.95
            ball._sample_drag_velocity(QPoint(40, 18))
            self.assertGreater(ball._drag_velocity.x(), 0)
            rect = app_gui_module.QRectF(ball._get_collapsed_circle_rect())
            dragged = ball._dragged_icon_rect(rect)
            self.assertGreater(dragged.width(), rect.width())
            self.assertLess(dragged.height(), rect.height())

            ball._start_drag_release_feedback()
            self.assertTrue(ball._drag_release_feedback_active())
            self.assertTrue(ball._visual_state_active())

            app_gui_module.time.monotonic = lambda: 1000.08
            pressed = ball._drag_release_icon_rect(rect)
            self.assertGreater(pressed.width(), rect.width())
            self.assertLess(pressed.height(), rect.height())
            self.assertLessEqual(pressed.bottom(), rect.bottom())
            self.assertGreaterEqual(pressed.bottom(), rect.bottom() - 2)

            app_gui_module.time.monotonic = lambda: 1000.16
            rebounded = ball._drag_release_icon_rect(rect)
            self.assertLess(rebounded.width(), pressed.width())
            self.assertGreater(rebounded.height(), pressed.height())
            self.assertLess(rebounded.bottom(), pressed.bottom())
            self.assertLess(rebounded.top(), pressed.top())
            for current in (pressed, rebounded):
                self.assertGreaterEqual(current.top(), 0)
                self.assertLessEqual(current.bottom(), ball.height())

            app_gui_module.time.monotonic = lambda: 1000.31
            self.assertFalse(ball._drag_release_feedback_active())
        finally:
            app_gui_module.time.monotonic = previous_monotonic
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_drag_bend_follows_pointer_direction(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        try:
            ball._drag_velocity = QPointF(760, 0)
            _vx, _vy, drag, rotation, shear_x, _scale_x, _scale_y = ball._drag_bend_values()
            self.assertGreater(drag, 0)
            self.assertLess(rotation, 0)
            self.assertGreater(shear_x, 0)

            ball._drag_velocity = QPointF(0, -760)
            *_unused, scale_x, scale_y = ball._drag_bend_values()
            self.assertLess(scale_x, 1.0)
            self.assertGreater(scale_y, 1.0)

            ball._drag_velocity = QPointF(0, 760)
            *_unused, scale_x, scale_y = ball._drag_bend_values()
            self.assertGreater(scale_x, 1.0)
            self.assertLess(scale_y, 1.0)
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_duplicate_ingest_uses_nudge_not_bounce(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        try:
            ball._on_ingest_finished("收纳完成：新增 0，去重 1", True)

            self.assertTrue(ball._nudge_feedback_active())
            self.assertFalse(ball._bounce_feedback_active())
            self.assertFalse(ball._drop_feedback_active())
            self.assertEqual(ball.quick_menu._attention_action, "")
            previous_monotonic = app_gui_module.time.monotonic
            try:
                ball._nudge_feedback_started_at = 1000.0
                ball._nudge_feedback_until = 1000.42
                app_gui_module.time.monotonic = lambda: 1000.10
                rect = app_gui_module.QRectF(ball._get_collapsed_circle_rect())
                self.assertNotEqual(ball._nudged_icon_rect(rect), rect)
                app_gui_module.time.monotonic = lambda: 1000.50
                self.assertFalse(ball._nudge_feedback_active())
            finally:
                app_gui_module.time.monotonic = previous_monotonic
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_failed_ingest_uses_reject_feedback(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        try:
            ball._on_ingest_finished("文件被拦截", False)

            self.assertTrue(ball._reject_feedback_active())
            self.assertFalse(ball._bounce_feedback_active())
            self.assertFalse(ball._nudge_feedback_active())
            self.assertEqual(ball.quick_menu._attention_action, "")
            previous_monotonic = app_gui_module.time.monotonic
            try:
                ball._reject_feedback_started_at = 1000.0
                ball._reject_feedback_until = 1000.32
                app_gui_module.time.monotonic = lambda: 1000.16
                rect = app_gui_module.QRectF(ball._get_collapsed_circle_rect())
                rejected = ball._rejected_icon_rect(rect)
                self.assertLess(rejected.width(), rect.width())
                self.assertGreater(rejected.center().y(), rect.center().y())
                app_gui_module.time.monotonic = lambda: 1000.40
                self.assertFalse(ball._reject_feedback_active())
            finally:
                app_gui_module.time.monotonic = previous_monotonic
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_worker_running_uses_subtle_breath_rect(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        try:
            rect = app_gui_module.QRectF(ball._get_collapsed_circle_rect())
            breathed = ball._busy_breath_icon_rect(rect, 1.0)

            self.assertGreater(breathed.width(), rect.width())
            self.assertGreater(breathed.height(), rect.height())
            self.assertEqual(breathed.center(), rect.center())
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_pending_badge_renders_when_assets_need_review(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        previous_builder = app_gui_module.build_material_panel_summary
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        app_gui_module.build_material_panel_summary = lambda: MaterialPanelSummary(
            total_count=1,
            recognized_count=0,
            pending_count=1,
            service_status="Haypile：运行中",
            recognition_status="分类：有待确认",
        )
        ball = app_gui_module.HaypileFloatingBall()
        try:
            self.assertTrue(ball._has_pending_assets)
            pixmap = QPixmap(ball.size())
            pixmap.fill(Qt.GlobalColor.transparent)
            ball.render(pixmap)

            image = pixmap.toImage()
            circle_rect = ball._get_collapsed_circle_rect()
            badge_color = image.pixelColor(circle_rect.right() - 8, circle_rect.top() + 11)
            self.assertGreater(badge_color.alpha(), 180)
            self.assertGreater(badge_color.red(), badge_color.blue())
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.build_material_panel_summary = previous_builder
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_drag_and_shake_stay_on_screen_edge(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        ball._available_geometry = lambda: app_gui_module.QRect(0, 0, 360, 360)
        try:
            ball.setGeometry(10, 10, ball.COLLAPSED_SIZE, ball.COLLAPSED_SIZE)
            ball.drag_offset = app_gui_module.QPoint(36, 36)
            ball._press_global_pos = app_gui_module.QPoint(100, 100)
            event = QMouseEvent(
                QEvent.Type.MouseMove,
                app_gui_module.QPointF(0, 0),
                app_gui_module.QPointF(0, 0),
                app_gui_module.QPointF(-120, -120),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )

            ball.mouseMoveEvent(event)

            self.assertGreaterEqual(ball.x(), 10)
            self.assertGreaterEqual(ball.y(), 10)

            ball.setGeometry(10, 10, ball.COLLAPSED_SIZE, ball.COLLAPSED_SIZE)
            ball._shake_window()
            for step in (0, 0.2, 0.4, 0.6, 0.8, 1):
                point = ball._shake_animation.keyValueAt(step)
                self.assertGreaterEqual(point.x(), 10)
                self.assertGreaterEqual(point.y(), 10)
            ball._shake_animation.stop()
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_drop_leaf_state_is_drag_only(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        try:
            ball.resize(ball.EXPANDED_SIZE, ball.EXPANDED_SIZE)
            ball.is_expanded = True
            ball._drag_hover = True
            ball._set_drop_open_progress(2)

            pixmap = QPixmap(ball.size())
            pixmap.fill(Qt.GlobalColor.transparent)
            ball.render(pixmap)

            self.assertEqual(ball._drop_open_progress, 1.0)
            self.assertGreater(len(ball._drop_leaf_frame_runs), 1000)
            leaf_buckets = {run[3] for run in ball._drop_leaf_frame_runs if len(run) > 3}
            self.assertGreater(len(leaf_buckets), 1)
            self.assertLessEqual(leaf_buckets, {0, 1, 2})
            self.assertEqual(len(ball._drop_leaf_renderers), 5)
            self.assertFalse(ball.quick_menu.isVisible())
            image = pixmap.toImage()
            center_color = image.pixelColor(ball.width() // 2, ball.height() // 2)
            self.assertLess(center_color.alpha(), 80)
            leaf_pixels = 0
            for x in range(80, 220, 4):
                for y in range(80, 220, 4):
                    color = image.pixelColor(x, y)
                    if color.alpha() > 80 and color.green() > color.red():
                        leaf_pixels += 1
            self.assertGreater(leaf_pixels, 120)
            top_edge = image.pixelColor(ball.width() // 2, int((ball.height() - min(ball.width(), ball.height()) * 0.47) / 2))
            self.assertLess(top_edge.alpha(), 120)

            ball._animate_drop_open(False)
            self.assertEqual(ball._drop_open_animation.endValue(), 0.0)
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_saves_and_restores_position(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        ball._available_geometry = lambda: app_gui_module.QRect(0, 0, 360, 360)
        ball._gui_state_path = self.tmpdir / "gui_state.json"
        try:
            ball.move(123, 145)
            ball._save_window_position()

            self.assertEqual(json.loads(ball._gui_state_path.read_text(encoding="utf-8")), {"x": 123, "y": 145})
            self.assertEqual(ball._restore_window_position(), app_gui_module.QPoint(123, 145))

            ball._gui_state_path.write_text('{"x":999,"y":-99}', encoding="utf-8")
            restored = ball._restore_window_position()
            self.assertLessEqual(restored.x() + ball.width(), 350)
            self.assertGreaterEqual(restored.y(), 10)
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_starts_backend_by_default(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        previous_popen = app_gui_module.subprocess.Popen
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        os.environ.pop("HAYPILE_GUI_ALLOW_BACKEND_START", None)
        os.environ.pop("HAYPILE_BACKEND_HOST_ALLOW_START", None)
        calls: list[dict[str, object]] = []

        class FakeProcess:
            pid = 12345

            def poll(self) -> None:
                return None

        def fake_popen(command, **kwargs):
            calls.append({"command": command, **kwargs})
            return FakeProcess()

        ball = app_gui_module.HaypileFloatingBall()
        try:
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start
            app_gui_module.subprocess.Popen = fake_popen
            ball._probe_backend_via_ipc = lambda: False
            ball._is_port_open = lambda _host, _port: False
            ball._wait_backend_ready = lambda timeout_seconds=5.0: True

            ball.start_api_server()

            self.assertTrue(ball.api_owned_by_gui)
            self.assertEqual(Path(calls[0]["command"][-1]).name, "backend_host.py")
            self.assertEqual(Path(calls[0]["cwd"]), ball.project_root)
            env = calls[0]["env"]
            self.assertEqual(env["HAYPILE_BACKEND_HOST_ALLOW_START"], "1")
        finally:
            ball.api_owned_by_gui = False
            ball.api_process = None
            ball.close()
            self.app.processEvents()
            app_gui_module.subprocess.Popen = previous_popen
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_floating_ball_can_disable_gui_backend_start(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        previous_popen = app_gui_module.subprocess.Popen
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        os.environ["HAYPILE_GUI_ALLOW_BACKEND_START"] = "0"
        calls: list[object] = []
        toasts: list[str] = []

        ball = app_gui_module.HaypileFloatingBall()
        try:
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start
            app_gui_module.subprocess.Popen = lambda *args, **kwargs: calls.append((args, kwargs))
            ball._probe_backend_via_ipc = lambda: False
            ball._is_port_open = lambda _host, _port: False
            ball.show_toast = lambda message, success=True: toasts.append(message)

            ball.start_api_server()

            self.assertFalse(ball.api_owned_by_gui)
            self.assertEqual(calls, [])
            self.assertIn("禁止界面自动启动", toasts[0])
        finally:
            ball.close()
            self.app.processEvents()
            app_gui_module.subprocess.Popen = previous_popen
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def test_ingest_finish_refreshes_visible_panel_and_triggers_feedback(self) -> None:
        previous_start = app_gui_module.HaypileFloatingBall.start_api_server
        app_gui_module.HaypileFloatingBall.start_api_server = lambda self: None
        ball = app_gui_module.HaypileFloatingBall()
        refreshes: list[bool] = []
        previous_refresh = ball.material_panel.refresh
        ball.material_panel.refresh = lambda: refreshes.append(True)
        try:
            ball.material_panel.show()
            self.app.processEvents()

            ball._on_ingest_finished("ok", True)

            self.assertEqual(refreshes, [True])
            self.assertTrue(ball._drop_feedback_active())
        finally:
            ball.material_panel.refresh = previous_refresh
            ball.close()
            self.app.processEvents()
            app_gui_module.HaypileFloatingBall.start_api_server = previous_start

    def _write_project(self, *, state: str) -> tuple[Path, Path, list[str]]:
        project_root = self.tmpdir / "signal-pool-demo"
        source_root = self.tmpdir / "signal-pool-demo-haypile-rehearsal"
        project_root.mkdir()
        source_root.mkdir()
        (project_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
        (project_root / "styles.css").write_text("body{}", encoding="utf-8")
        (project_root / "app.js").write_text("console.log('demo')", encoding="utf-8")

        for path_ref, content in self._file_contents().items():
            source = source_root / path_ref
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(content)
        written_files = list(self._file_contents().keys())
        entries = [
            {
                "path_ref": path_ref,
                "existed_before": False,
                "source_sha256": hashlib.sha256((source_root / path_ref).read_bytes()).hexdigest(),
                "source_size": (source_root / path_ref).stat().st_size,
            }
            for path_ref in written_files
        ]
        self._write_json(
            project_root / ".haypile" / "rollback" / "haypile-real-project-minimal-apply.json",
            {
                "manifest_type": "haypile_real_project_minimal_apply_rollback_manifest",
                "version": "haypile_real_project_minimal_apply_rollback_manifest.v1",
                "source_rehearsal_root": source_root.as_posix(),
                "entries": entries,
            },
        )
        self._write_json(
            project_root / "haypile-rehearsal-reports" / "real-project-minimal-apply-report.json",
            {
                "status": "applied",
                "passed": True,
                "source_rehearsal_root": source_root.as_posix(),
                "written_files": written_files,
            },
        )
        self._write_json(
            project_root / "haypile-rehearsal-reports" / "real-project-minimal-post-apply-verification.json",
            {"status": "verified", "passed": True, "remote_urls": [], "unregistered_assets": []},
        )
        if state == "rolled_back":
            self._write_json(
                project_root / "haypile-rehearsal-reports" / "real-project-minimal-rollback-report.json",
                {
                    "status": "restored",
                    "passed": True,
                    "removed_files": written_files,
                    "remaining_written_files": [],
                },
            )
        return project_root, source_root, written_files

    @staticmethod
    def _picker_preview(
        *,
        project_root: Path,
        project_state: str,
        status_label: str,
        picker_status: str,
        primary_action: str,
        primary_label: str,
    ) -> dict:
        operation_count = 5
        return {
            "preview_type": "haypile_real_project_picker_ui_preview",
            "version": "haypile_real_project_picker_ui_preview.v1",
            "picker_intent": {
                "intent_type": "haypile_real_project_picker_intent",
                "selected_project_name": project_root.name,
                "selected_project_root": project_root.as_posix(),
                "project_state": project_state,
                "project_status_label": status_label,
                "write_allowed": False,
                "execute_allowed": False,
                "auto_apply_allowed": False,
                "auto_rollback_allowed": False,
            },
            "panel_summary": {
                "summary_type": "haypile_real_project_picker_panel_summary",
                "version": "haypile_real_project_picker_panel_summary.v1",
                "project_name": project_root.name,
                "selected_project_root": project_root.as_posix(),
                "picker_status": picker_status,
                "ready": picker_status == "selection_ready",
                "project_state": project_state,
                "panel_status_label": f"{status_label}\n可确认重新投放 {operation_count} 项",
                "primary_action": primary_action,
                "primary_label": primary_label,
                "operation_count": operation_count,
                "blockers": [],
                "actions": ["view_project_picker_details"],
                "compact_prompt": {},
                "contract": {
                    "mode": "display_only_project_picker_panel_summary",
                    "write_allowed": False,
                    "execution_allowed": False,
                    "apply_allowed": False,
                    "rollback_allowed": False,
                    "auto_apply_allowed": False,
                    "auto_rollback_allowed": False,
                    "full_argus_inspect_allowed": False,
                    "worker_allowed": False,
                    "saga_mutation_allowed": False,
                    "task_qa_publish_allowed": False,
                },
                "next_step": "show_reapply_confirmation_ui",
            },
            "confirmation_intent": {},
            "confirmation_included": False,
            "execution_readiness": {},
            "execution_readiness_included": False,
            "execution_result": {},
            "execution_result_included": False,
            "write_allowed": False,
            "execute_allowed": False,
            "auto_apply_allowed": False,
            "auto_rollback_allowed": False,
            "worker_allowed": False,
            "saga_mutation_allowed": False,
            "task_qa_publish_allowed": False,
            "non_executable": True,
            "next_step": "show_reapply_confirmation_ui",
        }

    @staticmethod
    def _file_contents() -> dict[str, bytes]:
        return {
            "haypile-hydration.html": b"<!doctype html><title>Haypile</title>",
            "assets/images/water-drop.svg": b"<svg></svg>",
            "assets/css/hydration-theme.css": b":root { --hydration-primary: #38bdf8; }",
            "public/assets/images/water-drop.svg": b"<svg></svg>",
            "public/assets/css/hydration-theme.css": b":root { --hydration-primary: #38bdf8; }",
        }

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
