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
from PySide6.QtCore import QEvent, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import app_gui
from app.core.config import Settings
from app.core.file_lock import InterProcessFileLock
from app.services.asset_collections import read_collections, save_collection, set_collection_asset, collection_handoff
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

    def test_collection_gui_creates_from_selected_asset_and_adds_later_intake(self):
        panel = self.ball.material_panel
        self.ball._handle_quick_menu_action("assets")
        panel.set_batch_scope("all")
        panel._set_filter_mode("font")
        panel.item_labels[0].click()
        def choose_menu(index):
            menu = self.app.activePopupWidget()
            menu.actions()[index].trigger()
            menu.close()
        QTimer.singleShot(0, lambda: choose_menu(-1))
        panel.collection_member_button.click()
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
        QTimer.singleShot(0, lambda: choose_menu(0))
        panel.collection_member_button.click()
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
        panel._show_detail_for_bundle(self.service.get_bundle(image["id"]))
        panel._selected_bundle_id = image["id"]
        self.assertTrue(panel.collection_member_button.isEnabled())
        QTimer.singleShot(0, lambda: choose_menu(0))
        panel.collection_member_button.click()
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
        panel.new_collection_button.click()
        QTest.keyClick(panel.collection_name, Qt.Key.Key_Escape)
        self.assertFalse(panel.collection_editor.isVisible())
        self.assertTrue(self.ball.quick_menu.isVisible())
        panel.new_collection_button.click()
        panel.collection_name.setText("My group")
        panel.collection_save.click()
        original = panel._collections_path().read_bytes()
        panel.new_collection_button.click()
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
            self.assertEqual(panel.new_collection_button.text(), "New group")
            for button in panel.filter_buttons.values():
                if button.isVisible():
                    self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(button.text()) + 8)
        finally:
            app_gui.set_ui_language("auto")


if __name__ == "__main__":
    unittest.main()
