from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from fontTools.ttLib import TTFont
from PySide6.QtCore import QMimeData, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog

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
        self.add_page('<base href="/assets/"><link rel="stylesheet" href="main.css"><a href="copy.woff2">Font</a>')
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

    def test_page_stylesheet_font_budget_and_cancellation_cleanup(self):
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
                def choose():
                    dialog = ball._font_selection_dialog
                    self.assertTrue(ball._ingest_busy())
                    if accept:
                        dialog.font_list.item(0).setCheckState(Qt.CheckState.Checked)
                        dialog.accept()
                    else:
                        dialog.reject()
                QTimer.singleShot(0, choose)
                paths = [candidate.path for candidate in candidates]
                ball._on_remote_download_finished(paths, "ok", True, [])
                self.assertEqual(received, [[paths[0]]] if accept else [])
                self.assertFalse(paths[1].exists())
                self.assertEqual(ball._remote_ingest_paths, {paths[0]} if accept else set())
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
        dialog = app_gui.WebFontSelectionDialog(worker.font_candidates)
        try:
            self.assertFalse(dialog.store_button.isEnabled())
            self.assertEqual(dialog.selected_paths(), [])
            self.assertIn("冬藏", dialog.details.toPlainText())
            self.assertFalse(dialog.preview.pixmap().isNull())
            QTimer.singleShot(0, dialog.reject)
            self.assertEqual(dialog.exec(), QDialog.DialogCode.Rejected)
            self.assertFalse(dialog.selected_paths())
            dialog.font_list.item(0).setCheckState(Qt.CheckState.Checked)
            self.assertTrue(dialog.store_button.isEnabled())
            self.assertEqual(dialog.selected_paths(), results[0][0])
        finally:
            dialog.close()
            dialog.deleteLater()
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


if __name__ == "__main__":
    unittest.main()
