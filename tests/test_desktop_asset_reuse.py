from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ctypes
import hashlib
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import Mock, patch

from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QKeyEvent, QKeySequence, QMouseEvent, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

import app_gui
from app.gui import native_drag
from app.core.config import Settings
from app.core.file_lock import InterProcessFileLock
from app.services.asset_collections import read_collections, save_collection, dissolve_collection, set_collection_asset, collection_handoff
from app.services.asset_provenance import write_asset_provenance
from app.services.ingest_service import IngestCandidate, IngestService
from app.services.scanner import manifest_dirty_path, mark_manifest_dirty
from app.services.storage_runtime import StorageRuntimeDB
from tests.test_font_support import write_test_font


class DesktopAssetReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(_env_file=None, STORAGE_DIR=self.root / "storage", VISION_CLASSIFIER_ENABLED=False)
        self.ingest = IngestService(storage_dir=self.settings.STORAGE_DIR, assets_dir=self.settings.ASSETS_DIR,
                                   index_dir=self.settings.INDEX_DIR, themes_dir=self.settings.THEMES_DIR,
                                   manifest_path=self.settings.MANIFEST_PATH, fallback_theme="generic")
        self.png, self.gif, self.font, self.audio, self.svg = (self.root / name for name in
            ("picture.png", "motion.gif", "typeface.ttf", "sound.wav", "icon.svg"))
        with Image.new("RGB", (64, 48), "#4E5F3D") as image:
            image.save(self.png)
            with Image.new("RGB", image.size, "#A67624") as frame:
                image.save(self.gif, save_all=True, append_images=[frame], duration=80, loop=0)
        write_test_font(self.font)
        with wave.open(str(self.audio), "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 800)
        self.svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><rect width="24" height="24" fill="#4E5F3D"/></svg>')
        self.originals = [self.png, self.gif, self.font, self.audio, self.svg]
        result = self.ingest.ingest([IngestCandidate(path) for path in self.originals])
        self.assertTrue(result.success)
        self.ball = app_gui.HaypileFloatingBall(settings=self.settings)
        self.card = self.ball.intake_card
        self.service = self.ball._bundle_service()
        self.bundles = {bundle["sha256"]: bundle for bundle in self.service.list_bundles()}

    def tearDown(self):
        self.ball.close()
        deadline = time.monotonic() + 3
        while not self.ball._cleanup_done and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(self.ball._cleanup_done)
        self.app.clipboard().clear()
        self.temp.cleanup()

    def bundle_for(self, original):
        return self.bundles[hashlib.sha256(original.read_bytes()).hexdigest()]

    def test_provider_switch_stops_real_worker_before_next_asset_and_discards_late_result(self):
        import threading
        from app.services.style_classifier import StyleClassificationResult

        second = self.root / "second.png"
        Image.new("RGB", (64, 48), "blue").save(second)
        self.assertTrue(self.ingest.ingest([IngestCandidate(second)]).success)
        bundles = [self.bundle_for(self.png), self.service.get_bundle(hashlib.sha256(second.read_bytes()).hexdigest())]
        self.ball._set_ai_provider_mode("local")
        worker = app_gui.AIBatchWorker("old-provider", bundles, self.settings.ASSETS_DIR,
            self.ball._current_ai_provider_config(), settings=self.settings)
        entered, release = threading.Event(), threading.Event()
        calls = []
        async def held_response(path, **kwargs):
            calls.append(path)
            entered.set()
            if not release.wait(4):
                raise TimeoutError("test release timeout")
            return StyleClassificationResult(theme_id="generic", theme_confidence=0,
                role="hero_image", role_confidence=.9, source="model", reason="late response")

        worker.style_classifier.classify_image = held_response
        self.ball.ai_batch_worker = worker
        self.ball._ai_batch_result = None
        self.ball._ai_batch_queue = ["old-queued-batch"]
        worker.finished_signal.connect(self.ball._remember_ai_batch_result)
        worker.finished.connect(self.ball._finish_ai_batch)
        try:
            worker.start()
            self.assertTrue(entered.wait(2))
            self.ball._set_ai_provider_mode("api")
            self.assertTrue(worker.isInterruptionRequested())
            self.assertIs(self.ball.ai_batch_worker, worker)
            self.assertEqual(self.ball._ai_batch_queue, [])
            release.set()
            self.assertTrue(worker.wait(2000))
            deadline = time.monotonic() + 2
            while self.ball.ai_batch_worker is not None and time.monotonic() < deadline:
                QTest.qWait(5)
            self.assertIsNone(self.ball.ai_batch_worker)
            self.assertEqual(len(calls), 1)
            for bundle in bundles:
                current = self.service.get_bundle(bundle["id"])
                self.assertEqual(current["role"], "unknown")
                self.assertEqual(current["status"], "pending")
                self.assertFalse(current.get("ai_suggestions"))
        finally:
            release.set()
            if self.ball.ai_batch_worker is not None:
                worker.wait(4000)
                self.ball._shutdown_ai_batch_worker()

    def test_ai_off_full_workflow_with_model_and_network_unavailable(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.v1.bundles import router, get_bundle_service
        from app.services import library_backup
        import mcp_server

        original = self.root / "offline.webp"
        Image.new("RGB", (48, 48), "blue").save(original)
        source_bytes = original.read_bytes()
        identifier = hashlib.sha256(source_bytes).hexdigest()
        with patch("socket.socket.connect", side_effect=AssertionError("network unavailable")), \
             patch.object(app_gui, "StyleClassifier", side_effect=AssertionError("AI must stay off")), \
             patch.object(app_gui, "AIBatchWorker", side_effect=AssertionError("AI must stay off")):
            self.assertFalse(self.ball.ai_enabled)
            worker = app_gui.IngestWorker([original], self.settings.ASSETS_DIR, settings=self.settings)
            worker.batch_signal.connect(self.ball._on_ingest_batch)
            results = []
            worker.finished_signal.connect(lambda message, ok: results.append(ok))
            worker.run()
            self.assertEqual(results, [True])
            self.assertIsNone(self.ball.ai_batch_worker)
            self.assertEqual(self.ball._ai_batch_queue, [])
            self.assertTrue(self.ingest.ingest([IngestCandidate(original)]).duplicate_only)

            bundle = self.service.get_bundle(identifier)
            self.ball._handle_quick_menu_action("assets")
            panel = self.ball.material_panel
            panel.search_input.setText(identifier)
            panel.refresh()
            self.assertEqual(len(panel._visible_items), 1)
            self.card.show_bundle(bundle)
            self.card._copy_bundle_original()
            copied = Path(self.app.clipboard().mimeData().urls()[0].toLocalFile())
            self.assertEqual(copied.read_bytes(), source_bytes)
            self.service.set_bundle_role(identifier, "hero_image")
            self.assertEqual(self.service.get_bundle(identifier)["status"], "ready")

            # Exercise the real MCP dispatch and HTTP route in-process, without a network listener.
            api = FastAPI()
            api.include_router(router, prefix="/api/v1")
            api.dependency_overrides[get_bundle_service] = lambda: self.service
            with TestClient(api) as client, patch.object(mcp_server, "get_json", side_effect=lambda path: client.get(path).json()):
                listed = mcp_server.call_tool("haypile_list_bundles", {"status": "ready"})
                self.assertIn(identifier, [item["id"] for item in listed])
                self.assertEqual(mcp_server.call_tool("haypile_get_bundle", {"bundle_id": identifier})["sha256"], identifier)

            archive = self.root / "offline.haypile.zip"
            count = library_backup.create_backup(self.settings, archive)
            restored = Settings(_env_file=None, STORAGE_DIR=self.root / "restored")
            self.assertEqual(library_backup.restore_backup(restored, archive), count)
            reopened = app_gui.BundleService(assets_dir=restored.ASSETS_DIR, themes_dir=restored.THEMES_DIR,
                manifest_path=restored.MANIFEST_PATH, runtime_db_path=restored.INDEX_DIR / "storage_runtime.db")
            recovered = reopened.get_bundle(identifier)
            self.assertEqual(recovered["role"], "hero_image")
            self.assertEqual((restored.ASSETS_DIR / recovered["source_key"]).read_bytes(), source_bytes)

    def mouse(self, kind, x, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.LeftButton):
        event = QMouseEvent(kind, QPointF(x, 12), QPointF(x, 12), button, buttons, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(self.card.preview, event)

    def choose_menu(self, button, index):
        def choose():
            menu = self.app.activePopupWidget()
            self.assertIsInstance(menu, app_gui.QMenu)
            menu.actions()[index].trigger()
            menu.close()
        QTimer.singleShot(0, choose)
        button.click()

    def test_recent_duplicate_intake_moves_to_front_without_touching_original(self):
        old = self.bundle_for(self.png)
        source = self.settings.ASSETS_DIR / old["source_key"]
        old_stat = source.stat()
        result = self.ingest.ingest([IngestCandidate(self.png)])
        self.assertTrue(result.duplicate_only)
        # An unfinished batch must not reorder recent assets.
        runtime = StorageRuntimeDB(self.service.runtime_db_path)
        batch = runtime.begin_batch()
        runtime.record_batch_asset(batch, self.bundle_for(self.font)["sha256"], 0)
        recent = self.service.list_recent_bundles()
        self.assertEqual(recent[0]["id"], old["id"])
        self.assertEqual(len(recent), 3)
        self.assertEqual(len({item["id"] for item in recent}), 3)
        self.assertEqual(source.stat().st_mtime_ns, old_stat.st_mtime_ns)
        self.card.show_recent()
        self.assertFalse(self.card.recent_layout.itemAt(0).widget().icon().isNull())
        self.card.recent_layout.itemAt(0).widget().click()
        self.assertEqual(self.card.bundle["id"], old["id"])
        self.assertEqual(self.card.store_button.text(), "复制原文件")
        self.assertFalse(self.ball.quick_menu.isVisible())

    def test_copy_original_files_with_keyboard_preserves_formats_and_pending_state(self):
        for original in self.originals:
            with self.subTest(format=original.suffix):
                bundle = self.bundle_for(original)
                self.card.show_bundle(bundle)
                deadline = time.monotonic() + 2
                while self.card.font_preview_worker is not None and time.monotonic() < deadline:
                    QTest.qWait(5)
                status = self.card.status.text()
                self.card.store_button.setFocus()
                QTest.keyClick(self.card.store_button, Qt.Key.Key_Return)
                mime = self.app.clipboard().mimeData()
                self.assertEqual(len(mime.urls()), 1)
                copied = Path(mime.urls()[0].toLocalFile())
                self.assertEqual(copied, (self.settings.ASSETS_DIR / bundle["source_key"]).resolve())
                self.assertEqual(copied.read_bytes(), original.read_bytes())
                self.assertFalse(mime.hasImage())  # A GIF or SVG must not become a preview bitmap.
                self.assertEqual(self.card.status.text(), status)
                self.assertEqual(self.card.store_button.text(), "已复制原文件")
                self.assertEqual(self.service.get_bundle(bundle["id"])["status"], bundle["status"])
                self.assertEqual(self.card.mode, "bundle")
                self.assertEqual(self.card.size(), app_gui.QSize(280, 240))
                self.assertTrue(self.card.rect().contains(self.card.store_button.geometry()))
                self.assertFalse(self.ball.quick_menu.isVisible())

    def test_unavailable_or_changed_files_do_not_replace_clipboard_or_start_drag(self):
        bundle = self.bundle_for(self.png)
        source = self.settings.ASSETS_DIR / bundle["source_key"]
        data = source.read_bytes()
        outside = self.root / "outside.png"
        outside.write_bytes(data)
        for failure in ("dirty", "missing", "changed", "symlink"):
            with self.subTest(failure=failure):
                self.card.show_bundle(bundle)
                self.app.clipboard().setText("keep this clipboard")
                if failure == "dirty":
                    mark_manifest_dirty(self.settings.MANIFEST_PATH)
                elif failure == "missing":
                    source.unlink()
                elif failure == "changed":
                    source.write_bytes(b"changed file")
                else:
                    source.unlink()
                    source.symlink_to(outside)
                with patch.object(app_gui, "QDrag") as drag:
                    self.card.store_button.click()
                    self.card._drag_bundle_original()
                    drag.assert_not_called()
                self.assertEqual(self.app.clipboard().text(), "keep this clipboard")
                self.assertIn("不可用", self.card.status.text())
                manifest_dirty_path(self.settings.MANIFEST_PATH).unlink(missing_ok=True)
                source.unlink(missing_ok=True)
                source.write_bytes(data)

    def test_preview_drag_requires_threshold_and_offers_copy_only(self):
        bundle = self.bundle_for(self.gif)
        self.card.show_bundle(bundle)
        with patch.object(app_gui, "QDrag") as drag_type:
            drag = drag_type.return_value
            drag.exec.return_value = Qt.DropAction.IgnoreAction
            self.mouse(QEvent.Type.MouseButtonPress, 10, Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseMove, 11)
            drag_type.assert_not_called()
            self.mouse(QEvent.Type.MouseMove, 10 + QApplication.startDragDistance())
            mime = drag.setMimeData.call_args.args[0]
            self.assertEqual(Path(mime.urls()[0].toLocalFile()).read_bytes(), self.gif.read_bytes())
            drag.exec.assert_called_once_with(Qt.DropAction.CopyAction)
            self.assertEqual(self.card.mode, "bundle")  # Cancelling a drag does not remove the asset.
            self.mouse(QEvent.Type.MouseButtonRelease, 20, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
            self.assertIsNone(self.card._drag_origin)
            drag.exec.return_value = Qt.DropAction.CopyAction
            self.card._drag_bundle_original()
            self.assertFalse(self.card.isVisible())
            self.assertEqual((self.settings.ASSETS_DIR / bundle["source_key"]).read_bytes(), self.gif.read_bytes())

    def test_font_export_uses_atomic_export_and_keeps_card_during_dialog(self):
        self.card.show_bundle(self.bundle_for(self.font))
        self.card.info_button.setChecked(True)
        self.assertTrue(self.card.export_button.isVisible())
        self.assertTrue(self.card.rect().contains(self.card.export_button.geometry()))
        target = self.root / "exported.ttf"
        outside = QWidget()
        def choose(*_args):
            self.card._application_state_changed(Qt.ApplicationState.ApplicationInactive)
            self.card.eventFilter(outside, QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
            self.card._poll_outside()
            self.assertTrue(self.card.isVisible())
            return str(target), ""
        with patch.object(app_gui.QFileDialog, "getSaveFileName", side_effect=choose):
            self.card.export_button.click()
        self.assertEqual(target.read_bytes(), self.font.read_bytes())
        self.assertEqual(self.card.height(), 320)
        self.assertFalse(self.card._exporting)
        original_target = b"existing destination"
        target.write_bytes(original_target)
        with patch.object(app_gui.QFileDialog, "getSaveFileName", return_value=("", "")):
            self.card.export_button.click()
        self.assertEqual(target.read_bytes(), original_target)
        protected = self.settings.STORAGE_DIR / "protected.ttf"
        with patch.object(app_gui.QFileDialog, "getSaveFileName", return_value=(str(protected), "")):
            self.card.export_button.click()
        self.assertFalse(protected.exists())
        self.assertIn("导出失败", self.card.status.text())
        outside.close()

    def test_font_export_keeps_original_identity_when_nested_events_replace_the_card(self):
        self.card.show_bundle(self.bundle_for(self.font))
        target = self.root / "exported-after-replacement.ttf"
        replacement = self.bundle_for(self.png)
        replacement_status = []

        def choose(*_args):
            loop = QEventLoop()
            def replace():
                self.card.show_bundle(replacement)
                replacement_status.append((self.card.status.text(), self.card.status.toolTip()))
                loop.quit()
            QTimer.singleShot(0, replace)
            loop.exec()
            return str(target), ""

        with patch.object(app_gui.QFileDialog, "getSaveFileName", side_effect=choose):
            self.card._export_bundle_font()
        self.assertEqual(target.read_bytes(), self.font.read_bytes())
        self.assertEqual(self.card.bundle["id"], replacement["id"])
        self.assertEqual((self.card.status.text(), self.card.status.toolTip()), replacement_status[0])
        self.assertFalse(self.card._exporting)

    def _check_managed_shutdown_during_native_operation(self, mode, *, choose_target=False):
        ball = app_gui.HaypileFloatingBall(settings=self.settings, managed_shutdown=True)
        card = ball.intake_card
        original = self.font if mode == "export" else self.gif
        bundle = self.bundle_for(original)
        source = self.settings.ASSETS_DIR / bundle["source_key"]
        card.show_bundle(bundle)
        preview_deadline = time.monotonic() + 2
        while card.font_preview_worker is not None and time.monotonic() < preview_deadline:
            QTest.qWait(5)
        self.assertIsNone(card.font_preview_worker)
        candidate_path = self.root / f"shutdown-{mode}-candidate.png"
        candidate_path.write_bytes(self.png.read_bytes())
        with Image.open(candidate_path) as image:
            candidate = app_gui.WebImageCandidate(candidate_path, hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                {"width": 64, "height": 48, "mime_type": "image/png"}, "https://example.org", "image", "New image",
                image.convert("RGBA").tobytes(), 64, 48)
        events, errors, observations = [], [], []
        state = {"requested": False, "window_ready": False, "completed": False}
        backend = SimpleNamespace(is_stopped=True, stop=Mock())
        active_drag = None
        export_target = self.root / "shutdown-export.ttf"
        previous_quit_policy = self.app.quitOnLastWindowClosed()
        self.app.setQuitOnLastWindowClosed(False)

        # Match main(): an already stopped backend is the fastest completion path.
        def maybe_complete_shutdown():
            if state["completed"] or not state["window_ready"] or not backend.is_stopped:
                return
            state["completed"] = True
            ball.complete_shutdown()
            events.append("completed")
            QTimer.singleShot(0, QCoreApplication.quit)

        def request_shutdown():
            state["requested"] = True
            backend.stop()
            maybe_complete_shutdown()

        def mark_window_ready():
            events.append("ready")
            state["window_ready"] = True
            maybe_complete_shutdown()

        ball.shutdown_requested.connect(request_shutdown)
        ball.shutdown_ready.connect(mark_window_ready)
        self.app.aboutToQuit.connect(ball.shutdown)
        card.destroyed.connect(lambda: events.append("card_destroyed"))

        def native_loop():
            loop = QEventLoop()
            observation_timer = QTimer(loop)
            observation_timer.setSingleShot(True)
            def observe():
                observations.append({
                    "card_valid": isValid(card), "status_valid": isValid(card.status),
                    "drag_valid": isValid(active_drag) if active_drag is not None else True,
                    "native_operation_active": card._exporting or card._dragging,
                    "cleanup_done": ball._cleanup_done, "window_ready": state["window_ready"],
                    "card_keeps_candidate": card.candidates == [candidate],
                    "ball_keeps_candidate": ball._web_candidates == [candidate],
                    "candidate_file_exists": candidate_path.is_file(),
                })
                events.append("observed_active_operation")
                loop.quit()
            observation_timer.timeout.connect(observe)
            def begin_shutdown():
                # Native loops may deliver the events that replace a card first.
                ball._web_candidates = [candidate]
                card.show_candidates([], [candidate])
                ball.shutdown()
                observation_timer.start(20)
            QTimer.singleShot(0, begin_shutdown)
            events.append("native_enter")
            loop.exec()
            observation_timer.stop()
            events.append("native_return")

        def fake_dialog(*_args):
            native_loop()
            return str(export_target) if choose_target else "", ""

        def fake_drag(drag, _window):
            nonlocal active_drag
            active_drag = drag
            native_loop()
            return True

        def operation():
            try:
                if mode == "export":
                    card._export_bundle_font()
                else:
                    card._drag_bundle_original()
                events.append("operation_return")
            except Exception as exc:
                errors.append(exc)

        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(QCoreApplication.quit)
        try:
            with patch.object(app_gui.QFileDialog, "getSaveFileName", side_effect=fake_dialog), \
                 patch.object(app_gui, "execute_copy_drag", side_effect=fake_drag), \
                 patch.object(app_gui, "export_font_original", wraps=app_gui.export_font_original) as export:
                QTimer.singleShot(0, operation)
                watchdog.start(2000)
                outer_code = self.app.exec()
                export.assert_not_called()  # Cancellation and shutdown both prevent destination writes.
            self.assertEqual(outer_code, 0)
            self.assertEqual(errors, [])
            self.assertEqual(observations, [{
                "card_valid": True, "status_valid": True, "drag_valid": True,
                "native_operation_active": True, "cleanup_done": False, "window_ready": False,
                "card_keeps_candidate": True, "ball_keeps_candidate": True, "candidate_file_exists": True,
            }])
            self.assertTrue(state["completed"])
            self.assertEqual(events, ["native_enter", "observed_active_operation", "native_return",
                "operation_return", "ready", "completed", "card_destroyed"])
            backend.stop.assert_called_once_with()
            self.assertEqual(source.read_bytes(), original.read_bytes())
            self.assertFalse(export_target.exists())
            self.assertFalse(candidate_path.exists())  # Deferred cleanup still runs after the native loop.
        finally:
            watchdog.stop()
            if isValid(ball):
                self.app.aboutToQuit.disconnect(ball.shutdown)
                if not ball._cleanup_done:
                    ball._managed_shutdown = False
                    ball.shutdown()
                    ball._poll_shutdown()
                ball.close()
            self.app.setQuitOnLastWindowClosed(previous_quit_policy)

    def test_managed_shutdown_waits_for_cancelled_export_nested_loop(self):
        self._check_managed_shutdown_during_native_operation("export")

    def test_managed_shutdown_waits_for_drag_nested_loop(self):
        self._check_managed_shutdown_during_native_operation("drag")

    def test_managed_shutdown_prevents_export_when_dialog_returns_a_target(self):
        self._check_managed_shutdown_during_native_operation("export", choose_target=True)

    def test_drag_keeps_card_until_the_system_drag_finishes(self):
        bundle = self.bundle_for(self.gif)
        self.card.show_bundle(bundle)
        outside = QWidget()
        self.app.clipboard().setText("keep clipboard")
        def during_drag(_actions):
            self.card._application_state_changed(Qt.ApplicationState.ApplicationInactive)
            self.card._poll_outside()
            self.card.eventFilter(outside, QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
            self.card.eventFilter(self.card.preview, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                Qt.KeyboardModifier.NoModifier))
            self.assertTrue(self.card.isVisible())
            self.assertEqual(self.card.mode, "bundle")
            return Qt.DropAction.IgnoreAction
        with patch.object(app_gui, "QDrag") as drag_type, \
             patch.object(self.card.desktop_input, "buttons", return_value=(1, 0, 0)) as buttons, \
             patch.object(self.card.desktop_input, "escape", return_value=False), \
             patch.object(app_gui.QCursor, "pos", return_value=QPoint(-10000, -10000)):
            drag_type.return_value.exec.side_effect = during_drag
            self.card._drag_bundle_original()
            self.assertFalse(self.card._dragging)
            self.assertEqual(self.app.clipboard().text(), "keep clipboard")
            self.card._poll_outside()  # The press that began the cancelled drag is consumed.
            self.assertTrue(self.card.isVisible())
            drag_type.return_value.exec.side_effect = RuntimeError("drag failed")
            with self.assertRaises(RuntimeError):
                self.card._drag_bundle_original()
            self.assertFalse(self.card._dragging)
            buttons.return_value = (2, 0, 0)
            self.card._poll_outside()  # A new outside click still closes the card normally.
            self.assertFalse(self.card.isVisible())
        self.assertEqual((self.settings.ASSETS_DIR / bundle["source_key"]).read_bytes(), self.gif.read_bytes())
        outside.close()

    def test_completed_drag_does_not_dismiss_a_new_card(self):
        bundle = self.bundle_for(self.gif)
        with Image.open(self.png) as image:
            candidate = app_gui.WebImageCandidate(self.png, hashlib.sha256(self.png.read_bytes()).hexdigest(),
                {"width":64, "height":48, "mime_type":"image/png"}, "https://example.org", "image", "New image",
                image.convert("RGBA").tobytes(), 64, 48)
        replacements = (
            ("recent", self.card.show_recent),
            ("bundle", lambda: self.card.show_bundle(self.bundle_for(self.png))),
            ("images", lambda: self.card.show_candidates([], [candidate])),
        )
        for mode, replace in replacements:
            with self.subTest(mode=mode):
                self.card.show_bundle(bundle)
                def during_drag(_actions):
                    if mode == "images":
                        self.ball._web_candidates = [candidate]
                    replace()  # The native drag loop still delivers application events.
                    return Qt.DropAction.CopyAction
                with patch.object(app_gui, "QDrag") as drag_type:
                    drag_type.return_value.exec.side_effect = during_drag
                    self.card._drag_bundle_original()
                self.assertTrue(self.card.isVisible())
                self.assertEqual(self.card.mode, mode)
                if mode == "images":
                    self.assertEqual(self.card.candidates, [candidate])
                    self.assertEqual(self.ball._web_candidates, [candidate])
                    self.assertTrue(candidate.path.is_file())
        self.assertEqual((self.settings.ASSETS_DIR / bundle["source_key"]).read_bytes(), self.gif.read_bytes())

    def test_cocoa_completion_preserves_cancel_and_restores_the_native_callback(self):
        bundle = self.bundle_for(self.gif)
        signature = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p, native_drag._NSPoint, ctypes.c_ulong)
        forwarded = []
        original = signature(lambda *arguments: forwarded.append(arguments[-1]))
        original_address = ctypes.cast(original, ctypes.c_void_p).value
        for outcome in ("copy", "cancel", "other_view", "new_card", "exception", "changed_abi", "missing_method"):
            with self.subTest(outcome=outcome):
                self.card.show_bundle(bundle)
                view = int(self.card.winId())
                installed = original_address
                replacements = []
                def replace(_method, implementation):
                    nonlocal installed
                    previous = installed
                    installed = implementation.value if isinstance(implementation, ctypes.c_void_p) else implementation
                    replacements.append(installed)
                    return previous
                objc = SimpleNamespace(
                    object_getClass=Mock(return_value=2),
                    sel_registerName=Mock(return_value=3),
                    class_getInstanceMethod=Mock(return_value=0 if outcome == "missing_method" else 4),
                    method_getTypeEncoding=Mock(return_value=b"changed" if outcome == "changed_abi" else b"v48@0:8@16{CGPoint=dd}24Q40"),
                    method_getImplementation=Mock(return_value=original_address),
                    method_setImplementation=Mock(side_effect=replace),
                )
                forwarded.clear()
                def during_drag(actions):
                    self.assertEqual(actions, Qt.DropAction.CopyAction)
                    if outcome == "exception":
                        raise RuntimeError("drag failed")
                    if outcome not in ("changed_abi", "missing_method"):
                        receiver = view + 1 if outcome == "other_view" else view
                        operation = 0 if outcome == "cancel" else 1
                        signature(installed)(receiver, 3, None, native_drag._NSPoint(), operation)
                    if outcome == "new_card":
                        self.card.show_recent()
                    return Qt.DropAction.IgnoreAction
                with patch.object(native_drag.QGuiApplication, "platformName", return_value="cocoa"), \
                     patch.object(native_drag.ctypes, "CDLL", return_value=objc), \
                     patch.object(app_gui, "QDrag") as drag_type:
                    drag_type.return_value.exec.side_effect = during_drag
                    if outcome == "exception":
                        with self.assertRaises(RuntimeError):
                            self.card._drag_bundle_original()
                    else:
                        self.card._drag_bundle_original()
                    drag_type.return_value.exec.assert_called_once_with(Qt.DropAction.CopyAction)
                self.assertEqual(installed, original_address)
                self.assertEqual(len(replacements), 0 if outcome in ("changed_abi", "missing_method") else 2)
                self.assertEqual(forwarded, [] if outcome in ("exception", "changed_abi", "missing_method") else [0 if outcome == "cancel" else 1])
                self.assertEqual(self.card.isVisible(), outcome != "copy")
                self.assertFalse(self.card._dragging)
                if outcome == "new_card":
                    self.assertEqual(self.card.mode, "recent")
        self.assertEqual((self.settings.ASSETS_DIR / bundle["source_key"]).read_bytes(), self.gif.read_bytes())

    def test_closed_or_missing_card_has_no_file_actions_and_english_labels_fit(self):
        bundle = self.bundle_for(self.font)
        self.card.show_bundle(bundle)
        self.card.dismiss("close")
        self.app.clipboard().setText("keep this clipboard")
        with patch.object(app_gui.QFileDialog, "getSaveFileName") as dialog, patch.object(app_gui, "QDrag") as drag:
            self.card.store_button.click()
            self.card.export_button.click()
            self.card._drag_bundle_original()
            dialog.assert_not_called()
            drag.assert_not_called()
        self.assertEqual(self.app.clipboard().text(), "keep this clipboard")
        (self.settings.ASSETS_DIR / bundle["source_key"]).unlink()
        missing = self.service.get_bundle(bundle["id"])
        self.card.show_bundle(missing)
        self.assertFalse(self.card.store_button.isEnabled())
        self.assertFalse(self.card.export_button.isEnabled())
        self.assertEqual(self.card.preview.cursor().shape(), Qt.CursorShape.ArrowCursor)
        app_gui.set_ui_language("en")
        try:
            self.card.show_bundle(self.bundle_for(self.png))
            self.assertEqual(self.card.store_button.text(), "Copy original file")
            self.assertLess(self.card.store_button.fontMetrics().horizontalAdvance(self.card.store_button.text()), self.card.store_button.width())
            self.assertFalse(self.card.export_button.isVisible())
        finally:
            app_gui.set_ui_language("auto")

    def test_collection_storage_reopens_without_copying_or_confirming_assets(self):
        path = self.settings.STORAGE_DIR / "collections.json"
        self.assertEqual(read_collections(path), [])
        self.assertFalse(path.exists())
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.settings.ASSETS_DIR.rglob("*") if p.is_file()}
        font, image = self.bundle_for(self.font), self.bundle_for(self.png)
        group = save_collection(path, "个人网站", asset_id=font["id"])
        set_collection_asset(path, group["id"], image["id"], included=True)
        set_collection_asset(path, group["id"], image["id"], included=True)
        self.assertEqual(read_collections(path)[0]["asset_ids"], [font["id"], image["id"]])
        set_collection_asset(path, group["id"], image["id"], included=False)
        self.assertEqual(read_collections(path)[0]["asset_ids"], [font["id"]])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.settings.ASSETS_DIR.rglob("*") if p.is_file()})
        self.assertEqual(self.service.get_bundle(font["id"])["status"], "pending")
        self.ball.material_panel.refresh()
        self.assertGreater(self.ball.material_panel.collection_picker.findData(group["id"]), 0)

    def test_collection_storage_rejects_corruption_busy_and_failed_replace(self):
        path = self.settings.STORAGE_DIR / "collections.json"
        group = save_collection(path, "Keep", asset_id=self.bundle_for(self.png)["id"])
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "name_exists"):
            save_collection(path, "keep")
        lock = InterProcessFileLock(path.with_suffix(".lock"))
        self.assertTrue(lock.acquire(timeout=0))
        try:
            with self.assertRaises(TimeoutError):
                save_collection(path, "busy")
        finally:
            lock.release()
        with patch("app.services.json_io.os.replace", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                set_collection_asset(path, group["id"], self.bundle_for(self.gif)["id"], included=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(".collections.json.*.tmp")), [])
        for invalid in ('{broken', '{"version":2,"collections":[]}',
                        '{"version":1,"collections":[{"id":"' + group["id"] + '","name":"Keep","asset_ids":["../../outside"]}]}'):
            path.write_text(invalid)
            with self.assertRaises(ValueError):
                save_collection(path, "new")
            self.assertEqual(path.read_text(), invalid)
        self.app.clipboard().setText("keep clipboard")
        panel = self.ball.material_panel
        panel._collection_id = group["id"]
        panel.refresh()
        self.assertFalse(panel.new_collection_button.isEnabled())
        self.assertEqual(panel._all_recent_items, [])  # A broken group never expands to all assets.
        panel._copy_collection()
        self.assertEqual(self.app.clipboard().text(), "keep clipboard")

    def test_collection_rename_and_dissolve_preserve_members_and_originals_on_failure(self):
        path = self.settings.STORAGE_DIR / "collections.json"
        image = self.bundle_for(self.png)
        group = save_collection(path, "Keep", asset_id=image["id"])
        other = save_collection(path, "Other")
        before = {p: p.read_bytes() for p in self.settings.ASSETS_DIR.rglob("*") if p.is_file()}
        renamed = save_collection(path, " Renamed ", collection_id=group["id"])
        self.assertEqual(renamed, {**group, "name": "Renamed"})
        self.assertEqual(save_collection(path, "RENAMED", collection_id=group["id"])["id"], group["id"])
        original = path.read_bytes()
        for name, target in (("other", group["id"]), ("", group["id"]), ("new", "0" * 32)):
            with self.assertRaises(ValueError):
                save_collection(path, name, collection_id=target)
            self.assertEqual(path.read_bytes(), original)
        with patch("app.services.json_io.os.replace", side_effect=OSError("unavailable")):
            for operation in (lambda: save_collection(path, "Failed", collection_id=group["id"]),
                              lambda: dissolve_collection(path, group["id"])):
                with self.assertRaises(OSError):
                    operation()
                self.assertEqual(path.read_bytes(), original)
        lock = InterProcessFileLock(path.with_suffix(".lock"))
        self.assertTrue(lock.acquire(timeout=0))
        try:
            with self.assertRaises(TimeoutError):
                dissolve_collection(path, group["id"])
        finally:
            lock.release()
        dissolve_collection(path, group["id"])
        self.assertEqual(read_collections(path), [other])
        with self.assertRaisesRegex(ValueError, "not_found"):
            dissolve_collection(path, group["id"])
        path.write_text("{broken")
        with self.assertRaises(ValueError):
            dissolve_collection(path, other["id"])
        self.assertEqual(path.read_text(), "{broken")
        self.assertEqual(before, {p: p.read_bytes() for p in self.settings.ASSETS_DIR.rglob("*") if p.is_file()})
        self.assertEqual(self.service.get_bundle(image["id"])["status"], "pending")

    def test_collection_menu_renames_inline_and_requires_confirmation_to_dissolve(self):
        panel = self.ball.material_panel
        path = panel._collections_path()
        group = save_collection(path, "个人网站", asset_id=self.bundle_for(self.font)["id"])
        save_collection(path, "已有组合")
        self.ball._handle_quick_menu_action("assets")
        panel.collection_picker.setCurrentIndex(panel.collection_picker.findData(group["id"]))
        self.choose_menu(panel.new_collection_button, 1)
        self.assertEqual(panel.collection_name.text(), group["name"])
        panel.collection_name.setText("已有组合")
        QTest.keyClick(panel.collection_name, Qt.Key.Key_Return)
        self.assertTrue(panel.collection_notice.isVisible())
        self.assertIn("已存在", panel.collection_notice.text())
        panel.collection_name.setText("作品集")
        with patch.object(panel, "_toast_callback") as toast:
            panel.collection_save.click()
            toast.assert_called_once_with("组合已重命名", tone="success")
        self.assertEqual(read_collections(path)[0], {**group, "name": "作品集"})
        self.assertEqual(panel.collection_picker.currentText(), "作品集")
        self.choose_menu(panel.new_collection_button, 1)
        panel.collection_picker.setCurrentIndex(panel.collection_picker.findText("已有组合"))
        self.assertFalse(panel.collection_editor.isVisible())
        panel.collection_picker.setCurrentIndex(panel.collection_picker.findData(group["id"]))
        self.choose_menu(panel.new_collection_button, 1)
        self.ball.quick_menu.hide()
        panel.set_batch_scope("all")
        self.assertTrue(panel.collection_editor.isHidden())
        self.assertEqual(panel._collection_edit_id, "")
        self.ball._handle_quick_menu_action("assets")
        panel.collection_picker.setCurrentIndex(panel.collection_picker.findData(group["id"]))
        self.choose_menu(panel.new_collection_button, 2)
        self.assertTrue(panel.collection_name.isReadOnly())
        self.assertEqual(panel.collection_name.text(), "作品集")
        self.assertIn("原素材仍保留", panel.collection_notice.text())
        self.assertEqual(len(read_collections(path)), 2)
        QTest.keyClick(panel.collection_cancel, Qt.Key.Key_Escape)
        self.assertFalse(panel.collection_editor.isVisible())
        self.assertFalse(panel.collection_name.isReadOnly())
        self.assertEqual(len(read_collections(path)), 2)
        self.choose_menu(panel.new_collection_button, 2)
        with patch("app.services.json_io.os.replace", side_effect=OSError("unavailable")):
            panel.collection_save.click()
        self.assertTrue(panel.collection_editor.isVisible())
        self.assertEqual(panel._collection_id, group["id"])
        panel.collection_save.click()
        self.assertEqual(len(read_collections(path)), 1)
        self.assertEqual(panel._collection_id, "")
        self.assertEqual(len(panel._all_recent_items), len(self.originals))
        self.assertTrue((self.settings.ASSETS_DIR / self.bundle_for(self.font)["source_key"]).is_file())
        self.assertFalse(self.card.isVisible())
        self.assertEqual(self.ball.quick_menu.size(), app_gui.QSize(620, 520))

    def test_search_combines_font_source_and_usage_without_changing_group_members(self):
        panel = self.ball.material_panel
        font = self.bundle_for(self.font)
        self.service.update_bundle_font(font["id"], font_usage="body")
        source = self.settings.ASSETS_DIR / font["source_key"]
        write_asset_provenance(source, {"origin_url": "https://example.com/font?token=private", "font_usage": "body"})
        group = save_collection(panel._collections_path(), "Fonts", asset_id=font["id"])
        self.ball._handle_quick_menu_action("assets")
        panel.collection_picker.setCurrentIndex(panel.collection_picker.findData(group["id"]))
        for query, count in (("acceptance example.com body", 1), (" REGULAR  EXAMPLE.COM ", 1), ("example.com missingword", 0), ("", 1)):
            panel.search_input.setText(query)
            panel.refresh()
            self.assertEqual(len(panel._visible_items), count, query)
        self.assertTrue(panel.search_input.isClearButtonEnabled())
        self.assertIn("Haypile Acceptance", panel.item_labels[0].toolTip())
        self.assertIn("example.com", panel.item_labels[0].toolTip())
        self.assertNotIn("private", panel.item_labels[0].toolTip())
        self.assertEqual(read_collections(panel._collections_path())[0]["asset_ids"], [font["id"]])

    def test_font_names_remain_literal_in_tooltips_and_do_not_create_shortcuts(self):
        font = self.bundle_for(self.font)
        name = "<b>R&D</b>"
        font = {**font, "font_metadata": {**font["font_metadata"], "family": name}}
        document = QTextDocument()
        self.card.show_bundle(font)
        document.setHtml(self.card.title.toolTip())
        self.assertEqual(document.toPlainText(), name)
        with patch.object(self.ball, "_bundle_service", return_value=self.service), \
             patch.object(self.service, "list_recent_bundles", return_value=[font]):
            self.card.show_recent()
            button = self.card.recent_layout.itemAt(0).widget()
            document.setHtml(button.toolTip())
            self.assertEqual(document.toPlainText(), name + " · Regular")
            self.assertTrue(QKeySequence.mnemonic(button.text()).isEmpty())
        panel = self.ball.material_panel
        with patch.object(panel, "_bundle_service", return_value=self.service), \
             patch.object(self.service, "list_bundles", return_value=[font]):
            self.ball._handle_quick_menu_action("assets")
        button = panel.item_labels[0]
        document.setHtml(button.toolTip())
        self.assertTrue(document.toPlainText().startswith(name + " Regular\n"))
        self.assertTrue(QKeySequence.mnemonic(button.text()).isEmpty())

    def test_collection_gui_creates_from_selected_asset_and_adds_later_intake(self):
        panel = self.ball.material_panel
        self.ball._handle_quick_menu_action("assets")
        panel.set_batch_scope("all")
        panel._set_filter_mode("font")
        panel.item_labels[0].click()
        self.choose_menu(panel.collection_member_button, -1)
        self.assertTrue(panel.collection_editor.isVisible())
        panel.collection_name.setText("个人网站")
        QTest.keyClick(panel.collection_name, Qt.Key.Key_Return)
        group = read_collections(panel._collections_path())[0]
        self.assertEqual(group["asset_ids"], [self.bundle_for(self.font)["id"]])
        self.assertEqual(panel._collection_id, group["id"])
        self.assertFalse(panel.collection_editor.isVisible())
        self.assertTrue(self.ingest.ingest([IngestCandidate(self.png)]).duplicate_only)
        panel.set_batch_scope("all")
        panel._set_filter_mode("image")
        panel.search_input.setText(Path(self.bundle_for(self.png)["source_key"]).stem)
        panel.refresh()
        panel.item_labels[0].click()
        with patch.object(panel, "_toast_callback") as toast:
            self.choose_menu(panel.collection_member_button, 0)
            toast.assert_called_once_with("已加入组合", tone="success")
        self.assertEqual(set(read_collections(panel._collections_path())[0]["asset_ids"]),
                         {self.bundle_for(self.font)["id"], self.bundle_for(self.png)["id"]})
        panel.search_input.clear()
        panel._set_filter_mode("all")
        panel.collection_picker.setCurrentIndex(panel.collection_picker.findData(group["id"]))
        self.assertEqual(len(panel._all_recent_items), 2)
        self.assertFalse(self.card.isVisible())
        self.assertEqual(self.ball.quick_menu.size(), app_gui.QSize(620, 520))
        # Missing members can still be removed; the menu cannot add them elsewhere.
        image = self.bundle_for(self.png)
        (self.settings.ASSETS_DIR / image["source_key"]).unlink()
        panel.refresh()
        missing_index = next(i for i, item in enumerate(panel._visible_items) if item.source_key == image["source_key"])
        panel.item_labels[missing_index].click()
        self.assertTrue(panel.collection_member_button.isEnabled())
        with patch.object(panel, "_toast_callback") as toast:
            self.choose_menu(panel.collection_member_button, 0)
            toast.assert_called_once_with("已移出组合", tone="success")
        self.assertEqual(read_collections(panel._collections_path())[0]["asset_ids"], [self.bundle_for(self.font)["id"]])

    def test_collection_handoff_only_delivers_ready_members_and_preserves_clipboard_on_error(self):
        panel = self.ball.material_panel
        image, font, pending = (self.bundle_for(p) for p in (self.png, self.font, self.gif))
        path = panel._collections_path()
        group = save_collection(path, "My site", asset_id=image["id"])
        for bundle in (font, pending):
            set_collection_asset(path, group["id"], bundle["id"], included=True)
        self.service.set_bundle_role(image["id"], "hero_image")
        self.service.update_bundle_font(font["id"], font_usage="body")
        self.service.set_bundle_role(self.bundle_for(self.svg)["id"], "icon")
        payload = collection_handoff(path, group["id"], self.service, base_url="http://127.0.0.1:8010")
        self.assertEqual({b["id"] for b in payload["assets"]}, {image["id"], font["id"]})
        self.assertEqual(payload["collection"]["omitted_count"], 1)
        self.assertNotIn("batch_id", payload)
        self.assertTrue(payload["complete"])
        (self.settings.ASSETS_DIR / font["source_key"]).unlink()
        panel._collection_id = group["id"]
        panel._copy_collection()
        import json
        copied = json.loads(self.app.clipboard().text())
        self.assertEqual([b["id"] for b in copied["assets"]], [image["id"]])
        self.assertEqual(copied["collection"]["omitted_count"], 2)
        self.assertNotIn(str(self.settings.STORAGE_DIR), self.app.clipboard().text())
        previous = self.app.clipboard().text()
        mark_manifest_dirty(self.settings.MANIFEST_PATH)
        panel._copy_collection()
        self.assertEqual(self.app.clipboard().text(), previous)

    def test_collection_editor_cancel_duplicate_and_english_keep_management_size(self):
        panel = self.ball.material_panel
        self.ball._handle_quick_menu_action("assets")
        self.choose_menu(panel.new_collection_button, 0)
        QTest.keyClick(panel.collection_name, Qt.Key.Key_Escape)
        self.assertFalse(panel.collection_editor.isVisible())
        self.assertTrue(self.ball.quick_menu.isVisible())
        self.choose_menu(panel.new_collection_button, 0)
        panel.collection_name.setText("My group")
        panel.collection_save.click()
        original = panel._collections_path().read_bytes()
        self.choose_menu(panel.new_collection_button, 0)
        panel.collection_name.setText("My group")
        panel.collection_save.click()
        self.assertTrue(panel.collection_editor.isVisible())
        self.assertEqual(panel.collection_name.text(), "My group")
        self.assertEqual(panel._collections_path().read_bytes(), original)
        panel.collection_cancel.click()
        app_gui.set_ui_language("en")
        try:
            panel.retranslate()
            self.app.processEvents()
            self.assertEqual(self.ball.quick_menu.size(), app_gui.QSize(620, 520))
            for widget in (panel.collection_picker, panel.new_collection_button, panel.collection_copy):
                self.assertTrue(widget.isVisible())
                self.assertTrue(panel.rect().contains(widget.rect().translated(widget.mapTo(panel, app_gui.QPoint()))))
            self.assertEqual(panel.new_collection_button.text(), "Manage groups")
            for button in panel.filter_buttons.values():
                if button.isVisible():
                    self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(button.text()) + 8)
        finally:
            app_gui.set_ui_language("auto")


if __name__ == "__main__":
    unittest.main()
