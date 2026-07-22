from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from starlette.applications import Starlette
from starlette.routing import Mount

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.config import Settings, _ensure_private_directory
from app.core.exceptions import register_exception_handlers
from app.core.ipc import cleanup_unix_socket, start_ipc_listener
from app.main import ManifestStaticFiles, app
from app.services.asset_provenance import public_origin_url, sanitize_provenance
from app.services.bundle_service import BundleService
from app.services.json_io import atomic_write_json
from app.services.safe_remote_fetcher import MAX_REMOTE_URLS, dedupe_remote_urls
from app.services.scanner import AssetScanner
from app.services.vfs_storage import VFSStorage
from backend_host import ControlChannelServer


class LocalDataSecurityTests(unittest.TestCase):
    def test_provenance_removes_secrets_request_data_and_absolute_paths(self) -> None:
        payload = sanitize_provenance(
            {
                "origin_url": "https://cdn.example.com/hero.png",
                "source_key": "generic/images/hero.png",
                "temp_file": "/tmp/hero.png",
                "api_key": "secret",
                "request_body": {"image_bytes": "encoded"},
                "nested": {"local_path": "C:\\Users\\tester\\hero.png"},
                "ai_suggestions": {
                    "source": "model",
                    "reason": "r" * 200,
                    "agent_summary": "s" * 200,
                    "runtime_receipt": {"request_body": "secret"},
                    "unexpected": {"api_key": "secret"},
                    "trust": "untrusted_advisory",
                    "must_not_execute": True,
                },
            }
        )

        self.assertEqual(payload["origin_url"], "https://cdn.example.com")
        self.assertEqual(payload["source_key"], "generic/images/hero.png")
        self.assertNotIn("temp_file", payload)
        self.assertNotIn("api_key", payload)
        self.assertNotIn("request_body", payload)
        self.assertNotIn("nested", payload)
        self.assertEqual(len(payload["ai_suggestions"]["reason"]), 80)
        self.assertEqual(len(payload["ai_suggestions"]["agent_summary"]), 60)
        self.assertNotIn("runtime_receipt", payload["ai_suggestions"])
        self.assertNotIn("unexpected", payload["ai_suggestions"])
        self.assertTrue(payload["ai_suggestions"]["must_not_execute"])

        nonfinite = sanitize_provenance(
            {"ai_suggestions": {"confidence": {"role": float("nan")}}}
        )
        self.assertNotIn("ai_suggestions", nonfinite)

    def test_ipc_start_failure_does_not_log_local_socket_path(self) -> None:
        private_path = "/Users/tester/Library/Application Support/Haypile/storage/ipc.sock"
        channel = ControlChannelServer(object(), "127.0.0.1", 8010)
        with patch("backend_host.start_ipc_listener", side_effect=OSError(private_path)), self.assertLogs(
            "backend_host", level="ERROR"
        ) as captured:
            started = channel.start()

        self.assertFalse(started)
        self.assertNotIn(private_path, "\n".join(captured.output))

    def test_ipc_ping_identifies_haypile_and_startup_phase(self) -> None:
        server = type("Server", (), {"started": False})()
        channel = ControlChannelServer(server, "127.0.0.1", 8010)

        starting = channel._handle_payload({"type": "ping"})
        server.started = True
        ready = channel._handle_payload({"type": "ping"})

        self.assertEqual(starting["product"], "haypile")
        self.assertEqual(starting["protocol_version"], 1)
        self.assertEqual(starting["phase"], "starting")
        self.assertFalse(starting["ready"])
        self.assertEqual(ready["phase"], "ready")
        self.assertTrue(ready["ready"])

    def test_network_defaults_remain_local_only(self) -> None:
        settings = Settings(
            _env_file=None,
            HOST="0.0.0.0",
            CORS_ORIGINS=["*", "https://evil.example", "http://127.0.0.1:5173"],
            IPC_AUTHKEY="test-key",
        )

        self.assertEqual(settings.HOST, "127.0.0.1")
        self.assertEqual(settings.CORS_ORIGINS, ["http://127.0.0.1:5173"])
        self.assertEqual(Settings(_env_file=None, IPC_AUTHKEY="test-key").CORS_ORIGINS, [])

    def test_vision_model_endpoint_cannot_exfiltrate_assets(self) -> None:
        settings = Settings(
            _env_file=None,
            IPC_AUTHKEY="test-key",
            VISION_CLASSIFIER_BASE_URL="https://models.evil.example",
        )

        self.assertEqual(settings.VISION_CLASSIFIER_BASE_URL, "http://127.0.0.1:11434")

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_generated_ipc_secret_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            secret = Path(tmpdir) / "private" / "ipc_authkey"
            with patch.dict(
                os.environ,
                {"HAYPILE_IPC_AUTHKEY_FILE": str(secret), "IPC_AUTHKEY": ""},
                clear=False,
            ):
                settings = Settings(_env_file=None, IPC_AUTHKEY="")

            self.assertEqual(len(settings.IPC_AUTHKEY), 64)
            self.assertEqual(stat.S_IMODE(secret.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_local_files_and_directories_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage"
            _ensure_private_directory(root)
            payload_path = root / "state.json"
            atomic_write_json(payload_path, {"private": True})
            source = Path(tmpdir) / "source.bin"
            source.write_bytes(b"asset")
            destination = root / "assets" / "asset.bin"
            VFSStorage(copy_base_delay=0.1).materialize(source, destination)

            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(payload_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "Unix socket permissions only")
    def test_ipc_socket_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = str(Path(tmpdir) / "haypile.sock")
            listener = start_ipc_listener(address=address, authkey=b"test-secret")
            try:
                self.assertEqual(stat.S_IMODE(Path(address).stat().st_mode), 0o600)
            finally:
                listener.close()
                cleanup_unix_socket(address)

    def test_public_origin_strips_credentials_and_query_secrets(self) -> None:
        self.assertEqual(
            public_origin_url("https://user:secret@cdn.example.com:8443/a.png?token=private#part"),
            "https://cdn.example.com:8443",
        )

    def test_static_assets_send_private_sandbox_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assets = root / "assets"
            manifest = root / "manifest.json"
            assets.mkdir()
            (assets / "active.svg").write_text("<svg><script>alert(1)</script></svg>", encoding="utf-8")
            manifest.write_text(json.dumps({"active.svg": {"type": "image"}}), encoding="utf-8")
            static = ManifestStaticFiles(directory=str(assets), manifest_path=manifest)
            local_app = Starlette(routes=[Mount("/static", app=static)])

            with TestClient(local_app, base_url="http://127.0.0.1") as client:
                response = client.get("/static/active.svg")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertEqual(response.headers["content-security-policy"], "default-src 'none'; sandbox")
            self.assertEqual(response.headers["cross-origin-resource-policy"], "same-origin")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_untrusted_host_is_rejected(self) -> None:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.get(
                "/healthz",
                headers={"host": "evil.example"},
            )
        self.assertEqual(response.status_code, 400)

    def test_remote_drop_has_a_bounded_url_count(self) -> None:
        urls = [f"https://cdn.example.com/{index}.png" for index in range(100)]
        self.assertEqual(len(dedupe_remote_urls(urls)), MAX_REMOTE_URLS)

    def test_internal_errors_do_not_echo_local_paths(self) -> None:
        private_path = "/Users/tester/Library/Application Support/Haypile/storage/assets/secret.png"
        local_app = FastAPI()
        register_exception_handlers(local_app)

        @local_app.get("/boom")
        async def boom() -> None:
            raise RuntimeError(private_path)

        with self.assertLogs("app.core.exceptions", level="ERROR") as captured:
            with TestClient(local_app, raise_server_exceptions=False) as client:
                response = client.get("/boom")

        self.assertEqual(response.status_code, 500)
        self.assertNotIn(private_path, response.text)
        self.assertIsNone(response.json()["detail"])
        self.assertNotIn(private_path, "\n".join(captured.output))

    def test_scanner_rejects_decompression_bomb_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "bomb.png"
            image_path.write_bytes(b"not decoded")
            scanner = AssetScanner(assets_dir=Path(tmpdir), manifest_path=Path(tmpdir) / "manifest.json")

            with patch("app.services.media_validator.Image.open", side_effect=Image.DecompressionBombError):
                self.assertIsNone(scanner._scan_image(image_path))

    def test_bundle_service_does_not_read_manifest_paths_outside_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assets = root / "assets"
            index = root / "index"
            themes = root / "themes"
            assets.mkdir()
            index.mkdir()
            themes.mkdir()
            outside = root / "secret.png"
            outside.write_bytes(b"private")
            (index / "assets_manifest.json").write_text(
                json.dumps({"../secret.png": {"type": "image", "url_path": "/static/../secret.png"}}),
                encoding="utf-8",
            )

            service = BundleService(
                assets_dir=assets,
                manifest_path=index / "assets_manifest.json",
                themes_dir=themes,
                runtime_db_path=index / "storage_runtime.db",
            )

            self.assertEqual(service.list_bundles(), [])


if __name__ == "__main__":
    unittest.main()
