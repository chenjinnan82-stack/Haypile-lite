from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

from fontTools.ttLib import TTFont
from mutagen._vorbis import VCommentDict
from PIL import Image
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import app_gui
from app.core.config import Settings
from app.services.ingest_service import IngestCandidate, IngestService
from app.services.scanner import AssetScanner
from types import SimpleNamespace
from tests.test_font_support import write_test_font


class FakePlayer(QObject):
    PlaybackState = QMediaPlayer.PlaybackState
    playbackStateChanged = Signal(object)
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    errorOccurred = Signal(object, str)

    def __init__(self, parent):
        super().__init__(parent)
        self.url, self.state, self.offset = QUrl(), self.PlaybackState.StoppedState, 0
        self.play_count = 0

    def setAudioOutput(self, output): self.output = output
    def source(self): return self.url
    def setSource(self, url): self.url = url
    def playbackState(self): return self.state
    def position(self): return self.offset
    def duration(self): return 1000
    def setPosition(self, value): self.offset = value
    def play(self):
        self.play_count += 1
        self.state = self.PlaybackState.PlayingState
        self.playbackStateChanged.emit(self.state)
    def pause(self):
        self.state = self.PlaybackState.PausedState
        self.playbackStateChanged.emit(self.state)
    def stop(self):
        self.state = self.PlaybackState.StoppedState
        self.offset = 0
        self.playbackStateChanged.emit(self.state)


class FakeOutput:
    def __init__(self, parent): pass
    def setVolume(self, value): self.volume = value


class AudioTagCompatibilityTests(unittest.TestCase):
    def test_absent_or_malformed_optional_coverage_does_not_break_the_interface(self):
        for coverage in (None, {}, "unknown", {"character_count": 12}, {"character_count": "12"}):
            self.assertEqual(app_gui._font_coverage_text({"coverage": coverage}), "")

    def test_empty_vorbis_tags_ignore_keys_from_other_audio_formats(self):
        tags = VCommentDict()
        self.assertEqual(AssetScanner._audio_tags(SimpleNamespace(tags=tags)), {})
        tags["title"] = ["Original title"]
        self.assertEqual(AssetScanner._audio_tags(SimpleNamespace(tags=tags)), {"title": "Original title"})


class MaterialToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        app_gui.set_ui_language("zh")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(_env_file=None, STORAGE_DIR=self.root / "storage", VISION_CLASSIFIER_ENABLED=False)
        self.png = self.root / "colors.png"
        with Image.new("RGB", (64, 48), "#FF0000") as image:
            image.paste("#0000FF", (48, 0, 64, 48))
            image.save(self.png)
        self.wav = self.root / "sample.wav"
        with wave.open(str(self.wav), "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 8000)
        fonts = []
        for index, (family, weight, style) in enumerate((("Haypile Acceptance", 400, "Regular"),
                                                        ("Haypile Acceptance", 700, "Bold"), ("Other Family", 400, "Regular"))):
            path = self.root / f"font{index}.ttf"
            write_test_font(path, width=300 + index * 100)
            with TTFont(path) as font:
                font["OS/2"].usWeightClass = weight
                for name_id, value in ((1, family), (16, family), (2, style), (17, style)):
                    font["name"].setName(value, name_id, 3, 1, 0x409)
                font.save(path)
            fonts.append(path)
        service = IngestService(storage_dir=self.settings.STORAGE_DIR, assets_dir=self.settings.ASSETS_DIR,
                                index_dir=self.settings.INDEX_DIR, themes_dir=self.settings.THEMES_DIR,
                                manifest_path=self.settings.MANIFEST_PATH)
        self.assertTrue(service.ingest([IngestCandidate(path) for path in [self.png, self.wav, *fonts]]).success)
        self.panel = app_gui.MaterialPanelWindow(settings=self.settings, embedded=True)
        self.panel._batch_scope = "all"
        self.panel.resize(620, 520)
        self.panel.show()
        self.panel.refresh()

    def tearDown(self):
        self.panel._font_preview_closing = True
        self.panel._cancel_font_preview()
        deadline = time.monotonic() + 3
        while self.panel.font_preview_worker is not None and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertIsNone(self.panel.font_preview_worker)
        self.panel.close()
        self.app.processEvents()
        self.temp.cleanup()
        app_gui.set_ui_language("auto")

    def select_type(self, kind):
        self.panel._set_filter_mode(kind)
        self.panel._select_recent_item(0)
        self.app.processEvents()

    def test_font_family_filter_orders_variants_and_keeps_comparison_text(self):
        self.select_type("font")
        picker = self.panel.font_family_picker
        self.assertTrue(picker.isVisible())
        self.assertEqual(picker.count(), 3)
        picker.setCurrentIndex(picker.findData("haypile acceptance"))
        self.assertEqual(len(self.panel._recent_items), 2)
        weights = [(self.panel._bundle_for_item(item)["font_metadata"])["weight"] for item in self.panel._recent_items]
        self.assertEqual(weights, [400, 700])
        self.panel.font_text_input.setText("ABC 冬")
        self.panel.font_size_input.setValue(40)
        for index in (0, 1):
            self.panel._select_recent_item(index)
            deadline = time.monotonic() + 3
            while self.panel.font_preview_worker is not None or self.panel._font_preview_timer.isActive():
                self.assertLess(time.monotonic(), deadline)
                QTest.qWait(5)
            self.assertEqual(self.panel.font_text_input.text(), "ABC 冬")
            self.assertEqual(self.panel.font_size_input.value(), 40)
            self.assertIn("冬", self.panel.font_missing_label.text())
            self.assertIn("汉字 0", self.panel.detail_label.text())
            self.assertIn("不代表完整字库", self.panel.detail_label.text())
        self.panel._set_filter_mode("image")
        self.assertFalse(picker.isVisible())

    def test_palette_copies_hex_and_changed_asset_preserves_clipboard(self):
        self.select_type("image")
        self.assertTrue(self.panel.palette_row.isVisible())
        button = self.panel.palette_buttons[0]
        self.assertEqual(button.text(), "#FF0000")
        button.click()
        self.assertEqual(self.app.clipboard().text(), "#FF0000")
        self.assertIn("#FF0000", button.accessibleName())
        source = self.panel._verified_selected_source("image")
        source.write_bytes(b"changed image")
        self.app.clipboard().setText("previous clipboard")
        button.click()
        self.assertEqual(self.app.clipboard().text(), "previous clipboard")
        self.assertFalse(self.panel.palette_row.isVisible())

    def test_small_palette_does_not_interpolate_nearly_transparent_rgb(self):
        self.select_type("image")
        pixels = bytes([255, 0, 0, 16] * 70 + [0, 255, 0, 255] * 30)
        image = QImage(pixels, 100, 1, 400, QImage.Format.Format_RGBA8888).copy()
        self.panel._show_image_palette(QPixmap.fromImage(image))
        self.assertEqual([button.text() for button in self.panel.palette_buttons if button.isVisible()], ["#00FF00"])

    def test_audio_is_manual_pause_resume_and_releases_source_on_hide(self):
        self.select_type("audio")
        self.assertIsNone(self.panel._audio_player)
        with patch.object(app_gui, "QMediaPlayer", FakePlayer), patch.object(app_gui, "QAudioOutput", FakeOutput):
            self.panel.audio_play_button.click()
            player = self.panel._audio_player
            self.assertEqual(player.source().scheme(), "file")
            self.assertEqual(self.panel.audio_play_button.text(), "暂停")
            self.panel.audio_play_button.click()
            self.assertEqual(self.panel.audio_play_button.text(), "继续")
            self.panel.audio_play_button.click()
            self.assertEqual(player.play_count, 2)
            self.panel.hide()
            self.assertTrue(player.source().isEmpty())
            self.assertEqual(player.state, FakePlayer.PlaybackState.StoppedState)

    def test_audio_error_switch_and_integrity_failure_never_autoplay(self):
        self.select_type("audio")
        with patch.object(app_gui, "QMediaPlayer", FakePlayer), patch.object(app_gui, "QAudioOutput", FakeOutput):
            self.panel.audio_play_button.click()
            player = self.panel._audio_player
            player.errorOccurred.emit(None, "decoder error")
            self.assertIn("无法试听", self.panel.audio_time_label.text())
            self.assertTrue(player.source().isEmpty())
            self.panel.audio_play_button.click()
            self.assertEqual(player.play_count, 2)
            source = self.panel._verified_selected_source("audio")
            self.panel.audio_stop_button.click()
            source.write_bytes(b"changed audio")
            self.panel.audio_play_button.click()
            self.assertEqual(player.play_count, 2)
            self.assertTrue(player.source().isEmpty())
            self.panel._set_filter_mode("image")
            self.panel._select_recent_item(0)
            self.assertFalse(self.panel.audio_controls.isVisible())

    def test_immediate_decoder_error_cannot_be_overwritten_by_play_or_late_updates(self):
        class FailingPlayer(FakePlayer):
            def setSource(self, url):
                super().setSource(url)
                if not url.isEmpty():
                    self.errorOccurred.emit(None, "immediate decoder failure")
        self.select_type("audio")
        with patch.object(app_gui, "QMediaPlayer", FailingPlayer), patch.object(app_gui, "QAudioOutput", FakeOutput):
            self.panel.audio_play_button.click()
            player = self.panel._audio_player
            self.assertEqual(player.play_count, 0)
            self.assertTrue(player.source().isEmpty())
            player.durationChanged.emit(1000)
            player.positionChanged.emit(500)
            self.assertIn("无法试听", self.panel.audio_time_label.text())
            self.assertEqual(self.panel.audio_play_button.text(), "试听")


if __name__ == "__main__":
    unittest.main()
