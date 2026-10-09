from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fontTools.ttLib import TTFont
from PySide6.QtCore import QMimeData, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

import app_gui
from app.services.asset_provenance import read_asset_provenance
from app.services.safe_remote_fetcher import SafeFetchError, validate_remote_url
from app.services.web_font_service import MAX_PAGE_BYTES, css_font_references, download_web_fonts
from tests.test_font_support import write_test_font


class Response:
    def __init__(self, content_type: str, body: bytes):
        self.headers = {"content-type": content_type, "content-length": str(len(body))}
        self.body = body
        self.closed = False

    def __enter__(self): return self
    def __exit__(self, *_args): self.closed = True
    def iter_bytes(self):
        for start in range(0, len(self.body), 4096):
            yield self.body[start:start + 4096]


class WebFontIntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = self.root / "original.ttf"
        write_test_font(self.original)
        self.woff = self.root / "font.woff2"
        with TTFont(self.original) as font:
            font.flavor = "woff2"
            font.save(self.woff)
        self.resources = {}
        self.calls = []
        self.responses = []
        self.page = "https://public.example/site/"
        self.incoming = self.root / "incoming"

    def tearDown(self):
        self.app.processEvents()
        self.temp.cleanup()

    def open(self, url, **_kwargs):
        validate_remote_url(url, resolver=lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
        self.calls.append(url)
        content_type, body = self.resources[url]
        response = Response(content_type, body)
        self.responses.append(response)
        return response

    def add_page(self, html):
        self.resources[self.page] = ("text/html; charset=utf-8", html.encode())

    def discover(self, **kwargs):
        return download_web_fonts(self.page, self.incoming, opener=self.open, **kwargs)

    def test_relative_links_imports_css_escapes_fallbacks_and_byte_deduplication(self):
        for malformed in ("/*" + "x" * MAX_PAGE_BYTES, '"' + '\\"' * (MAX_PAGE_BYTES // 2), "@font-face{src:url(" + "\\abcdef" * 200000):
            started = time.monotonic()
            self.assertEqual(css_font_references(malformed, self.page), ([], []))
            self.assertLess(time.monotonic() - started, 2)
        self.add_page('<base target="_blank"><base href="/assets/"><link rel="stylesheet" href="main.css"><a href="copy.woff2">Font</a>')
        self.resources["https://public.example/assets/main.css"] = ("text/css", b'''@import "nested.css";
          /* @font-face{src:url(fake.woff2)} */ .x{content:"@font-face {url(fake2.woff2)}"}
          @media screen { @font-face { font-family: "Test"; font-weight: 400;
           src: local("System"), url(bad.woff2) format('woff2'), url(font\\2e woff2) format('woff2'), url(alternate.woff); }}''')
        self.resources["https://public.example/assets/nested.css"] = ("text/css", b'@import "main.css"; @font-face{src:url(font.woff2)}')
        self.resources["https://public.example/assets/bad.woff2"] = ("font/woff2", b"not a font")
        for name in ("font.woff2", "copy.woff2"):
            self.resources["https://public.example/assets/" + name] = ("font/woff2", self.woff.read_bytes())
        candidates, limited, failed = self.discover()
        self.assertEqual((len(candidates), limited, failed), (1, False, 1))
        self.assertEqual(candidates[0].path.read_bytes(), self.woff.read_bytes())
        self.assertEqual(candidates[0].missing, "冬藏")
        self.assertEqual(candidates[0].width, 508)  # 2× pixels for a 254-point sample.
        self.assertEqual(read_asset_provenance(candidates[0].path)["origin_url"], "https://public.example")
        self.assertEqual(self.calls.count("https://public.example/assets/main.css"), 1)
        self.assertFalse(any("fake" in url or "alternate" in url for url in self.calls))
        self.assertTrue(all(response.closed for response in self.responses))
        self.assertEqual(len(list(self.incoming.glob("*.woff2"))), 1)

    def test_extensionless_font_response_and_unavailable_resources_do_not_ingest_images(self):
        self.add_page('''<link rel="stylesheet" href="http://127.0.0.1/private.css">
          <style>@font-face {font-family: Test;src:url(image.png),url(download) format("woff2")}</style>''')
        self.resources[self.page + "image.png"] = ("image/png", b"pixels must not be downloaded")
        self.resources[self.page + "download"] = ("application/octet-stream", self.woff.read_bytes())
        candidates, limited, failed = self.discover()
        self.assertEqual((len(candidates), limited, failed), (1, False, 2))
        self.assertEqual(candidates[0].path.suffix, ".woff2")
        self.assertFalse(any("127.0.0.1" in url for url in self.calls))
        self.assertFalse(list(self.incoming.glob("*.png")))
        self.assertEqual(candidates[0].path.read_bytes(), self.woff.read_bytes())

    def test_inert_templates_do_not_change_font_sources_or_the_page_base(self):
        self.add_page('''<template><base href="https://wrong.example/">
          <link rel="stylesheet" href="unused.css"><style>@font-face{src:url(unused.woff2)}</style>
          <template><a href="nested.ttf">Unused</a></template></template>
          <base href="/assets/"><style>@font-face{src:url(font.woff2)}</style>''')
        self.resources["https://public.example/assets/font.woff2"] = ("font/woff2", self.woff.read_bytes())
        candidates, limited, failed = self.discover()
        self.assertEqual((len(candidates), limited, failed), (1, False, 0))
        self.assertEqual(self.calls, [self.page, "https://public.example/assets/font.woff2"])
        self.assertEqual(candidates[0].path.read_bytes(), self.woff.read_bytes())

    def test_page_stylesheet_font_budget_and_cancellation_cleanup(self):
        for limits in ({"max_fonts": 0}, {"max_bytes": 0}):
            self.calls.clear()
            candidates, limited, failed = self.discover(**limits)
            self.assertEqual((candidates, limited, failed), ([], True, 0))
            self.assertFalse(self.calls)
        self.resources[self.page] = ("text/html", b"x" * (MAX_PAGE_BYTES + 1))
        with self.assertRaises(SafeFetchError): self.discover()
        self.add_page("".join(f'<link rel="stylesheet" href="{index}.css">' for index in range(10)))
        for index in range(10):
            self.resources[self.page + f"{index}.css"] = ("text/css", b'@font-face{src:url(font.woff2)}')
        self.resources[self.page + "font.woff2"] = ("font/woff2", self.woff.read_bytes())
        candidates, limited, failed = self.discover()
        self.assertEqual((len(candidates), limited, failed), (1, True, 0))
        self.assertEqual(sum(url.endswith(".css") for url in self.calls), 8)
        for path in self.incoming.iterdir(): path.unlink()
        candidates, limited, _failed = self.discover(max_bytes=1)
        self.assertFalse(candidates)
        self.assertFalse(list(self.incoming.iterdir()))
        self.add_page('<style>@font-face{src:url(font.woff2)} @font-face{src:url(other.woff2)}</style>')
        candidates, _limited, _failed = self.discover(should_stop=lambda: bool(list(self.incoming.glob("*.provenance.json"))))
        self.assertFalse(candidates)
        self.assertFalse(list(self.incoming.iterdir()))

    def test_picker_cancel_and_selection_clean_temps_before_ingest(self):
        from app.core.config import Settings
        self.add_page('<style>@font-face{src:url(font.woff2)} @font-face{src:url(other.woff2)}</style>')
        self.resources[self.page + "font.woff2"] = ("font/woff2", self.woff.read_bytes())
        other = self.root / "other.ttf"
        write_test_font(other, width=300)
        with TTFont(other) as font:
            font.flavor = "woff2"
            font.save(self.root / "other.woff2")
        self.resources[self.page + "other.woff2"] = ("font/woff2", (self.root / "other.woff2").read_bytes())
        settings = Settings(STORAGE_DIR=self.root / "storage", VISION_CLASSIFIER_ENABLED=False)
        with patch.object(app_gui, "get_settings", return_value=settings):
            ball = app_gui.HaypileFloatingBall()
        try:
            for accept in (False, True):
                candidates, _limited, _failed = self.discover()
                worker = app_gui.RemoteDownloadWorker([], self.incoming)
                worker.font_candidates = candidates
                ball.remote_worker = worker
                received = []
                ball._start_worker = lambda files: received.append(files)
                paths = [candidate.path for candidate in candidates]
                ball._on_remote_download_finished(paths, "ok", True, [])
                self.assertTrue(ball._ingest_busy())
                self.assertEqual(received, [])
                self.assertTrue(all(path.exists() for path in paths))
                self.assertTrue(ball.intake_card.isVisible())
                self.assertFalse(ball.intake_card.isModal())
                if accept:
                    ball.intake_card.store_button.click()
                else:
                    ball.intake_card.dismiss("close")
                self.assertEqual(received, [[paths[0]]] if accept else [])
                self.assertFalse(paths[1].exists())
                self.assertEqual(ball._remote_ingest_paths, {paths[0]} if accept else set())
                self.assertEqual(ball._web_candidates, [])
                ball._cleanup_remote_ingest_paths()
                self.assertFalse(list(self.incoming.iterdir()))
        finally:
            ball.close()
            self.app.processEvents()

    def test_worker_shutdown_retains_owner_until_thread_stops_and_cleans_temps(self):
        from app.core.config import Settings
        self.add_page('<style>@font-face{src:url(font.woff2)}</style>')
        self.resources[self.page + "font.woff2"] = ("font/woff2", self.woff.read_bytes())
        settings = Settings(STORAGE_DIR=self.root / "storage", VISION_CLASSIFIER_ENABLED=False)
        with patch.object(app_gui, "get_settings", return_value=settings):
            ball = app_gui.HaypileFloatingBall()
        stopped = []
        try:
            worker = app_gui.RemoteDownloadWorker([self.page], self.incoming)
            def slow_open(url, **kwargs):
                time.sleep(.05)
                return self.open(url, **kwargs)
            worker.finished.connect(lambda: stopped.append(True))
            ball.remote_worker = worker
            with patch.object(app_gui, "open_safe_remote", slow_open):
                worker.start()
                ball.shutdown()
                deadline = time.monotonic() + 3
                while not ball._cleanup_done and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.005)
            self.assertTrue(stopped)
            self.assertTrue(ball._cleanup_done)
            self.assertIsNone(ball.remote_worker)
            self.assertFalse(list(self.incoming.glob("*")))
        finally:
            ball.close()
            self.app.processEvents()

    def test_worker_page_selection_is_optional_and_html_anchor_drag_is_recognized(self):
        self.add_page('<style>@font-face{src:url(font.woff2)}</style>')
        self.resources[self.page + "font.woff2"] = ("font/woff2", self.woff.read_bytes())
        worker = app_gui.RemoteDownloadWorker([self.page], self.incoming)
        results = []
        worker.finished_signal.connect(lambda *args: results.append(args))
        with patch.object(app_gui, "open_safe_remote", self.open): worker.run()
        self.assertTrue(results[0][2])
        self.assertEqual(len(worker.font_candidates), 1)
        from app.core.config import Settings
        settings = Settings(STORAGE_DIR=self.root / "storage", VISION_CLASSIFIER_ENABLED=False)
        with patch.object(app_gui, "get_settings", return_value=settings):
            ball = app_gui.HaypileFloatingBall()
        try:
            ball.remote_worker = worker
            received = []
            ball._start_worker = lambda files: received.append(files)
            ball._on_remote_download_finished(results[0][0], "ok", True, [])
            card = ball.intake_card
            self.assertTrue(card.store_button.isEnabled())
            self.assertFalse(received)
            self.assertIn("冬藏", card.status.text())
            self.assertFalse(card.preview.pixmap().isNull())
            card.info_button.setChecked(True)
            self.assertLessEqual(card.height(), 320)
            self.assertIn('https://public.example', card.details.toPlainText())
            card.dismiss("escape")
            self.assertFalse(card.isVisible())
            self.assertFalse(received)
            self.assertFalse(list(self.incoming.iterdir()))
        finally:
            ball.close()
            self.app.processEvents()
        mime = QMimeData()
        mime.setHtml('<a href="https://public.example/site/">Personal site</a>')
        self.assertEqual(app_gui.HaypileFloatingBall._extract_remote_media_urls(mime), [self.page])
        mime.setHtml('<a href="https://public.example/site/"><img src="https://public.example/image.png"></a>')
        self.assertEqual(app_gui.HaypileFloatingBall._extract_remote_media_urls(mime), ["https://public.example/image.png"])
        mime.setHtml('<a href="https://[broken">Malformed link</a>')
        self.assertEqual(app_gui.HaypileFloatingBall._extract_remote_media_urls(mime), [])
        mime.setText(self.page + "#heading")
        self.assertEqual(app_gui.HaypileFloatingBall._extract_remote_media_urls(mime), [self.page])
        broken = app_gui.RemoteDownloadWorker([], self.incoming)
        errors = []
        broken.finished_signal.connect(lambda *args: errors.append(args))
        leftover = self.incoming / "unexpected.woff2"
        def fail(downloaded):
            leftover.write_bytes(self.woff.read_bytes())
            downloaded.append(leftover)
            broken.font_candidates = list(worker.font_candidates)
            raise RuntimeError("test interruption")
        with patch.object(broken, "_run_downloads", fail): broken.run()
        self.assertFalse(errors[0][2])
        self.assertFalse(leftover.exists())
        self.assertFalse(broken.font_candidates)
        self.assertTrue(self.original.exists())

    def test_outside_click_delivers_action_and_discards_unconfirmed_font(self):
        from app.core.config import Settings
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        self.add_page('<style>@font-face{src:url(font.woff2)}</style>')
        self.resources[self.page + "font.woff2"] = ("font/woff2", self.woff.read_bytes())
        candidates, _, _ = self.discover()
        settings = Settings(STORAGE_DIR=self.root / "storage", VISION_CLASSIFIER_ENABLED=False)
        with patch.object(app_gui, "get_settings", return_value=settings):
            ball = app_gui.HaypileFloatingBall()
        outside = QPushButton("Original page action")
        clicked = []
        outside.clicked.connect(lambda: clicked.append(True))
        outside.show()
        try:
            ball._web_candidates = candidates
            ball.intake_card.show_candidates(candidates)
            self.assertTrue(ball.intake_card.isVisible())
            QTest.mouseClick(outside, Qt.MouseButton.LeftButton)
            self.assertEqual(clicked, [True])
            self.assertFalse(ball.intake_card.isVisible())
            self.assertFalse(ball._web_candidates)
            self.assertFalse(list(self.incoming.iterdir()))
        finally:
            outside.close()
            ball.close()
            self.app.processEvents()

    def test_counter_detects_completed_click_and_escape_cancels_loading(self):
        from app.core.config import Settings
        from PySide6.QtCore import QPoint
        settings = Settings(STORAGE_DIR=self.root / "storage", VISION_CLASSIFIER_ENABLED=False)
        with patch.object(app_gui, "get_settings", return_value=settings):
            ball = app_gui.HaypileFloatingBall()
        try:
            card = ball.intake_card
            card.show_message("Loading", "progress")
            card._last_buttons = (1, 0, 0)
            card._desktop_buttons = lambda: (2, 0, 0)
            with patch.object(app_gui.QCursor, "pos", return_value=QPoint(-100, -100)):
                card._poll_outside()
            self.assertFalse(card.isVisible())
            self.assertFalse(ball._remote_request_cancelled)
            interrupted = []
            class WaitingWorker(app_gui.RemoteDownloadWorker):
                def requestInterruption(self): interrupted.append(True)
            ball.remote_worker = WaitingWorker([], self.incoming)
            card.show_message("Loading", "progress")
            card._last_escape = False
            card._desktop_escape = lambda: True
            card._poll_outside()
            self.assertEqual(interrupted, [True])
            self.assertTrue(ball._remote_request_cancelled)
            self.assertFalse(card.isVisible())
            ball._on_remote_download_finished([], "late result", True, [])
            self.assertFalse(card.isVisible())
        finally:
            ball.close()
            self.app.processEvents()

    def test_late_actions_on_dismissed_card_do_not_reopen_windows(self):
        from app.core.config import Settings
        settings = Settings(_env_file=None, STORAGE_DIR=self.root / "storage", VISION_CLASSIFIER_ENABLED=False)
        ball = app_gui.HaypileFloatingBall(settings=settings)
        try:
            card = ball.intake_card
            card.show_recent()
            card.dismiss("outside")
            with patch.object(ball, "_handle_quick_menu_action") as open_manager:
                card.store_button.click()
                card.info_button.click()
                open_manager.assert_not_called()
            self.assertFalse(card.isVisible())
            self.assertFalse(ball.quick_menu.isVisible())
        finally:
            ball.close()
            self.app.processEvents()

    def test_recent_font_usage_survives_preview_and_failed_update(self):
        from app.core.config import Settings
        from app.services.ingest_service import IngestCandidate, IngestService
        from PySide6.QtTest import QTest
        settings = Settings(STORAGE_DIR=self.root / "storage", VISION_CLASSIFIER_ENABLED=False)
        service = IngestService(storage_dir=settings.STORAGE_DIR, assets_dir=settings.ASSETS_DIR,
                                index_dir=settings.INDEX_DIR, themes_dir=settings.THEMES_DIR,
                                manifest_path=settings.MANIFEST_PATH, fallback_theme="generic")
        self.assertTrue(service.ingest([IngestCandidate(self.original)]).success)
        with patch.object(app_gui, "get_settings", return_value=settings):
            ball = app_gui.HaypileFloatingBall()
        try:
            card = ball.intake_card
            bundle = ball._bundle_service().list_bundles()[0]
            card.show_bundle(bundle)
            deadline = time.monotonic() + 2
            while card.font_preview_worker is not None and time.monotonic() < deadline:
                QTest.qWait(5)
            self.assertIsNone(card.font_preview_worker)
            self.assertFalse(card.preview.pixmap().isNull())
            card.info_button.setChecked(True)
            card.usage_buttons["body"].setChecked(True)
            self.assertEqual(ball._bundle_service().get_bundle(bundle["id"])["font_usage"], "body")
            with patch.object(app_gui.BundleService, "update_bundle_font", side_effect=TimeoutError):
                card.usage_buttons["heading"].setChecked(True)
            self.assertTrue(card.usage_buttons["body"].isChecked())
            self.assertFalse(card.usage_buttons["heading"].isChecked())
            self.assertIn("保存失败", card.status.text())
            self.assertEqual(card.height(), 320)
            self.assertTrue(card.rect().contains(card.store_button.geometry()))
            card.dismiss("close")
            self.assertTrue((settings.ASSETS_DIR / bundle["source_key"]).is_file())
            app_gui.set_ui_language("en")
            card.show_bundle(card.bundle)
            self.assertEqual(card.close_button.accessibleName(), "Close")
            self.assertEqual(card.usage_buttons["body"].text(), "Body")
            card.dismiss("close")
        finally:
            app_gui.set_ui_language("auto")
            ball.close()
            self.app.processEvents()

    def test_font_collection_keeps_explicit_storage_configuration(self):
        from app.core.config import Settings
        from PySide6.QtTest import QTest
        settings = Settings(_env_file=None, STORAGE_DIR=self.root / "isolated", VISION_CLASSIFIER_ENABLED=False)
        global_settings = Settings(_env_file=None, STORAGE_DIR=self.root / "other", VISION_CLASSIFIER_ENABLED=False)
        ball = app_gui.HaypileFloatingBall(settings=settings)
        try:
            with patch.object(app_gui, "get_settings", return_value=global_settings):
                ball._start_worker([self.original])
                deadline = time.monotonic() + 3
                while ball.worker is not None and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.005)
            self.assertIsNone(ball.worker)
            bundles = ball._bundle_service().list_bundles()
            self.assertEqual(len(bundles), 1)
            self.assertEqual(bundles[0]["type"], "font")
            self.assertTrue(settings.MANIFEST_PATH.is_file())
            self.assertFalse(global_settings.STORAGE_DIR.exists())
        finally:
            ball.close()
            self.app.processEvents()

    def test_result_arriving_before_thread_stop_keeps_shutdown_owner(self):
        from app.core.config import Settings
        for kind in ("remote", "ingest"):
            with self.subTest(kind=kind):
                emitted, release = threading.Event(), threading.Event()
                settings = Settings(_env_file=None, STORAGE_DIR=self.root / kind, VISION_CLASSIFIER_ENABLED=False)
                ball = app_gui.HaypileFloatingBall(settings=settings)
                base = app_gui.RemoteDownloadWorker if kind == "remote" else app_gui.IngestWorker
                class EarlyResultWorker(base):
                    def run(self):
                        if kind == "remote":
                            self.finished_signal.emit([], "done", True)
                        else:
                            self.finished_signal.emit("done", True)
                        emitted.set()
                        release.wait(3)
                worker = (EarlyResultWorker([], self.incoming) if kind == "remote"
                          else EarlyResultWorker([], settings.ASSETS_DIR, settings=settings))
                worker.setParent(ball)
                attribute = "remote_worker" if kind == "remote" else "worker"
                setattr(ball, attribute, worker)
                if kind == "remote":
                    worker.finished_signal.connect(lambda files, message, success: ball._on_remote_download_finished(files, message, success, []))
                else:
                    worker.finished_signal.connect(ball._on_ingest_finished)
                try:
                    worker.start()
                    self.assertTrue(emitted.wait(2))
                    self.app.processEvents()
                    self.assertIs(getattr(ball, attribute), worker)
                    ball.shutdown()
                    self.assertFalse(ball._cleanup_done)
                    release.set()
                    deadline = time.monotonic() + 3
                    while not ball._cleanup_done and time.monotonic() < deadline:
                        self.app.processEvents()
                        time.sleep(.005)
                    self.assertTrue(ball._cleanup_done)
                finally:
                    release.set()
                    if getattr(ball, attribute) is not None:
                        worker.wait(3000)
                    ball.close()
                    self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
