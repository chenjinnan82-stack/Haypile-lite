from __future__ import annotations

from contextlib import contextmanager
import http.client
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.services.safe_remote_fetcher import SafeFetchError, download_remote_media, open_safe_remote
from app.services.web_font_service import read_web_text


class RemoteFetchDeadlineTests(unittest.TestCase):
    @contextmanager
    def slow_response(self, phase: str, *, content_type: str = "image/png"):
        """Exercise the real stdlib parser over a socket, with no network access."""
        client, server = socket.socketpair()
        finished = threading.Event()
        connections = []

        class PublicPeerSocket:
            def getpeername(self):
                return "8.8.8.8", 80

            def __getattr__(self, name):
                return getattr(client, name)

        class Connection(http.client.HTTPConnection):
            def __init__(self, target, address, timeout):
                super().__init__(target.host, target.port, timeout=timeout)
                connections.append(self)

            def connect(self):
                client.settimeout(self.timeout)
                self.sock = PublicPeerSocket()

        def serve():
            try:
                server.recv(8192)
                if phase == "headers":
                    server.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    ending = b"\r\nContent-Length: 0\r\n\r\n"
                else:
                    size = b"Content-Length: 20\r\n" if phase == "body" else b"Connection: close\r\n"
                    if phase == "partial_body":
                        size = b"Content-Length: 65556\r\n"
                    if phase == "chunked_body":
                        size = b"Transfer-Encoding: chunked\r\n"
                    server.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + content_type.encode() + b"\r\n" + size + b"\r\n")
                    if phase == "partial_body":
                        server.sendall(b"p" * 65536)
                    ending = b"0\r\n\r\n" if phase == "chunked_body" else b""
                for _ in range(20):
                    if finished.wait(0.03):
                        return
                    server.sendall(b"1\r\nx\r\n" if phase == "chunked_body" else b"x")
                server.sendall(ending)
            except OSError:
                pass  # The fetcher shuts down the connection when its budget expires.
            finally:
                server.close()

        thread = threading.Thread(target=serve, name="test-slow-response")
        thread.start()

        def opener(url, **kwargs):
            return open_safe_remote(url, resolver=lambda *_a, **_k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
            ], **kwargs)

        try:
            with patch("app.services.safe_remote_fetcher._PinnedHTTPConnection", Connection):
                yield opener
        finally:
            finished.set()
            client.close()
            server.close()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertTrue(all(connection.sock is None for connection in connections))
            self.assertEqual(client.fileno(), -1)
            self.assertFalse(any(thread.name == "HaypileRemoteFetch" for thread in threading.enumerate()))

    def test_slow_headers_and_bodies_obey_absolute_deadline(self):
        for phase in ("headers", "body", "close_body", "chunked_body"):
            with self.subTest(phase=phase), self.slow_response(phase) as opener:
                started = time.monotonic()
                with self.assertRaisesRegex(SafeFetchError, "download_timeout"):
                    with opener("http://public.example/test.png", timeout=0.12) as response:
                        list(response.iter_bytes())
                self.assertLess(time.monotonic() - started, 0.35)

    def test_cancellation_interrupts_headers_and_bodies(self):
        for phase in ("headers", "body", "close_body", "chunked_body"):
            with self.subTest(phase=phase), self.slow_response(phase) as opener:
                started = time.monotonic()
                with self.assertRaisesRegex(SafeFetchError, "interrupted"):
                    with opener("http://public.example/test.png", timeout=2,
                                should_stop=lambda: time.monotonic() - started >= 0.10) as response:
                        list(response.iter_bytes())
                self.assertLess(time.monotonic() - started, 0.35)

    def test_watchdog_callback_error_interrupts_fetch_and_reaches_owner(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase), self.slow_response(phase) as opener:
                started = time.monotonic()
                callback_error = RuntimeError("watchdog callback failed")
                thread_errors = []

                def should_stop():
                    if threading.current_thread().name == "HaypileRemoteFetch" and time.monotonic() - started >= 0.05:
                        raise callback_error
                    return False

                captured = None
                with patch("threading.excepthook", side_effect=thread_errors.append):
                    try:
                        with opener("http://public.example/test.png", timeout=0.12,
                                    should_stop=should_stop) as response:
                            list(response.iter_bytes())
                    except BaseException as exc:
                        captured = exc
                self.assertLess(time.monotonic() - started, 0.35)
                self.assertIs(captured, callback_error)
                self.assertEqual(thread_errors, [])

    def test_media_cancellation_removes_partial_download(self):
        with tempfile.TemporaryDirectory() as raw, self.slow_response("partial_body") as opener:
            started = time.monotonic()
            path, reason = download_remote_media("http://public.example/test.png", Path(raw), 1,
                max_bytes=100000, timeout=2, opener=opener,
                should_stop=lambda: time.monotonic() - started >= 0.10)
            self.assertEqual((path, reason), (None, "interrupted"))
            self.assertLess(time.monotonic() - started, 0.35)
            self.assertEqual(list(Path(raw).iterdir()), [])

    def test_media_timeout_removes_partial_download(self):
        with tempfile.TemporaryDirectory() as raw, self.slow_response("partial_body") as opener:
            with self.assertRaisesRegex(SafeFetchError, "download_timeout"):
                download_remote_media("http://public.example/test.png", Path(raw), 1,
                                      max_bytes=100000, timeout=0.12, opener=opener)
            self.assertEqual(list(Path(raw).iterdir()), [])

    def test_web_text_cancellation_reaches_blocked_fetch(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase), self.slow_response(phase, content_type="text/html") as opener:
                started = time.monotonic()
                with self.assertRaisesRegex(SafeFetchError, "interrupted"):
                    read_web_text("http://public.example/page", opener=opener, timeout=2,
                                  should_stop=lambda: time.monotonic() - started >= 0.10)
                self.assertLess(time.monotonic() - started, 0.35)

    def test_early_response_close_releases_socket_and_watchdog(self):
        with self.slow_response("close_body") as opener:
            with opener("http://public.example/test.png", timeout=2) as response:
                self.assertEqual(response.status, 200)

    def test_cancellation_is_checked_before_and_after_dns(self):
        def unused_resolver(*_args, **_kwargs):
            self.fail("Cancelled requests must not resolve a host")

        with self.assertRaisesRegex(SafeFetchError, "interrupted"):
            open_safe_remote("http://public.example/test.png", resolver=unused_resolver, should_stop=lambda: True)

        stopped = False

        def resolver(*_args, **_kwargs):
            nonlocal stopped
            stopped = True
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]

        with self.assertRaisesRegex(SafeFetchError, "interrupted"):
            open_safe_remote("http://public.example/test.png", resolver=resolver, should_stop=lambda: stopped)

    def test_tls_handshake_is_watched_after_socket_is_available(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                client, server = socket.socketpair()
                context = ssl.create_default_context()

                def connect(_address, timeout):
                    client.settimeout(timeout)  # Match socket.create_connection's contract.
                    return client

                started = time.monotonic()
                try:
                    with patch("app.services.safe_remote_fetcher.socket.create_connection", side_effect=connect), \
                         patch.object(ssl.SSLSocket, "shutdown", return_value=None), \
                         patch.object(context, "wrap_socket", wraps=context.wrap_socket) as wrap:
                        with self.assertRaisesRegex(SafeFetchError, "interrupted" if cancelled else "download_timeout"):
                            open_safe_remote("https://8.8.8.8/test.png", timeout=2 if cancelled else 0.12,
                                ssl_context=context,
                                should_stop=lambda: cancelled and time.monotonic() - started >= 0.10)
                    self.assertLess(time.monotonic() - started, 0.35)
                    self.assertEqual(wrap.call_args.kwargs["server_hostname"], "8.8.8.8")
                    self.assertFalse(wrap.call_args.kwargs["do_handshake_on_connect"])
                    self.assertEqual(client.fileno(), -1)
                    self.assertFalse(any(thread.name == "HaypileRemoteFetch" for thread in threading.enumerate()))
                finally:
                    client.close()
                    server.close()


if __name__ == "__main__":
    unittest.main()
