from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import hashlib
from pathlib import Path
import tempfile
import time
import unittest
import wave
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import app_gui
from app.core.config import Settings
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

    def mouse(self, kind, x, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.LeftButton):
        event = QMouseEvent(kind, QPointF(x, 12), QPointF(x, 12), button, buttons, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(self.card.preview, event)

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


if __name__ == "__main__":
    unittest.main()
