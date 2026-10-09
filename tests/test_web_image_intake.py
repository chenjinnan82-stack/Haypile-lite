from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import hashlib
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

import app_gui
from app.core.config import Settings
from app.services.asset_provenance import read_asset_provenance
from app.services.safe_remote_fetcher import SafeFetchError, validate_remote_url
from app.services.web_image_service import MAX_WEB_IMAGES, download_web_images, srcset_urls
from tests.test_font_support import write_test_font
from tests.test_web_font_intake import Response


class WebImageIntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.incoming = self.root / "incoming"
        self.page = "https://public.example/site/?token=page-secret"
        self.resources = {}
        self.calls = []
        self.responses = []
        self.originals = {}
        for name, color, size in [("cover.png", "orange", (160, 90)), ("photo.png", "blue", (80, 120))]:
            with Image.new("RGB", size, color) as image, BytesIO() as output:
                image.save(output, "PNG")
                self.originals[name] = output.getvalue()
            self.resources["https://public.example/assets/" + name] = ("image/png", self.originals[name])
        self.resources["https://public.example/assets/icon.svg"] = ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><circle cx="12" cy="12" r="10" fill="#4e5f3d"/></svg>')

    def tearDown(self):
        self.app.processEvents()
        self.temp.cleanup()

    def open(self, url, **_kwargs):
        validate_remote_url(url, resolver=lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
        self.calls.append(url)
        if url not in self.resources:
            raise SafeFetchError("not_found")
        response = Response(*self.resources[url])
        self.responses.append(response)
        return response

    def page_html(self, html):
        self.resources[self.page] = ("text/html; charset=utf-8", html.encode())

    def discover(self, **kwargs):
        return download_web_images(self.page, self.incoming, opener=self.open, **kwargs)

    def worker(self):
        worker = app_gui.RemoteDownloadWorker([self.page], self.incoming)
        results = []
        worker.finished_signal.connect(lambda *args: results.append(args))
        with patch.object(app_gui, "open_safe_remote", self.open):
            worker.run()
        return worker, results[0]

    def test_cover_responsive_lazy_images_icons_base_and_original_deduplication(self):
        self.page_html('''<base target="_blank"><img alt="Sea &amp; sky" src="missing.png" srcset="small.png 320w, photo?w=1600,fit=max 1600w">
          <base href="/assets/"><link rel="icon" href="icon.svg">
          <meta property="og:image" content="cover.png?signature=secret#preview">
          <meta name="twitter:image" content="cover-copy.png">
          <img src="cover.png"><img data-src="photo.png" src="data:image/png;base64,AAA">
          <img width="1" height="1" src="tracker.png">
          <template><img src="template.png"></template><script>"<img src='script.png'>"</script>''')
        self.resources["https://public.example/assets/cover.png?signature=secret"] = self.resources["https://public.example/assets/cover.png"]
        self.resources["https://public.example/assets/cover-copy.png"] = self.resources["https://public.example/assets/cover.png"]
        self.resources["https://public.example/assets/photo?w=1600,fit=max"] = self.resources["https://public.example/assets/photo.png"]
        candidates, limited, failed = self.discover()
        self.assertEqual((len(candidates), limited, failed), (3, False, 0))
        self.assertEqual([c.hint for c in candidates], ["cover", "image", "icon"])
        self.assertIn("https://public.example/assets/photo?w=1600,fit=max", self.calls)
        self.assertFalse(any(name in url for url in self.calls for name in ("missing", "small", "tracker", "template", "script")))
        self.assertEqual(candidates[1].label, "Sea & sky")
        for c in candidates:
            self.assertEqual(c.sha256, hashlib.sha256(c.path.read_bytes()).hexdigest())
            self.assertEqual(read_asset_provenance(c.path)["origin_url"], "https://public.example")
        self.assertEqual(candidates[0].path.read_bytes(), self.originals["cover.png"])
        self.assertTrue(all(response.closed for response in self.responses))
        self.assertEqual(len(list(self.incoming.iterdir())), 6)
        self.assertEqual(srcset_urls("a.png 1x, b.png 2x"), ["b.png", "a.png"])
        self.assertEqual(srcset_urls("a.png 0w, b.png 1e999x, c.png nonsense"), [])
        self.assertEqual(srcset_urls("x" * 16385), [])

    def test_private_unsupported_corrupt_and_unsafe_images_fall_back_safely(self):
        self.page_html('''<base href="/assets/">
          <meta property="og:image" content="http://127.0.0.1/private.png">
          <picture><source srcset="audio.png 2x"><img srcset="bad.svg 2x, broken.png 1x" src="photo.png"></picture>
          <img src="audio.png"><img src="javascript:alert(1)">
          <link rel="icon" href="icon.svg">''')
        self.resources["https://public.example/assets/bad.svg"] = ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://private.example/secret.png"/></svg>')
        self.resources["https://public.example/assets/broken.png"] = ("image/png", b"not an image")
        self.resources["https://public.example/assets/audio.png"] = ("audio/wav", b"must not be written")
        candidates, limited, failed = self.discover()
        self.assertEqual((len(candidates), limited, failed), (2, False, 4))
        self.assertFalse(any("127.0.0.1" in url for url in self.calls))
        self.assertEqual([c.path.name for c in candidates], ["photo.png", "icon.svg"])
        self.assertEqual(len(list(self.incoming.iterdir())), 4)
        self.assertTrue(all(response.closed for response in self.responses))

    def test_count_bytes_attempt_limits_and_cancellation_clean_unconfirmed_files(self):
        for mode in ("count", "bytes", "attempts", "cancel"):
            with self.subTest(mode=mode):
                for path in self.incoming.glob("*"):
                    path.unlink()
                self.calls.clear()
                self.page_html('<base href="/assets/"><meta property="og:image" content="cover.png"><img src="photo.png"><link rel="icon" href="icon.svg">')
                if mode == "count":
                    candidates, limited, _ = self.discover(max_images=1)
                    self.assertEqual(len(candidates), 1)
                    self.assertTrue(limited)
                elif mode == "bytes":
                    candidates, _, _ = self.discover(max_bytes=1)
                    self.assertFalse(candidates)
                    self.assertFalse(list(self.incoming.iterdir()))
                elif mode == "attempts":
                    self.page_html("".join(f'<img src="https://public.example/missing-{i}.png">' for i in range(100)))
                    candidates, limited, _ = self.discover()
                    self.assertFalse(candidates)
                    self.assertTrue(limited)
                    self.assertEqual(len(self.calls) - 1, MAX_WEB_IMAGES * 2)
                else:
                    candidates, _, _ = self.discover(should_stop=lambda: bool(list(self.incoming.glob("*.provenance.json"))))
                    self.assertFalse(candidates)
                    self.assertFalse(list(self.incoming.iterdir()))

    def test_mixed_card_switches_in_place_and_stores_only_current_original(self):
        font = self.root / "font.ttf"
        write_test_font(font)
        self.resources["https://public.example/assets/font.ttf"] = ("font/ttf", font.read_bytes())
        self.page_html('''<base href="/assets/"><style>@font-face{src:url(font.ttf)}</style>
            <meta property="og:image" content="cover.png"><img src="photo.png"><link rel="icon" href="icon.svg">''')
        worker, result = self.worker()
        self.assertTrue(result[2])
        self.assertEqual((len(worker.font_candidates), len(worker.image_candidates)), (1, 3))
        self.assertEqual(self.calls.count(self.page), 2)  # MIME probe + one shared HTML snapshot.
        self.assertTrue(all(c.pixels and c.width <= 508 and c.height <= 200 for c in worker.image_candidates))
        settings = Settings(_env_file=None, STORAGE_DIR=self.root / "isolated", VISION_CLASSIFIER_ENABLED=False)
        ball = app_gui.HaypileFloatingBall(settings=settings)
        try:
            ball.remote_worker = worker
            ball._on_remote_download_finished(result[0], result[1], True, [])
            card = ball.intake_card
            self.assertEqual(card.mode, "fonts")
            self.assertTrue(card.kind_button.isVisible())
            self.assertFalse(any(path.is_file() for path in settings.ASSETS_DIR.rglob("*")))
            card.kind_button.click()
            self.assertEqual(card.mode, "images")
            self.assertEqual(card.size().width(), 280)
            self.assertEqual(card.height(), 240)
            self.assertFalse(card.preview.pixmap().isNull())
            self.assertEqual(card.store_button.text(), "收纳这张")
            self.assertEqual(card.title.accessibleName(), "网页封面")
            self.assertEqual(card.next_button.accessibleName(), "下一张")
            self.assertEqual(card.close_button.accessibleName(), "关闭")
            with patch.object(card.preview, "setPixmap", wraps=card.preview.setPixmap) as set_pixmap:
                card._show_candidate()
            # Inspect the input to Qt; QLabel's getter can report offscreen density.
            pixmap = set_pixmap.call_args.args[0]
            self.assertEqual(pixmap.devicePixelRatio(), 2)
            self.assertLessEqual(pixmap.deviceIndependentSize().width(), 254)
            self.assertLessEqual(pixmap.deviceIndependentSize().height(), 100)
            card.info_button.setChecked(True)
            self.assertEqual(card.height(), 320)
            self.assertFalse(card.usage_row.isVisible())
            self.assertIn("image/png", card.details.toPlainText())
            self.assertNotIn("page-secret", card.details.toPlainText())
            card.next_button.click()
            selected = card.candidates[card.index]
            card.kind_button.click()
            self.assertEqual(card.mode, "fonts")
            card.kind_button.click()
            self.assertEqual(card.index, 1)
            self.assertIs(card.candidates[card.index], selected)
            unselected = [c.path for c in ball._web_candidates if c != selected]
            card.store_button.click()
            self.assertFalse(ball._web_candidates)
            self.assertTrue(all(not path.exists() for path in unselected))
            deadline = time.monotonic() + 3
            while ball.worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.005)
            self.assertIsNone(ball.worker)
            bundles = ball._bundle_service().list_bundles()
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            self.assertEqual(bundle["type"], "image")
            self.assertEqual(bundle["origin_url"], "https://public.example")
            stored = settings.ASSETS_DIR / bundle["source_key"]
            self.assertEqual(stored.read_bytes(), self.originals["photo.png"])
            self.assertEqual(bundle["sha256"], selected.sha256)
            self.assertFalse(list(self.incoming.iterdir()))
        finally:
            ball.close()
            self.app.processEvents()

    def test_image_only_card_outside_click_delivers_action_and_cleans_all_candidates(self):
        self.page_html('<base href="/assets/"><meta property="og:image" content="cover.png"><img src="photo.png">')
        worker, result = self.worker()
        ball = app_gui.HaypileFloatingBall(settings=Settings(_env_file=None, STORAGE_DIR=self.root / "isolated", VISION_CLASSIFIER_ENABLED=False))
        outside = QPushButton("Original page action")
        clicks = []
        outside.clicked.connect(lambda: clicks.append(True))
        outside.show()
        try:
            ball.remote_worker = worker
            ball._on_remote_download_finished(result[0], result[1], True, [])
            card = ball.intake_card
            self.assertEqual(card.mode, "images")
            self.assertFalse(card.kind_button.isVisible())
            self.assertFalse(card.isModal())
            card.info_button.setChecked(True)
            QTest.mouseClick(outside, Qt.MouseButton.LeftButton)
            self.assertEqual(clicks, [True])
            self.assertFalse(card.isVisible())
            self.assertFalse(list(self.incoming.iterdir()))
            self.assertFalse(any(path.is_file() for path in ball.settings.ASSETS_DIR.rglob("*")))
        finally:
            outside.close()
            ball.close()
            self.app.processEvents()

    def test_compact_card_keeps_long_names_accessible_and_icon_actions_keyboard_operable(self):
        self.page_html('<base href="/assets/"><meta property="og:image" content="cover.png"><img src="photo.png">')
        worker, _ = self.worker()
        name = '<一张很长的网页图片说明> & Mountains and sea ' * 4
        images = [replace(worker.image_candidates[0], label=name), worker.image_candidates[1]]
        ball = app_gui.HaypileFloatingBall(settings=Settings(_env_file=None, STORAGE_DIR=self.root / "isolated", VISION_CLASSIFIER_ENABLED=False))
        try:
            card = ball.intake_card
            for language, close, next_image in [("zh", "关闭", "下一张"), ("en", "Close", "Next image")]:
                with self.subTest(language=language):
                    app_gui.set_ui_language(language)
                    card.show_candidates([], images)
                    self.app.processEvents()
                    self.assertEqual(card.title.accessibleName(), name)
                    self.assertEqual(card.close_button.accessibleName(), close)
                    self.assertEqual(card.next_button.accessibleName(), next_image)
                    QTest.keyClick(card.info_button, Qt.Key.Key_Space)
                    self.app.processEvents()
                    self.assertTrue(card.information.isVisible())
                    self.assertIn(name, card.details.toPlainText())
                    self.assertEqual(card.details.horizontalScrollBar().maximum(), 0)
                    for widget in (card.title, card.close_button, card.preview, card.status, card.previous_button,
                                   card.next_button, card.info_button, card.details, card.store_button):
                        rect = widget.rect().translated(widget.mapTo(card, QPoint()))
                        self.assertTrue(card.rect().contains(rect), widget.objectName())
                    QTest.keyClick(card.next_button, Qt.Key.Key_Return)
                    self.assertEqual(card.index, 1)
                    QTest.keyClick(card.close_button, Qt.Key.Key_Space)
                    self.assertFalse(card.isVisible())
            self.assertTrue(all(c.path.is_file() for c in images))  # Rendering never owns these files.
        finally:
            app_gui.set_ui_language("auto")
            ball.close()
            self.app.processEvents()

    def test_preview_failure_cleans_every_owned_file_and_direct_images_keep_old_route(self):
        self.page_html('<base href="/assets/"><meta property="og:image" content="cover.png"><img src="photo.png">')
        with patch.object(app_gui.RemoteDownloadWorker, "_image_preview", side_effect=RuntimeError("test failure")):
            worker, result = self.worker()
        self.assertFalse(result[2])
        self.assertFalse(worker.image_candidates)
        self.assertFalse(list(self.incoming.iterdir()))
        worker = app_gui.RemoteDownloadWorker(["https://public.example/assets/cover.png"], self.incoming)
        results = []
        worker.finished_signal.connect(lambda *args: results.append(args))
        with patch.object(app_gui, "open_safe_remote", self.open):
            worker.run()
        self.assertTrue(results[0][2])
        self.assertFalse(worker.image_candidates)
        self.assertEqual(results[0][0][0].read_bytes(), self.originals["cover.png"])


if __name__ == "__main__":
    unittest.main()
